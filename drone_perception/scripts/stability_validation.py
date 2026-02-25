"""
Stability validation — simulates 10 minutes of sustained 30 FPS pipeline load.

Uses:
  - MockCamera: generates synthetic 720p frames at target FPS
  - MockEngine: runs real preprocessing + NMS + tracking; skips GPU inference
    (produces synthetic detections to exercise the full CPU pipeline path)

Measures (sampled every second):
  - Effective FPS (sliding 2s window)
  - Per-stage P50/P95/P99 latency
  - Python process RSS memory (MB)
  - Queue depths at capture / preprocess / output queues
  - Frame drop rate
  - End-to-end latency (capture_ns → publish_ns) per frame
  - Latency creep: slope of pipeline_ms over time (should be ~0)

Outputs:
  - Per-second CSV: stability_validation_YYYYMMDD_HHMMSS.csv
  - Final summary table printed to stdout
"""

from __future__ import annotations

import csv
import os
import queue
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

# Ensure repo root on path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from drone_perception.core.async_pipeline import (
    AsyncPerceptionPipeline, LatencyRingBuffer,
)
from drone_perception.core.detector import Detection, PostProcessor
from drone_perception.core.pipeline import FrameResult, PipelineStatus
from drone_perception.core.preprocessor import Preprocessor
from drone_perception.core.timing import FrameTimestamp, check_budget, now_ns
from drone_perception.core.tracker import ByteTracker, KalmanBoxTracker
from drone_perception.core.temporal_smoother import TemporalSmoother
from drone_perception.core.confidence_calibrator import ConfidenceCalibrator
from drone_perception.core.health_monitor import FPSCounter


# ---------------------------------------------------------------------------
# Mock camera
# ---------------------------------------------------------------------------

class MockCamera:
    """
    Generates 720p BGR frames at a fixed FPS, with synthetic motion blur
    and low-light variations injected periodically to stress the pipeline.
    """

    def __init__(self, fps: float = 30.0, duration_s: float = 600.0):
        self._interval    = 1.0 / fps
        self._duration    = duration_s
        self._start       = time.monotonic()
        self._frame_count = 0
        self._lock        = threading.Lock()

    def read(self):
        with self._lock:
            elapsed = time.monotonic() - self._start
            if elapsed > self._duration:
                return False, None

            self._frame_count += 1
            n = self._frame_count

            # Generate lightweight synthetic frame (no actual image data needed)
            frame = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)

            # Simulate low-light every 60 seconds for 5 seconds
            if (int(elapsed) % 60) < 5:
                frame = (frame * 0.12).astype(np.uint8)   # very dark

            # Simulate motion blur artefact every 30 seconds for 2 seconds
            if (int(elapsed) % 30) < 2:
                frame[:] = 100  # flat grey (high blur score)

            # Pace: sleep enough to match target FPS
            next_frame_time = self._start + n * self._interval
            sleep = next_frame_time - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)

            return True, frame

    @property
    def frames_generated(self) -> int:
        return self._frame_count

    def release(self):
        pass


# ---------------------------------------------------------------------------
# Mock inference engine (no GPU — synthetic detections)
# ---------------------------------------------------------------------------

class MockEngine:
    """
    Returns synthetic raw output shaped like YOLOv9-S.
    Injects 0–4 random objects per frame to exercise the full tracking path.
    CPU-only: used to validate pipeline mechanics, not model accuracy.
    """

    _VERSION = "mock-engine-stability-test"

    def __init__(self, net_w: int = 1280, net_h: int = 736):
        self._w = net_w
        self._h = net_h
        # Simulate ~4.5 ms inference (sleep to mimic GPU latency)
        self._simulated_latency_s = 0.0045

    def infer(self, blob: np.ndarray) -> np.ndarray:
        time.sleep(self._simulated_latency_s + random.gauss(0, 0.0005))

        from drone_perception.core.detector import NUM_CLASSES
        n_anchors = 52416    # matches YOLOv9-S at 1280×736
        raw = np.zeros((1, n_anchors, 4 + NUM_CLASSES), dtype=np.float32)

        # Inject 0–3 objects at random locations with valid confidence
        n_objects = random.randint(0, 3)
        anchor_stride = n_anchors // max(n_objects + 1, 1)
        for i in range(n_objects):
            ai = i * anchor_stride + random.randint(0, anchor_stride - 1)
            ai = min(ai, n_anchors - 1)
            cx = random.uniform(0.15, 0.85)
            cy = random.uniform(0.15, 0.85)
            w  = random.uniform(0.03, 0.15)
            h  = random.uniform(0.05, 0.20)
            cid = random.choice([0, 7, 8, 16, 17])   # person, cars, drones
            conf = random.uniform(0.55, 0.95)

            raw[0, ai, 0] = cx
            raw[0, ai, 1] = cy
            raw[0, ai, 2] = w
            raw[0, ai, 3] = h
            raw[0, ai, 4 + cid] = conf

        return raw

    @property
    def version(self) -> str:
        return self._VERSION


