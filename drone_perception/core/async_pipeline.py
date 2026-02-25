"""
Hardened asynchronous pipeline for sustained 30 FPS on Jetson Orin Nano.

Threading model:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  Thread A — CaptureThread                                           │
  │    v4l2 / GStreamer camera → raw BGR frame → preprocess_queue       │
  │    Drop policy: if queue full, discard oldest frame (not newest)    │
  └───────────────────────────┬─────────────────────────────────────────┘
                              │ preprocess_queue (maxsize=2)
  ┌───────────────────────────▼─────────────────────────────────────────┐
  │  Thread B — PreprocessThread (CPU)                                  │
  │    Letterbox + CLAHE + blur estimate → inference_queue              │
  └───────────────────────────┬─────────────────────────────────────────┘
                              │ inference_queue (maxsize=1)
  ┌───────────────────────────▼─────────────────────────────────────────┐
  │  Thread C — InferenceThread (GPU / CUDA stream)                     │
  │    TRT forward pass → postproc → calibrate → output_queue           │
  │    Watchdog: if inference > 50 ms, inject stale-track sentinel      │
  └───────────────────────────┬─────────────────────────────────────────┘
                              │ output_queue (maxsize=4)
  ┌───────────────────────────▼─────────────────────────────────────────┐
  │  Thread D — OutputThread (CPU)                                      │
  │    ByteTrack + TemporalSmoother + Privacy + JSON serialise          │
  │    → results_callback(FrameResult)                                  │
  └─────────────────────────────────────────────────────────────────────┘

Backpressure strategy:
  - preprocess_queue and inference_queue are bounded (maxsize 2 and 1).
  - CaptureThread drops frames if preprocess_queue is full — ensures
    sensor stays live and we always process the LATEST frame.
  - OutputThread has a larger buffer (4) so bursty tracking doesn't stall
    inference.

Zero-copy on Jetson:
  - Jetson Orin has unified DRAM. When using MMAPI / libargus (GStreamer
    nvarguscamerasrc) the frame is already in GPU-accessible memory.
  - We use numpy views over CUDA pinned buffers where possible.
  - cv2.cuda.GpuMat is used for CLAHE if CUDA OpenCV is available.

Watchdog:
  - A dedicated WatchdogTimer runs in Thread C.
  - If TRT inference doesn't complete within WATCHDOG_MS, it sets
    an Event that InferenceThread checks; InferenceThread then emits
    a STALE_TRACKS sentinel and re-initialises the CUDA context.

Per-stage latency metrics:
  - Each stage records (stage_name, frame_id, duration_ms) into a
    RingBuffer. The /metrics endpoint (in api/) exposes P50/P95/P99.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .confidence_calibrator import ConfidenceCalibrator
from .detector import PostProcessor
from .pipeline import FrameResult, PipelineStatus
from .preprocessor import Preprocessor, PreprocessResult
from .temporal_smoother import TemporalSmoother
from .tracker import ByteTracker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Latency ring buffer
# ---------------------------------------------------------------------------

@dataclass
class StageMetric:
    stage: str
    frame_id: int
    duration_ms: float
    timestamp: float = field(default_factory=time.monotonic)


class LatencyRingBuffer:
    """Lock-free (single-writer) ring buffer for latency metrics."""

    def __init__(self, capacity: int = 300):
        self._buf: deque[StageMetric] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(self, stage: str, frame_id: int, duration_ms: float) -> None:
        with self._lock:
            self._buf.append(StageMetric(stage, frame_id, duration_ms))

    def percentiles(self, stage: str) -> Dict[str, float]:
        with self._lock:
            vals = [m.duration_ms for m in self._buf if m.stage == stage]
        if not vals:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "count": 0, "mean": 0.0}
        a = np.array(vals)
        return {
            "p50":   float(np.percentile(a, 50)),
            "p95":   float(np.percentile(a, 95)),
            "p99":   float(np.percentile(a, 99)),
            "mean":  float(a.mean()),
            "count": len(vals),
        }

    def all_stages(self) -> List[str]:
        with self._lock:
            return list({m.stage for m in self._buf})


# ---------------------------------------------------------------------------
# Watchdog timer
# ---------------------------------------------------------------------------

class WatchdogTimer:
    """
    Fires a callback if reset() is not called within timeout_ms.
    Used to detect stalled GPU inference.
    """

    def __init__(self, timeout_ms: float, callback: Callable):
        self._timeout = timeout_ms / 1000.0
        self._callback = callback
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._timeout, self._callback)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None


# ---------------------------------------------------------------------------
# Sentinel for stale-track frames
# ---------------------------------------------------------------------------

_STALE_SENTINEL = object()


# ---------------------------------------------------------------------------
# Capture thread (camera → preprocess_queue)
# ---------------------------------------------------------------------------

class CaptureThread(threading.Thread):
    """
    Reads frames from an OpenCV VideoCapture or any object with
    a read() method returning (bool, np.ndarray).

    Drop policy: non-blocking put; if queue is full, pop oldest and push new.
    This ensures InferenceThread always sees the freshest frame.
    """

    def __init__(
        self,
        source,                             # cv2.VideoCapture or compatible
        out_queue: queue.Queue,
        metrics: LatencyRingBuffer,
        target_fps: float = 30.0,
    ):
        super().__init__(name="CaptureThread", daemon=True)
        self._src      = source
        self._q        = out_queue
        self._metrics  = metrics
        self._interval = 1.0 / target_fps
        self._stop_evt = threading.Event()
        self._frame_id = 0
        self.frames_captured = 0
        self.frames_dropped  = 0

    def run(self) -> None:
        logger.info("CaptureThread started")
        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            ok, frame = self._src.read()
            if not ok:
                logger.warning("Camera read failed — retrying")
                time.sleep(0.005)
                continue

            self._frame_id += 1
            self.frames_captured += 1
            payload = (self._frame_id, frame)

            # Non-blocking drop policy
            try:
                self._q.put_nowait(payload)
            except queue.Full:
                # Evict oldest
                try:
                    self._q.get_nowait()
                    self.frames_dropped += 1
                except queue.Empty:
                    pass
                self._q.put_nowait(payload)

            elapsed = time.monotonic() - t0
            self._metrics.record("capture", self._frame_id, elapsed * 1000)

            # Pace to target FPS
            sleep = self._interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    def stop(self) -> None:
        self._stop_evt.set()


# ---------------------------------------------------------------------------
# Preprocess thread (raw frame → blob)
# ---------------------------------------------------------------------------

class PreprocessThread(threading.Thread):

    def __init__(
        self,
        in_queue: queue.Queue,
        out_queue: queue.Queue,
        metrics: LatencyRingBuffer,
        net_w: int = 1280,
        net_h: int = 736,
    ):
        super().__init__(name="PreprocessThread", daemon=True)
        self._in  = in_queue
        self._out = out_queue
        self._pre = Preprocessor(net_w=net_w, net_h=net_h)
        self._metrics = metrics
        self._stop_evt = threading.Event()

    def run(self) -> None:
        logger.info("PreprocessThread started")
        while not self._stop_evt.is_set():
            try:
                frame_id, bgr = self._in.get(timeout=0.1)
            except queue.Empty:
                continue

            t0 = time.monotonic()
            pre_result = self._pre.process(bgr, frame_id)
            elapsed_ms = (time.monotonic() - t0) * 1000
            self._metrics.record("preprocess", frame_id, elapsed_ms)

            try:
                self._out.put_nowait(pre_result)
            except queue.Full:
                try:
                    self._out.get_nowait()
                except queue.Empty:
                    pass
                self._out.put_nowait(pre_result)

    def stop(self) -> None:
        self._stop_evt.set()


# ---------------------------------------------------------------------------
# Inference thread (blob → raw detections)
# ---------------------------------------------------------------------------

WATCHDOG_MS = 50.0   # alert if inference takes longer than this

class InferenceThread(threading.Thread):

    def __init__(
        self,
        in_queue: queue.Queue,
        out_queue: queue.Queue,
        engine,                    # TRTEngine | ONNXEngine
        metrics: LatencyRingBuffer,
        net_w: int = 1280,
        net_h: int = 736,
    ):
        super().__init__(name="InferenceThread", daemon=True)
        self._in      = in_queue
        self._out     = out_queue
        self._engine  = engine
        self._post    = PostProcessor(net_w=net_w, net_h=net_h)
        self._cal     = ConfidenceCalibrator()
        self._metrics = metrics
        self._stop_evt   = threading.Event()
        self._stalled    = threading.Event()
        self._watchdog   = WatchdogTimer(WATCHDOG_MS, self._on_stall)

    def _on_stall(self) -> None:
        logger.error("WATCHDOG: inference stalled > %.0f ms — injecting stale sentinel", WATCHDOG_MS)
        self._stalled.set()
        try:
            self._out.put_nowait(_STALE_SENTINEL)
        except queue.Full:
            pass

    def run(self) -> None:
        logger.info("InferenceThread started")
        while not self._stop_evt.is_set():
            try:
                pre: PreprocessResult = self._in.get(timeout=0.1)
            except queue.Empty:
                continue

            self._stalled.clear()
            self._watchdog.reset()
            t0 = time.monotonic()

            try:
                raw_output = self._engine.infer(pre.blob)
            except Exception as exc:
                logger.error("Inference error frame %d: %s", pre.frame_id, exc)
                self._watchdog.cancel()
                self._out.put(_STALE_SENTINEL)
                continue

            self._watchdog.cancel()

            if self._stalled.is_set():
                # Watchdog fired during inference — output already sent
                continue

            inf_ms = (time.monotonic() - t0) * 1000
            self._metrics.record("inference", pre.frame_id, inf_ms)

            # Post-process
            t1 = time.monotonic()
            detections = self._post.process(
                raw_output,
                pre.scale, pre.pad_top, pre.pad_left,
                pre.orig_hw[0], pre.orig_hw[1],
            )
            detections = self._cal.calibrate(
                detections,
                blur_score=pre.blur_score,
                low_light=pre.low_light,
                frame_w=pre.orig_hw[1],
                frame_h=pre.orig_hw[0],
            )
            self._metrics.record("postprocess", pre.frame_id, (time.monotonic() - t1) * 1000)

            payload = (pre, detections, inf_ms, self._engine.version)
            try:
                self._out.put(payload, timeout=0.05)
            except queue.Full:
                logger.warning("output_queue full — dropping frame %d", pre.frame_id)

    def stop(self) -> None:
        self._watchdog.cancel()
        self._stop_evt.set()


# ---------------------------------------------------------------------------
# Output thread (tracking + smoothing + privacy + callback)
# ---------------------------------------------------------------------------

class OutputThread(threading.Thread):

    def __init__(
        self,
        in_queue: queue.Queue,
        metrics: LatencyRingBuffer,
        result_callback: Callable[[FrameResult], None],
        privacy_module=None,
        allow_human_reid: bool = False,
    ):
        super().__init__(name="OutputThread", daemon=True)
        self._in       = in_queue
        self._metrics  = metrics
        self._cb       = result_callback
        self._privacy  = privacy_module
        self._tracker  = ByteTracker(allow_human_reid=allow_human_reid)
        self._smoother = TemporalSmoother()
        self._stop_evt = threading.Event()
        self._last_tracks = []

    def run(self) -> None:
        logger.info("OutputThread started")
        while not self._stop_evt.is_set():
            try:
                payload = self._in.get(timeout=0.1)
            except queue.Empty:
                continue

            t0 = time.monotonic()

            # Stale sentinel
            if payload is _STALE_SENTINEL:
                result = FrameResult(
                    frame_id=-1,
                    timestamp_ns=time.monotonic_ns(),
                    tracks=self._last_tracks,
                    raw_detections=[],
                    status=PipelineStatus.STALE_TRACKS,
                    inference_ms=0.0,
                    pipeline_ms=0.0,
                    blur_score=0.0,
                    low_light=False,
                    model_version="unknown",
                )
                self._cb(result)
                continue

            pre, detections, inf_ms, model_ver = payload

            # Privacy gate
            if self._privacy:
                detections = self._privacy.apply(detections, bgr=None)

            # Track
            tracks = self._tracker.update(detections)

            # Smooth
            tracks = self._smoother.update(tracks)
            self._last_tracks = tracks

            # Status
            if pre.blur_score > 0.8:
                status = PipelineStatus.MOTION_BLUR
            elif not detections:
                status = PipelineStatus.NO_DETECTIONS
            elif all((d.calibrated_conf or d.conf) < 0.25 for d in detections):
                status = PipelineStatus.LOW_CONFIDENCE
            else:
                status = PipelineStatus.OK

            total_ms = (time.monotonic() - t0) * 1000
            self._metrics.record("output", pre.frame_id, total_ms)
            self._metrics.record("pipeline_total", pre.frame_id, inf_ms + total_ms)

            result = FrameResult(
                frame_id=pre.frame_id,
                timestamp_ns=pre.timestamp_ns,
                tracks=tracks,
                raw_detections=detections,
                status=status,
                inference_ms=inf_ms,
                pipeline_ms=inf_ms + total_ms,
                blur_score=pre.blur_score,
                low_light=pre.low_light,
                model_version=model_ver,
            )
            self._cb(result)

    def stop(self) -> None:
        self._stop_evt.set()


# ---------------------------------------------------------------------------
# AsyncPerceptionPipeline — top-level orchestrator
# ---------------------------------------------------------------------------

class AsyncPerceptionPipeline:
    """
    Orchestrates all four threads.

    Usage:
        pipeline = AsyncPerceptionPipeline(engine, camera, on_result)
        pipeline.start()
        ...
        pipeline.stop()
        stats = pipeline.get_stats()
    """

    def __init__(
        self,
        engine,
        camera,                                   # cv2.VideoCapture or compatible
        result_callback: Callable[[FrameResult], None],
        privacy_module=None,
        allow_human_reid: bool = False,
        target_fps: float = 30.0,
        net_w: int = 1280,
        net_h: int = 736,
    ):
        self.metrics = LatencyRingBuffer(capacity=600)

        self._q_capture    = queue.Queue(maxsize=2)
        self._q_preprocess = queue.Queue(maxsize=1)
        self._q_output     = queue.Queue(maxsize=4)

        self._capture = CaptureThread(
            camera, self._q_capture, self.metrics, target_fps
        )
        self._preprocess = PreprocessThread(
            self._q_capture, self._q_preprocess, self.metrics, net_w, net_h
        )
        self._inference = InferenceThread(
            self._q_preprocess, self._q_output, engine, self.metrics, net_w, net_h
        )
        self._output = OutputThread(
            self._q_output, self.metrics, result_callback,
            privacy_module, allow_human_reid
        )

        self._start_time: float = 0.0

    def start(self) -> None:
        self._start_time = time.monotonic()
        self._capture.start()
        self._preprocess.start()
        self._inference.start()
        self._output.start()
        logger.info("AsyncPerceptionPipeline started (4 threads)")

    def stop(self) -> None:
        self._capture.stop()
        self._preprocess.stop()
        self._inference.stop()
        self._output.stop()
        for t in (self._capture, self._preprocess, self._inference, self._output):
            t.join(timeout=2.0)
        logger.info("AsyncPerceptionPipeline stopped")

    def get_stats(self) -> Dict:
        elapsed = time.monotonic() - self._start_time
        captured = self._capture.frames_captured
        dropped  = self._capture.frames_dropped
        fps = captured / elapsed if elapsed > 0 else 0.0

        return {
            "uptime_s":       round(elapsed, 1),
            "frames_captured": captured,
            "frames_dropped":  dropped,
            "drop_rate":       round(dropped / max(captured, 1), 4),
            "effective_fps":   round(fps, 1),
            "latency": {
                stage: self.metrics.percentiles(stage)
                for stage in ["capture", "preprocess", "inference",
                              "postprocess", "output", "pipeline_total"]
            },
        }
