#!/usr/bin/env python3
"""
Drone Perception System — Main Runner
======================================

Async pipeline diagram:
  Camera → [CaptureThread] → q_cap(2)
         → [PreprocessThread] → q_pre(1)
         → [InferenceThread + Watchdog] → q_out(4)
         → [OutputThread] → result_callback
         → FastAPI WebSocket /stream

Usage:
  # Basic: ONNX CPU fallback (development / no Jetson)
  python -m drone_perception.main --onnx weights/yolov9s_drone.onnx

  # Production: TensorRT FP16 on Jetson Orin Nano
  python -m drone_perception.main \
    --engine weights/yolov9s_drone.engine \
    --onnx   weights/yolov9s_drone.onnx   \
    --camera 0 \
    --api-port 8080 \
    --log-dir /var/log/drone_perception

  # With privacy operator token (SAR face re-ID authorised)
  DRONE_OPERATOR_TOKEN=<jwt> python -m drone_perception.main ...

Environment variables:
  DRONE_FRAME_KEY        Fernet key for frame snapshot encryption
  DRONE_OPERATOR_TOKEN   JWT operator token (if empty: anonymous mode)
  DRONE_API_KEYS         Comma-separated valid API keys for HTTP endpoints
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .api.schema import PerceptionFrame, frame_result_to_output
from .core.async_pipeline import AsyncPerceptionPipeline
from .core.health_monitor import (
    DegradationLevel, FPSCounter, HealthMonitor, StageHealth,
    DEGRADATION_RESOLUTIONS, configure_structured_logging, warmup_engine,
)
from .core.pipeline import FrameResult
from .core.timing import FrameTimestamp, check_budget, now_ns
from .deployment.trt_engine import create_engine
from .privacy.privacy_module import OperatorToken, PrivacyModule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drone Perception System — civilian SAR/inspection/awareness",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--engine",   default=None,    help="TensorRT .engine file")
    p.add_argument("--onnx",     default=None,    help="ONNX model file (fallback)")
    p.add_argument("--camera",   default=0,       help="Camera index or GStreamer pipeline string")
    p.add_argument("--fps",      type=float, default=30.0, help="Target capture FPS")
    p.add_argument("--net-w",    type=int,   default=1280, help="Network input width")
    p.add_argument("--net-h",    type=int,   default=736,  help="Network input height")
    p.add_argument("--api-port", type=int,   default=8080, help="FastAPI HTTP port")
    p.add_argument("--log-dir",  default=None,    help="Directory for JSON log files")
    p.add_argument("--no-api",   action="store_true", help="Disable HTTP API server")
    p.add_argument("--warmup-runs", type=int, default=15, help="TRT warmup iterations")
    p.add_argument("--debug",    action="store_true", help="Set log level to DEBUG")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Result handler
# ---------------------------------------------------------------------------

class ResultHandler:
    """
    Receives FrameResult from OutputThread, converts to API schema,
    logs metrics, and optionally feeds the FastAPI state.
    """

    def __init__(
        self,
        fps_counter: FPSCounter,
        health_stages: dict,
        api_state=None,
    ):
        self._fps    = fps_counter
        self._stages = health_stages
        self._api    = api_state
        self._current_level = DegradationLevel.NORMAL
        self._frame_count = 0
        self._last_log_time = time.monotonic()

    def on_result(self, result: FrameResult) -> None:
        self._fps.tick()
        self._frame_count += 1

        # Update stage heartbeat
        if "output" in self._stages:
            self._stages["output"].update_heartbeat()

        # Convert to API schema
        frame = frame_result_to_output(result, degradation_level=self._current_level.name)

        # Push to API
        if self._api:
            self._api.latest_frame = frame

        # Log budget violations
        if result.pipeline_ms > 33.3:
            logger.warning(
                "Budget exceeded",
                extra={"frame_id": result.frame_id, "pipeline_ms": round(result.pipeline_ms, 2)},
            )

        # Periodic stats log every 5 seconds
        now = time.monotonic()
        if now - self._last_log_time >= 5.0:
            fps = self._fps.current_fps()
            logger.info(
                "Pipeline stats",
                extra={
                    "fps":          round(fps, 1),
                    "frame_count":  self._frame_count,
                    "status":       result.status.value,
                    "inference_ms": round(result.inference_ms, 2),
                    "pipeline_ms":  round(result.pipeline_ms, 2),
                    "blur_score":   round(result.blur_score, 3),
                    "low_light":    result.low_light,
                    "tracks":       len(result.tracks),
                },
            )
            self._last_log_time = now


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    # Configure structured JSON logging
    configure_structured_logging(
        log_dir=Path(args.log_dir) if args.log_dir else None,
        level=logging.DEBUG if args.debug else logging.INFO,
    )

    logger.info(
        "Drone perception system starting",
        extra={"engine": args.engine, "onnx": args.onnx, "camera": args.camera},
    )

    # ------------------------------------------------------------------
    # 1. Load inference engine
    # ------------------------------------------------------------------
    if not args.engine and not args.onnx:
        logger.error("Must provide --engine or --onnx")
        return 1

    try:
        engine = create_engine(
            engine_path=args.engine,
            onnx_path=args.onnx,
            fp16=True,
        )
    except Exception as exc:
        logger.error("Failed to load engine: %s", exc)
        return 1

    # ------------------------------------------------------------------
    # 2. Warmup TRT engine (prevents first-frame latency spike)
    # ------------------------------------------------------------------
    try:
        steady_ms = warmup_engine(engine, args.net_w, args.net_h, args.warmup_runs)
        if steady_ms > 10.0:
            logger.warning(
                "Warmup latency high — consider INT8 or resolution reduction",
                extra={"steady_ms": round(steady_ms, 2)},
            )
    except Exception as exc:
        logger.warning("Warmup skipped: %s", exc)

    # ------------------------------------------------------------------
    # 3. Open camera
    # ------------------------------------------------------------------
    import cv2
    cam_src = int(args.camera) if str(args.camera).isdigit() else args.camera

    # Prefer GStreamer nvarguscamerasrc on Jetson for CSI cameras
    gst_pipeline = (
        f"nvarguscamerasrc ! "
        f"video/x-raw(memory:NVMM), width=1280, height=720, framerate={int(args.fps)}/1 ! "
        f"nvvidconv ! video/x-raw, format=BGRx ! videoconvert ! appsink"
    )
    cap = cv2.VideoCapture(gst_pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        logger.info("GStreamer pipeline unavailable — using V4L2 fallback")
        cap = cv2.VideoCapture(cam_src)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_FPS,          args.fps)

    if not cap.isOpened():
        logger.error("Cannot open camera: %s", cam_src)
        return 1

    # ------------------------------------------------------------------
    # 4. Privacy module
    # ------------------------------------------------------------------
    import os
    token_str = os.environ.get("DRONE_OPERATOR_TOKEN")
    if token_str:
        # In production, validate JWT here; for now use anonymous
        logger.warning("Operator token validation not implemented — using anonymous mode")
    privacy = PrivacyModule(token=OperatorToken.anonymous())

    # ------------------------------------------------------------------
    # 5. Health monitoring
    # ------------------------------------------------------------------
    fps_counter = FPSCounter(window_s=2.0)
    stage_healths = {
        "capture":    StageHealth("capture",    budget_ms=3.0),
        "preprocess": StageHealth("preprocess", budget_ms=3.0),
        "inference":  StageHealth("inference",  budget_ms=7.0),
        "output":     StageHealth("output",     budget_ms=5.0),
    }

    current_level = [DegradationLevel.NORMAL]

    def on_degradation(level: DegradationLevel) -> None:
        current_level[0] = level
        new_w, new_h = DEGRADATION_RESOLUTIONS[level]
        logger.warning(
            "Resolution degraded",
            extra={"level": level.name, "width": new_w, "height": new_h},
        )
        # TODO: hot-reload pipeline with new resolution (requires reinit)

    health_monitor = HealthMonitor(
        fps_counter=fps_counter,
        stage_healths=stage_healths,
        on_fps_alert=lambda fps: logger.warning("FPS alert", extra={"fps": fps}),
        on_stage_dead=lambda name: logger.critical("Stage dead", extra={"stage": name}),
        on_degradation=on_degradation,
    )

    # ------------------------------------------------------------------
    # 6. API server (optional)
    # ------------------------------------------------------------------
    api_state = None
    if not args.no_api:
        try:
            from .api.server import state as api_state_obj, run_server, _VALID_KEYS
            api_state = api_state_obj
            api_state.metrics       = None   # set after pipeline start
            api_state.health_monitor = health_monitor
            api_state.fps_counter   = fps_counter

            # Load API keys from env
            keys_str = os.environ.get("DRONE_API_KEYS", "")
            if keys_str:
                _VALID_KEYS.update(keys_str.split(","))

            api_thread = threading.Thread(
                target=run_server,
                kwargs={"host": "0.0.0.0", "port": args.api_port},
                daemon=True,
            )
            api_thread.start()
            logger.info("API server started on port %d", args.api_port)
        except Exception as exc:
            logger.warning("API server disabled: %s", exc)
            api_state = None

    # ------------------------------------------------------------------
    # 7. Result handler
    # ------------------------------------------------------------------
    handler = ResultHandler(fps_counter, stage_healths, api_state)

    def result_callback(result: FrameResult) -> None:
        handler.on_result(result)

    # ------------------------------------------------------------------
    # 8. Build and start async pipeline
    # ------------------------------------------------------------------
    pipeline = AsyncPerceptionPipeline(
        engine=engine,
        camera=cap,
        result_callback=result_callback,
        privacy_module=privacy,
        allow_human_reid=False,
        target_fps=args.fps,
        net_w=args.net_w,
        net_h=args.net_h,
    )

    if api_state:
        api_state.pipeline = pipeline
        api_state.metrics  = pipeline.metrics

    health_monitor.start()
    pipeline.start()

    logger.info(
        "Pipeline running",
        extra={"target_fps": args.fps, "resolution": f"{args.net_w}x{args.net_h}"},
    )

    # ------------------------------------------------------------------
    # 9. Graceful shutdown on SIGINT / SIGTERM
    # ------------------------------------------------------------------
    _stop = threading.Event()

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received: %s", sig)
        _stop.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        _stop.wait()
    finally:
        logger.info("Stopping pipeline...")
        pipeline.stop()
        health_monitor.stop()
        cap.release()

        stats = pipeline.get_stats()
        logger.info("Final stats", extra=stats)
        print("\n=== Perception Pipeline Final Stats ===")
        import json
        print(json.dumps(stats, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
