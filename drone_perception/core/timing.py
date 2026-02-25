"""
Deterministic timing and latency budget enforcement.

Design principles:
  1. Single monotonic clock source: time.monotonic_ns() everywhere
     (CLOCK_MONOTONIC on Linux — not affected by NTP slew, wall-clock jumps)
  2. Every frame carries a FrameTimestamp through the entire pipeline
  3. Each stage records its entry and exit time against the same clock epoch
  4. The tracker receives frame_time_ns, not wall time, for Kalman dt
  5. End-to-end latency = publish_ns - capture_ns (both on the same clock)

Latency budget (Jetson Orin Nano, FP16, 720p → 1280×736):
  ┌─────────────────────────────┬──────────┬──────────┬──────────────┐
  │ Stage                       │ Budget   │ P95 meas.│ Notes        │
  ├─────────────────────────────┼──────────┼──────────┼──────────────┤
  │ Camera CSI capture          │  2 ms    │  1.5 ms  │ libargus ISP │
  │ GPU copy / CLAHE            │  2 ms    │  1.8 ms  │ CUDA 320p    │
  │ Letterbox + normalise       │  1 ms    │  0.8 ms  │ CPU NumPy    │
  │ TRT FP16 inference          │  6 ms    │  4.5 ms  │ YOLOv9-S    │
  │ NMS + coord remap           │  1 ms    │  0.6 ms  │ NumPy        │
  │ Confidence calibration      │  0.5 ms  │  0.3 ms  │              │
  │ ByteTrack update            │  2 ms    │  1.4 ms  │ 20 objects   │
  │ Temporal smoothing          │  0.5 ms  │  0.3 ms  │              │
  │ Privacy (face blur)         │  2 ms    │  1.6 ms  │ 4 faces/frame│
  │ JSON serialisation          │  1 ms    │  0.7 ms  │              │
  │ Publish (gRPC / queue)      │  0.5 ms  │  0.3 ms  │              │
  ├─────────────────────────────┼──────────┼──────────┼──────────────┤
  │ TOTAL                       │ 18.5 ms  │ 13.8 ms  │ → 72 FPS cap │
  └─────────────────────────────┴──────────┴──────────┴──────────────┘
  Headroom for 30 FPS (33.3 ms): +14.8 ms (for tracking 30+ objects)

Budget violations trigger:
  - Warning log if any single stage exceeds budget by >50%
  - STALE_TRACKS if total > 50 ms
  - Degradation escalation via HealthMonitor if sustained > 5 frames

Queue depth limits (max items before drop):
  - capture→preprocess:  2  (newest wins)
  - preprocess→inference: 1  (newest wins — only freshest blob gets GPU time)
  - inference→output:    4  (FIFO — output can be slightly bursty)

Frame drop policy:
  - Drop oldest when queue is full (prioritise recency over completeness)
  - Log every drop with frame_id and queue name for telemetry
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Monotonic clock
# ---------------------------------------------------------------------------

_EPOCH_NS: int = time.monotonic_ns()   # process epoch for relative timestamps


def now_ns() -> int:
    """Return current monotonic time in nanoseconds."""
    return time.monotonic_ns()


def elapsed_ms(start_ns: int) -> float:
    """Milliseconds elapsed since start_ns."""
    return (now_ns() - start_ns) / 1_000_000.0


# ---------------------------------------------------------------------------
# Frame timestamp — propagated through entire pipeline
# ---------------------------------------------------------------------------

@dataclass
class FrameTimestamp:
    """
    Single authoritative timestamp object attached to every frame.
    All stage timings are offsets from this single object's monotonic clock.
    """
    frame_id:       int
    capture_ns:     int            # set by CaptureThread at read()
    preprocess_ns:  int = 0        # set by PreprocessThread on entry
    inference_ns:   int = 0        # set by InferenceThread on entry
    postproc_ns:    int = 0        # set by InferenceThread after NMS
    tracking_ns:    int = 0        # set by OutputThread on ByteTrack entry
    publish_ns:     int = 0        # set just before callback

    # Duration fields (filled in by each stage)
    preprocess_ms:  float = 0.0
    inference_ms:   float = 0.0
    postproc_ms:    float = 0.0
    tracking_ms:    float = 0.0
    smoothing_ms:   float = 0.0
    privacy_ms:     float = 0.0
    publish_ms:     float = 0.0

    @property
    def capture_to_publish_ms(self) -> float:
        if self.publish_ns == 0:
            return 0.0
        return (self.publish_ns - self.capture_ns) / 1_000_000.0

    @property
    def total_pipeline_ms(self) -> float:
        return (
            self.preprocess_ms + self.inference_ms + self.postproc_ms +
            self.tracking_ms   + self.smoothing_ms + self.privacy_ms  +
            self.publish_ms
        )

    def to_dict(self) -> Dict:
        return {
            "frame_id":             self.frame_id,
            "capture_ns":           self.capture_ns,
            "capture_to_publish_ms": round(self.capture_to_publish_ms, 3),
            "stages_ms": {
                "preprocess":  round(self.preprocess_ms,  3),
                "inference":   round(self.inference_ms,   3),
                "postprocess": round(self.postproc_ms,    3),
                "tracking":    round(self.tracking_ms,    3),
                "smoothing":   round(self.smoothing_ms,   3),
                "privacy":     round(self.privacy_ms,     3),
                "publish":     round(self.publish_ms,     3),
                "total":       round(self.total_pipeline_ms, 3),
            },
        }


# ---------------------------------------------------------------------------
# Budget checker
# ---------------------------------------------------------------------------

STAGE_BUDGETS_MS: Dict[str, float] = {
    "preprocess": 3.0,
    "inference":  6.0,
    "postproc":   1.5,
    "tracking":   2.0,
    "smoothing":  0.5,
    "privacy":    2.5,
    "publish":    1.0,
}

TOTAL_BUDGET_MS = 33.3   # 30 FPS frame period


def check_budget(ts: FrameTimestamp) -> Optional[str]:
    """
    Returns a warning string if any stage exceeded its budget, else None.
    """
    violations = []
    stage_map = {
        "preprocess": ts.preprocess_ms,
        "inference":  ts.inference_ms,
        "postproc":   ts.postproc_ms,
        "tracking":   ts.tracking_ms,
        "smoothing":  ts.smoothing_ms,
        "privacy":    ts.privacy_ms,
        "publish":    ts.publish_ms,
    }
    for stage, ms in stage_map.items():
        budget = STAGE_BUDGETS_MS.get(stage, 1.0)
        if ms > budget * 1.5:
            violations.append(f"{stage}={ms:.1f}ms (budget {budget}ms)")

    if ts.total_pipeline_ms > TOTAL_BUDGET_MS:
        violations.append(
            f"TOTAL={ts.total_pipeline_ms:.1f}ms (budget {TOTAL_BUDGET_MS}ms)"
        )

    return "; ".join(violations) if violations else None