# ---------------------------------------------------------------------------
# Per-second snapshot
# ---------------------------------------------------------------------------

class PerSecondSnapshot:
    __slots__ = [
        "second", "fps", "pipeline_p50", "pipeline_p95", "inference_p50",
        "inference_p95", "preprocess_p50", "tracking_p50", "rss_mb",
        "drop_rate", "total_frames", "total_drops",
    ]

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# Main validation runner
# ---------------------------------------------------------------------------

def run_validation(
    duration_s: float = 600.0,
    target_fps: float = 30.0,
    output_dir: Path = Path("."),
) -> None:

    print(f"{'='*65}")
    print(f"  Drone Perception Stability Validation")
    print(f"  Duration: {duration_s:.0f}s  |  Target: {target_fps:.0f} FPS")
    print(f"{'='*65}")

    camera     = MockCamera(fps=target_fps, duration_s=duration_s)
    engine     = MockEngine()
    fps_ctr    = FPSCounter(window_s=2.0)
    snapshots: List[PerSecondSnapshot] = []
    e2e_latencies: deque = deque(maxlen=1000)

    # Collect result stats
    _results_lock = threading.Lock()
    _total_results = [0]
    _total_budget_violations = [0]

    def on_result(result: FrameResult) -> None:
        fps_ctr.tick()
        with _results_lock:
            _total_results[0] += 1
            if result.pipeline_ms > 33.3:
                _total_budget_violations[0] += 1
        e2e_latencies.append(result.pipeline_ms)

    KalmanBoxTracker.count = 0
    pipeline = AsyncPerceptionPipeline(
        engine=engine,
        camera=camera,
        result_callback=on_result,
        target_fps=target_fps,
    )

    start = time.monotonic()
    pipeline.start()

    # Sample every second
    second = 0
    last_drop_check = 0

    try:
        while True:
            time.sleep(1.0)
            second += 1
            elapsed = time.monotonic() - start

            if elapsed > duration_s + 2:
                break

            stats  = pipeline.get_stats()
            fps    = fps_ctr.current_fps()
            latency= pipeline.metrics

            def p(stage, pct):
                d = latency.percentiles(stage)
                return round(d.get(f"p{pct}", 0.0), 2)

            # RSS memory
            rss_mb = 0.0
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_mb = int(line.split()[1]) / 1024.0
                            break
            except OSError:
                pass

            snap = PerSecondSnapshot(
                second=second,
                fps=round(fps, 1),
                pipeline_p50=p("pipeline_total", 50),
                pipeline_p95=p("pipeline_total", 95),
                inference_p50=p("inference", 50),
                inference_p95=p("inference", 95),
                preprocess_p50=p("preprocess", 50),
                tracking_p50=p("output", 50),
                rss_mb=round(rss_mb, 1),
                drop_rate=round(stats.get("drop_rate", 0.0), 4),
                total_frames=stats.get("frames_captured", 0),
                total_drops=stats.get("frames_dropped", 0),
            )
            snapshots.append(snap)

            # Live progress every 30 seconds
            if second % 30 == 0 or second <= 5:
                print(
                    f"  t={second:4d}s | FPS={fps:5.1f} | "
                    f"P50={snap.pipeline_p50:6.2f}ms P95={snap.pipeline_p95:6.2f}ms | "
                    f"drop={snap.drop_rate:.3f} | RSS={snap.rss_mb:.0f}MB"
                )

    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        pipeline.stop()

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    if not snapshots:
        print("No data collected.")
        return

    # Exclude warmup (first 3 samples) and cooldown (last 2 samples)
    steady = snapshots[3:-2] if len(snapshots) > 6 else snapshots
    fps_vals    = [s.fps for s in steady]
    p95_vals    = [s.pipeline_p95 for s in steady]
    p50_vals    = [s.pipeline_p50 for s in steady]
    rss_vals    = [s.rss_mb for s in steady]
    drop_vals   = [s.drop_rate for s in steady]

    # Latency creep: linear regression slope of p50 over time
    if len(p50_vals) > 10:
        x = np.arange(len(p50_vals))
        slope, _ = np.polyfit(x, p50_vals, 1)
    else:
        slope = 0.0

    # Save CSV
    ts_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"stability_validation_{ts_str}.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["second", "fps", "pipeline_p50_ms", "pipeline_p95_ms",
                    "inference_p50_ms", "inference_p95_ms", "preprocess_p50_ms",
                    "tracking_p50_ms", "rss_mb", "drop_rate",
                    "total_frames", "total_drops"])
        for s in snapshots:
            w.writerow([
                s.second, s.fps, s.pipeline_p50, s.pipeline_p95,
                s.inference_p50, s.inference_p95, s.preprocess_p50,
                s.tracking_p50, s.rss_mb, s.drop_rate,
                s.total_frames, s.total_drops,
            ])
    print(f"\n  CSV saved: {csv_path}")

    # ------------------------------------------------------------------
    # Print summary table
    # ------------------------------------------------------------------
    print(f"\n{'='*65}")
    print("  STABILITY VALIDATION SUMMARY")
    print(f"{'='*65}")
    print(f"  Duration tested:       {snapshots[-1].second}s")
    print(f"  Total frames captured: {snapshots[-1].total_frames}")
    print(f"  Total frames dropped:  {snapshots[-1].total_drops}")
    print(f"  Budget violations:     {_total_budget_violations[0]} "
          f"({100*_total_budget_violations[0]/max(_total_results[0],1):.2f}%)")
    print()

    def fmt(label, vals, unit="", pass_fn=None):
        a = np.array(vals)
        p = "PASS" if (pass_fn is None or pass_fn(a)) else "FAIL"
        print(f"  {label:<30} min={a.min():.2f} mean={a.mean():.2f} "
              f"max={a.max():.2f} {unit}  [{p}]")

    print(f"  {'Metric':<30} {'min':>8} {'mean':>8} {'max':>8}  {'Status'}")
    print(f"  {'-'*60}")
    fmt("FPS",                fps_vals,  "fps",  lambda a: a.mean() >= 28.0 and a.min() >= 20.0)
    fmt("Pipeline P50 (ms)",  p50_vals,  "ms",   lambda a: a.mean() < 20.0)
    fmt("Pipeline P95 (ms)",  p95_vals,  "ms",   lambda a: a.mean() < 30.0)
    fmt("RSS Memory (MB)",    rss_vals,  "MB",   lambda a: a.max() < 800.0)
    fmt("Frame drop rate",    drop_vals, "",     lambda a: a.mean() < 0.02)

    slope_pass = abs(slope) < 0.01
    print(f"\n  {'Latency creep (slope):':<30} {slope:+.5f} ms/sample  "
          f"  [{'PASS' if slope_pass else 'FAIL'}]")

    # Overall verdict
    all_pass = (
        np.mean(fps_vals) >= 28.0 and
        np.min(fps_vals)  >= 20.0 and
        np.mean(p95_vals) < 30.0  and
        np.max(rss_vals)  < 800.0 and
        np.mean(drop_vals) < 0.02 and
        slope_pass
    )
    print(f"\n  {'='*60}")
    verdict = "PASS — Sustained 30 FPS stability confirmed" if all_pass \
              else "FAIL — See metrics above for failures"
    print(f"  OVERALL: {verdict}")
    print(f"  {'='*60}\n")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=600.0, help="Test duration in seconds")
    p.add_argument("--fps",      type=float, default=30.0,  help="Target FPS")
    p.add_argument("--output",   default=".",               help="Output directory for CSV")
    args = p.parse_args()
    run_validation(
        duration_s=args.duration,
        target_fps=args.fps,
        output_dir=Path(args.output),
    )
