"""
Production health monitor for the drone perception pipeline.

Responsibilities:
  1. Per-stage health checks (alive + latency within budget)
  2. FPS monitor with alert callback when effective FPS < threshold
  3. Memory usage logging (RSS + GPU memory via nvidia-smi / tegrastats)
  4. Graceful degradation: reduce input resolution when overloaded
  5. Structured JSON logging to rotating files and stdout
  6. TensorRT engine warmup routine (prevents first-frame latency spike)

Degradation ladder (triggered by consecutive FPS-alert events):
  Level 0 (normal):    1280×736,  full pipeline
  Level 1 (degraded):  960×544,   reduce preprocess queue timeout
  Level 2 (stressed):  640×384,   skip temporal smoother
  Level 3 (critical):  480×256,   skip tracking (detections only)

Each level is held for at least HOLD_FRAMES before escalating further,
and recovery back to Level 0 requires RECOVERY_FRAMES of sustained good FPS.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON structured logger
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """Emit every log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts":      self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f"),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
            "func":    f"{record.filename}:{record.lineno}",
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_structured_logging(
    log_dir: Optional[Path] = None,
    level: int = logging.INFO,
) -> None:
    """
    Configure root logger with JSON formatter.
    Writes to stdout AND a rotating file if log_dir is provided.
    """
    import logging.handlers

    root = logging.getLogger()
    root.setLevel(level)
    fmt  = JsonFormatter()

    # Stdout handler
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # File handler
    if log_dir:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "perception.jsonl",
            maxBytes=20 * 1024 * 1024,  # 20 MB
            backupCount=5,
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)


# ---------------------------------------------------------------------------
# Degradation levels
# ---------------------------------------------------------------------------

class DegradationLevel(IntEnum):
    NORMAL   = 0   # 1280×736
    DEGRADED = 1   # 960×544
    STRESSED = 2   # 640×384
    CRITICAL = 3   # 480×256


DEGRADATION_RESOLUTIONS = {
    DegradationLevel.NORMAL:   (1280, 736),
    DegradationLevel.DEGRADED: (960,  544),
    DegradationLevel.STRESSED: (640,  384),
    DegradationLevel.CRITICAL: (480,  256),
}

HOLD_FRAMES     = 30   # minimum frames before escalating
RECOVERY_FRAMES = 90   # frames of good FPS before de-escalating


# ---------------------------------------------------------------------------
# Memory snapshot
# ---------------------------------------------------------------------------

@dataclass
class MemorySnapshot:
    timestamp: float
    rss_mb: float
    gpu_used_mb: float
    gpu_total_mb: float
    swap_mb: float


def _get_memory_snapshot() -> MemorySnapshot:
    """Read process RSS from /proc/self/status and GPU from nvidia-smi."""
    rss_mb = 0.0
    swap_mb = 0.0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) / 1024.0
                elif line.startswith("VmSwap:"):
                    swap_mb = int(line.split()[1]) / 1024.0
    except OSError:
        pass

    gpu_used  = 0.0
    gpu_total = 0.0

    # Try tegrastats first (Jetson), then nvidia-smi (desktop/server)
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=0.5, stderr=subprocess.DEVNULL,
        ).decode().strip().split(",")
        gpu_used  = float(out[0])
        gpu_total = float(out[1])
    except Exception:
        pass

    return MemorySnapshot(
        timestamp=time.monotonic(),
        rss_mb=rss_mb,
        gpu_used_mb=gpu_used,
        gpu_total_mb=gpu_total,
        swap_mb=swap_mb,
    )


# ---------------------------------------------------------------------------
# Per-stage health state
# ---------------------------------------------------------------------------

@dataclass
class StageHealth:
    name: str
    is_alive: bool = True
    last_heartbeat: float = field(default_factory=time.monotonic)
    latency_p95_ms: float = 0.0
    budget_ms: float = 10.0
    over_budget: bool = False

    def update_heartbeat(self) -> None:
        self.last_heartbeat = time.monotonic()

    def staleness_s(self) -> float:
        return time.monotonic() - self.last_heartbeat


