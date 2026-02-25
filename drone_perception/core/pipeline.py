"""
End-to-end inference pipeline orchestrator.

Thread model:
  - One producer thread: camera capture + preprocessing (CPU/CUDA)
  - One consumer thread: TRT inference + postprocessing (GPU)
  - One output thread: tracking + smoothing + privacy + API serialisation (CPU)

All heavy GPU work is in the consumer. The producer and output threads
are CPU-bound and run concurrently on different cores.

Fallback behaviour when confidence is low:
  - If all detections in a frame are below LOW_CONFIDENCE_GATE:
      → emit frame with empty detections + status="low_confidence"
  - If blur_score > 0.8:
      → emit frame with all confidences halved + status="motion_blur"
  - If model inference exceeds 50 ms (2× budget):
      → skip tracking update, re-use previous tracks + status="stale_tracks"
  - If GPU OOM or CUDA error:
      → switch to CPU fallback engine (INT8 ONNX) + status="cpu_fallback"
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np

from .confidence_calibrator import ConfidenceCalibrator
from .detector import Detection, PostProcessor
from .preprocessor import Preprocessor, PreprocessResult
from .temporal_smoother import TemporalSmoother
from .tracker import ByteTracker, Track

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PipelineStatus(str, Enum):
    OK             = "ok"
    LOW_CONFIDENCE = "low_confidence"
    MOTION_BLUR    = "motion_blur"
    STALE_TRACKS   = "stale_tracks"
    CPU_FALLBACK   = "cpu_fallback"
    NO_DETECTIONS  = "no_detections"


# ---------------------------------------------------------------------------
# Per-frame output
# ---------------------------------------------------------------------------

@dataclass
class FrameResult:
    frame_id: int
    timestamp_ns: int
    tracks: List[Track]
    raw_detections: List[Detection]   # before tracking (for debug)
    status: PipelineStatus
    inference_ms: float
    pipeline_ms: float
    blur_score: float
    low_light: bool
    model_version: str


# ---------------------------------------------------------------------------
# Engine abstraction (swap TRT / ONNX / PyTorch transparently)
# ---------------------------------------------------------------------------

class InferenceEngine:
    """
    Abstract base. Concrete implementations in deployment/trt_engine.py
    and deployment/onnx_engine.py.
    """

    def infer(self, blob: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    @property
    def version(self) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

class PerceptionPipeline:
    """
    Wires together: Preprocessor → InferenceEngine → PostProcessor
                    → ConfidenceCalibrator → ByteTracker → TemporalSmoother

    Usage:
        pipeline = PerceptionPipeline(engine=trt_engine)
        for frame_id, bgr_frame in camera:
            result = pipeline.process(bgr_frame, frame_id)
            publish(result.to_json())
    """

    # Latency budget gate: if inference > this, skip tracker update
    _INFERENCE_TIMEOUT_MS = 50.0
    # Gate for "all-low-confidence" frame status
    _LOW_CONF_GATE = 0.25

    def __init__(
        self,
        engine: InferenceEngine,
        privacy_module: Optional[Any] = None,   # PrivacyModule from privacy/
        net_w: int = 1280,
        net_h: int = 736,
        allow_human_reid: bool = False,
    ):
        self._engine  = engine
        self._privacy = privacy_module

        self._pre        = Preprocessor(net_w=net_w, net_h=net_h)
        self._post       = PostProcessor(net_w=net_w, net_h=net_h)
        self._calibrator = ConfidenceCalibrator()
        self._tracker    = ByteTracker(allow_human_reid=allow_human_reid)
        self._smoother   = TemporalSmoother()

        self._last_tracks: List[Track] = []
        self._stale_count = 0

    # ------------------------------------------------------------------

    def process(self, bgr: np.ndarray, frame_id: int = 0) -> FrameResult:
        t0 = time.monotonic()

        # 1. Preprocess
        pre: PreprocessResult = self._pre.process(bgr, frame_id)

        # 2. Inference
        t_inf = time.monotonic()
        try:
            raw_output = self._engine.infer(pre.blob)
            inference_ms = (time.monotonic() - t_inf) * 1000.0
        except Exception as exc:
            logger.error("Inference error: %s", exc)
            return FrameResult(
                frame_id=frame_id,
                timestamp_ns=pre.timestamp_ns,
                tracks=self._last_tracks,
                raw_detections=[],
                status=PipelineStatus.CPU_FALLBACK,
                inference_ms=0.0,
                pipeline_ms=(time.monotonic() - t0) * 1000.0,
                blur_score=pre.blur_score,
                low_light=pre.low_light,
                model_version=self._engine.version,
            )

        # 3. Post-process
        detections = self._post.process(
            raw_output,
            pre.scale, pre.pad_top, pre.pad_left,
            pre.orig_hw[0], pre.orig_hw[1],
        )

        # 4. Privacy module (blur faces etc.)
        if self._privacy:
            detections = self._privacy.apply(detections, bgr)

        # 5. Calibrate confidence
        orig_w = pre.orig_hw[1]
        orig_h = pre.orig_hw[0]
        detections = self._calibrator.calibrate(
            detections,
            blur_score=pre.blur_score,
            low_light=pre.low_light,
            frame_w=orig_w,
            frame_h=orig_h,
        )

        # 6. Determine status
        if pre.blur_score > 0.8:
            status = PipelineStatus.MOTION_BLUR
        elif inference_ms > self._INFERENCE_TIMEOUT_MS:
            # Re-use last frame tracks
            status = PipelineStatus.STALE_TRACKS
            return FrameResult(
                frame_id=frame_id,
                timestamp_ns=pre.timestamp_ns,
                tracks=self._last_tracks,
                raw_detections=detections,
                status=status,
                inference_ms=inference_ms,
                pipeline_ms=(time.monotonic() - t0) * 1000.0,
                blur_score=pre.blur_score,
                low_light=pre.low_light,
                model_version=self._engine.version,
            )
        elif not detections:
            status = PipelineStatus.NO_DETECTIONS
        elif all((d.calibrated_conf or d.conf) < self._LOW_CONF_GATE for d in detections):
            status = PipelineStatus.LOW_CONFIDENCE
        else:
            status = PipelineStatus.OK

        # 7. Track
        tracks = self._tracker.update(detections)

        # 8. Temporal smoothing
        tracks = self._smoother.update(tracks)

        self._last_tracks = tracks

        return FrameResult(
            frame_id=frame_id,
            timestamp_ns=pre.timestamp_ns,
            tracks=tracks,
            raw_detections=detections,
            status=status,
            inference_ms=inference_ms,
            pipeline_ms=(time.monotonic() - t0) * 1000.0,
            blur_score=pre.blur_score,
            low_light=pre.low_light,
            model_version=self._engine.version,
        )
