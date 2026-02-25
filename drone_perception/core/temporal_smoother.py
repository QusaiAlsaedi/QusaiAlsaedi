"""
Temporal smoothing for class labels and confidence scores.

Problem: single-frame detectors flicker class labels under motion blur,
lighting changes, and partial occlusion. E.g. car_sedan → car_suv → car_sedan
over consecutive frames for the same vehicle.

Solution: exponential moving average (EMA) on per-class confidence scores
per track_id. The reported class is the argmax of the smoothed distribution.

Additionally handles "class stabilisation":
  Once a class has been consistently reported for N frames (hysteresis),
  it is "locked in" and requires K consecutive contrary frames to switch.
  This prevents spurious class switches but allows legitimate re-classification.

Privacy note: temporal smoothing is applied identically to all classes.
No identity inference is performed beyond what the detector already outputs.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

import numpy as np

from .detector import CLASS_NAMES, NUM_CLASSES
from .tracker import Track


class ClassSmootherState:
    """Per-track smoothing state."""

    def __init__(self, alpha: float = 0.4, lock_frames: int = 5, unlock_frames: int = 8):
        self.alpha = alpha
        self.lock_frames = lock_frames
        self.unlock_frames = unlock_frames

        self.ema_scores = np.zeros(NUM_CLASSES, dtype=np.float32)
        self.frame_count = 0
        self.locked_class: Optional[int] = None
        self.lock_count = 0
        self.contrary_count = 0

    def update(self, class_id: int, conf: float) -> Tuple[int, float]:
        """
        Update EMA with new observation.
        Returns (smoothed_class_id, smoothed_confidence).
        """
        # One-hot encode new observation
        obs = np.zeros(NUM_CLASSES, dtype=np.float32)
        obs[class_id] = conf
        self.frame_count += 1

        if self.frame_count == 1:
            self.ema_scores = obs.copy()
        else:
            self.ema_scores = self.alpha * obs + (1 - self.alpha) * self.ema_scores

        best_class = int(self.ema_scores.argmax())
        best_conf  = float(self.ema_scores[best_class])

        # Hysteresis lock
        if self.locked_class is None:
            self.lock_count += 1 if best_class == class_id else 0
            if self.lock_count >= self.lock_frames:
                self.locked_class = best_class
                self.contrary_count = 0
        else:
            if best_class != self.locked_class:
                self.contrary_count += 1
                if self.contrary_count >= self.unlock_frames:
                    self.locked_class = best_class
                    self.lock_count = self.unlock_frames
                    self.contrary_count = 0
            else:
                self.contrary_count = 0

        out_class = self.locked_class if self.locked_class is not None else best_class
        return out_class, best_conf


class TemporalSmoother:
    """
    Applies per-track class smoothing to a list of Track objects.
    Mutates Track.detection.class_id and class_name in place.
    """

    def __init__(
        self,
        alpha: float = 0.4,
        lock_frames: int = 5,
        unlock_frames: int = 8,
        max_stale_frames: int = 60,
    ):
        self._alpha = alpha
        self._lock = lock_frames
        self._unlock = unlock_frames
        self._max_stale = max_stale_frames
        self._states: Dict[int, ClassSmootherState] = {}
        self._last_seen: Dict[int, int] = {}
        self._frame_counter = 0

    def update(self, tracks: List[Track]) -> List[Track]:
        self._frame_counter += 1

        for track in tracks:
            tid = track.track_id
            self._last_seen[tid] = self._frame_counter

            if tid not in self._states:
                self._states[tid] = ClassSmootherState(
                    alpha=self._alpha,
                    lock_frames=self._lock,
                    unlock_frames=self._unlock,
                )

            state = self._states[tid]
            smooth_class, smooth_conf = state.update(
                track.detection.class_id,
                track.detection.conf,
            )

            # Mutate in place
            track.detection.class_id   = smooth_class
            track.detection.class_name = (
                CLASS_NAMES[smooth_class] if smooth_class < len(CLASS_NAMES) else "unknown"
            )
            # Update conf to smoothed value
            if track.detection.calibrated_conf is not None:
                track.detection.calibrated_conf = smooth_conf
            else:
                track.detection.conf = smooth_conf

        # Garbage-collect stale states
        stale = [
            tid for tid, last in self._last_seen.items()
            if self._frame_counter - last > self._max_stale
        ]
        for tid in stale:
            del self._states[tid]
            del self._last_seen[tid]

        return tracks