# ---------------------------------------------------------------------------
# FPS counter
# ---------------------------------------------------------------------------

class FPSCounter:
    """Sliding-window FPS estimate over the last N seconds."""

    def __init__(self, window_s: float = 2.0):
        self._window = window_s
        self._timestamps: deque[float] = deque()

    def tick(self) -> float:
        now = time.monotonic()
        self._timestamps.append(now)
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        return len(self._timestamps) / self._window

    def current_fps(self) -> float:
        now = time.monotonic()
        cutoff = now - self._window
        count = sum(1 for t in self._timestamps if t >= cutoff)
        return count / self._window


# ---------------------------------------------------------------------------
# TRT Warmup
# ---------------------------------------------------------------------------

def warmup_engine(engine, net_w: int = 1280, net_h: int = 736, n_runs: int = 10) -> float:
    """
    Run N_RUNS forward passes with a zero tensor to warm up the TRT engine.
    Returns mean inference latency over the last 5 runs (warmup excluded).

    Reason: TRT JIT-compiles CUDA kernels on the first few passes.
    Without warmup, frame 1 latency is 5–10× higher than steady-state.
    """
    dummy = np.zeros((1, 3, net_h, net_w), dtype=np.float32)
    latencies = []
    logger.info("Warming up TRT engine (%d runs)...", n_runs)
    for i in range(n_runs):
        t0 = time.perf_counter()
        engine.infer(dummy)
        ms = (time.perf_counter() - t0) * 1000
        latencies.append(ms)
        logger.debug("Warmup run %d: %.2f ms", i + 1, ms)

    steady_ms = float(np.mean(latencies[-5:]))
    logger.info(
        "Warmup complete. Steady-state latency: %.2f ms (target <6 ms FP16)",
        steady_ms,
    )
    return steady_ms


# ---------------------------------------------------------------------------
# Health Monitor
# ---------------------------------------------------------------------------

class HealthMonitor(threading.Thread):
    """
    Background thread that:
      - Polls stage heartbeats every CHECK_INTERVAL_S
      - Computes effective FPS from a shared FPSCounter
      - Logs memory snapshots every MEMORY_LOG_INTERVAL_S
      - Fires alert callbacks on FPS drop / stage stall / OOM
      - Manages DegradationLevel and notifies pipeline via callback
    """

    CHECK_INTERVAL_S      = 1.0
    MEMORY_LOG_INTERVAL_S = 5.0
    STAGE_STALL_THRESHOLD_S = 3.0   # stage considered dead if no heartbeat for this long
    FPS_ALERT_THRESHOLD   = 25.0
    GPU_MEM_ALERT_FRACTION = 0.90   # alert when > 90% GPU RAM used

    def __init__(
        self,
        fps_counter: FPSCounter,
        stage_healths: Dict[str, StageHealth],
        on_fps_alert: Optional[Callable[[float], None]] = None,
        on_stage_dead: Optional[Callable[[str], None]] = None,
        on_degradation: Optional[Callable[[DegradationLevel], None]] = None,
        on_memory_alert: Optional[Callable[[MemorySnapshot], None]] = None,
    ):
        super().__init__(name="HealthMonitor", daemon=True)
        self._fps         = fps_counter
        self._stages      = stage_healths
        self._on_fps      = on_fps_alert
        self._on_dead     = on_stage_dead
        self._on_degrade  = on_degradation
        self._on_mem      = on_memory_alert

        self._stop_evt    = threading.Event()
        self._level       = DegradationLevel.NORMAL
        self._bad_frames  = 0
        self._good_frames = 0
        self._last_mem_log = 0.0

    # ------------------------------------------------------------------

    def run(self) -> None:
        logger.info("HealthMonitor started")
        while not self._stop_evt.is_set():
            self._stop_evt.wait(self.CHECK_INTERVAL_S)
            if self._stop_evt.is_set():
                break

            self._check_fps()
            self._check_stages()
            self._check_memory()

    def _check_fps(self) -> None:
        fps = self._fps.current_fps()
        if fps < self.FPS_ALERT_THRESHOLD and fps > 0:
            self._bad_frames += 1
            self._good_frames = 0
            logger.warning(
                "FPS below threshold",
                extra={"fps": fps, "threshold": self.FPS_ALERT_THRESHOLD,
                       "degradation_level": self._level.name},
            )
            if self._on_fps:
                self._on_fps(fps)
            self._try_escalate()
        elif fps >= self.FPS_ALERT_THRESHOLD:
            self._good_frames += 1
            self._bad_frames = 0
            self._try_recover()

    def _try_escalate(self) -> None:
        if (self._bad_frames >= HOLD_FRAMES and
                self._level < DegradationLevel.CRITICAL):
            new_level = DegradationLevel(self._level + 1)
            self._level = new_level
            self._bad_frames = 0
            logger.warning(
                "Degradation escalated",
                extra={"level": new_level.name,
                       "resolution": DEGRADATION_RESOLUTIONS[new_level]},
            )
            if self._on_degrade:
                self._on_degrade(new_level)

    def _try_recover(self) -> None:
        if (self._good_frames >= RECOVERY_FRAMES and
                self._level > DegradationLevel.NORMAL):
            new_level = DegradationLevel(self._level - 1)
            self._level = new_level
            self._good_frames = 0
            logger.info(
                "Degradation recovered",
                extra={"level": new_level.name,
                       "resolution": DEGRADATION_RESOLUTIONS[new_level]},
            )
            if self._on_degrade:
                self._on_degrade(new_level)

    def _check_stages(self) -> None:
        for name, health in self._stages.items():
            staleness = health.staleness_s()
            if staleness > self.STAGE_STALL_THRESHOLD_S and health.is_alive:
                health.is_alive = False
                logger.error(
                    "Stage stalled",
                    extra={"stage": name, "staleness_s": round(staleness, 2)},
                )
                if self._on_dead:
                    self._on_dead(name)
            health.over_budget = health.latency_p95_ms > health.budget_ms

    def _check_memory(self) -> None:
        now = time.monotonic()
        if now - self._last_mem_log < self.MEMORY_LOG_INTERVAL_S:
            return
        self._last_mem_log = now

        snap = _get_memory_snapshot()
        logger.info(
            "Memory snapshot",
            extra={
                "rss_mb":       round(snap.rss_mb, 1),
                "gpu_used_mb":  round(snap.gpu_used_mb, 1),
                "gpu_total_mb": round(snap.gpu_total_mb, 1),
                "swap_mb":      round(snap.swap_mb, 1),
            },
        )

        if (snap.gpu_total_mb > 0 and
                snap.gpu_used_mb / snap.gpu_total_mb > self.GPU_MEM_ALERT_FRACTION):
            logger.error(
                "GPU memory critical",
                extra={"used_mb": snap.gpu_used_mb, "total_mb": snap.gpu_total_mb},
            )
            if self._on_mem:
                self._on_mem(snap)

    # ------------------------------------------------------------------

    def current_level(self) -> DegradationLevel:
        return self._level

    def current_resolution(self) -> tuple:
        return DEGRADATION_RESOLUTIONS[self._level]

    def stop(self) -> None:
        self._stop_evt.set()

    def report(self) -> Dict:
        return {
            "degradation_level": self._level.name,
            "resolution":        DEGRADATION_RESOLUTIONS[self._level],
            "bad_frames":        self._bad_frames,
            "good_frames":       self._good_frames,
            "stages": {
                name: {
                    "alive":          h.is_alive,
                    "staleness_s":    round(h.staleness_s(), 2),
                    "latency_p95_ms": round(h.latency_p95_ms, 2),
                    "over_budget":    h.over_budget,
                }
                for name, h in self._stages.items()
            },
        }
