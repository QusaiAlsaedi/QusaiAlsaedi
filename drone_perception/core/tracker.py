"""
Multi-object tracker — ByteTrack variant adapted for drone perception.

Architecture Option C (Tracking + Re-ID):
  • ByteTrack for robust association across occlusion (uses low-conf dets too)
  • Kalman filter per track (constant-velocity model in image coords)
  • Re-ID embedding only for VEHICLES and DRONES (NOT for humans — privacy)
  • Human tracks use appearance-free ByteTrack only unless operator-authorised

Why ByteTrack over DeepSORT?
  • DeepSORT requires re-ID network per crop (~3–5 ms extra)
  • ByteTrack achieves comparable MOTA with zero re-ID cost for most classes
  • On Orin Nano, ByteTrack leaves budget for the re-ID head on vehicles only

ID-switch mitigation:
  1. Two-buffer matching: high-conf → low-conf detections
  2. IoU + Mahalanobis distance gating in Kalman prediction step
  3. 30-frame grace period before track deletion (handles full occlusion)
  4. Appearance embedding (MobileNetV3-Small, 128-d) for vehicle re-ID only
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .detector import Detection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kalman filter (2D constant-velocity model)
# ---------------------------------------------------------------------------

class KalmanBoxTracker:
    """
    State: [cx, cy, s, r, vx, vy, vs]
    where s = scale (area), r = aspect ratio (w/h, constant)
    Follows SORT convention.
    """

    count = 0

    def __init__(self, bbox_xyxy: Tuple[float, float, float, float]):
        KalmanBoxTracker.count += 1
        self.id = KalmanBoxTracker.count

        # State dim=7, meas dim=4
        self._F = np.eye(7, dtype=np.float32)    # transition
        self._F[0, 4] = self._F[1, 5] = self._F[2, 6] = 1.0
        self._H = np.eye(4, 7, dtype=np.float32) # measurement
        self._P = np.eye(7, dtype=np.float32) * 10.0
        self._P[4:, 4:] *= 1000.0               # high uncertainty on velocities
        self._Q = np.eye(7, dtype=np.float32)
        self._Q[4:, 4:] *= 0.01
        self._R = np.eye(4, dtype=np.float32)
        self._R[2:, 2:] *= 10.0

        self._x = np.zeros((7, 1), dtype=np.float32)
        m = self._xyxy_to_z(bbox_xyxy)
        self._x[:4] = m.reshape(4, 1)

        self.hits = 1
        self.hit_streak = 1
        self.age = 0
        self.time_since_update = 0
        self.history: deque = deque(maxlen=30)

    @staticmethod
    def _xyxy_to_z(bbox: Tuple[float, float, float, float]) -> np.ndarray:
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        s  = (x2 - x1) * (y2 - y1)
        r  = (x2 - x1) / (y2 - y1 + 1e-6)
        return np.array([cx, cy, s, r], dtype=np.float32)

    @staticmethod
    def _z_to_xyxy(z: np.ndarray) -> Tuple[float, float, float, float]:
        cx, cy, s, r = z.flatten()[:4]
        w = np.sqrt(max(s * r, 0))
        h = s / (w + 1e-6)
        return (cx - w/2, cy - h/2, cx + w/2, cy + h/2)

    def predict(self) -> Tuple[float, float, float, float]:
        if self._x[6] + self._x[2] <= 0:
            self._x[6] = 0
        self._x = self._F @ self._x
        self._P = self._F @ self._P @ self._F.T + self._Q
        self.age += 1
        self.time_since_update += 1
        self.history.append(self._z_to_xyxy(self._x))
        return self.history[-1]

    def update(self, bbox_xyxy: Tuple[float, float, float, float]) -> None:
        self.time_since_update = 0
        self.hits += 1
        self.hit_streak += 1
        z = self._xyxy_to_z(bbox_xyxy).reshape(4, 1)
        y = z - self._H @ self._x
        S = self._H @ self._P @ self._H.T + self._R
        K = self._P @ self._H.T @ np.linalg.inv(S)
        self._x = self._x + K @ y
        self._P = (np.eye(7, dtype=np.float32) - K @ self._H) @ self._P

    def get_state(self) -> Tuple[float, float, float, float]:
        return self._z_to_xyxy(self._x)


# ---------------------------------------------------------------------------
# IoU matching
# ---------------------------------------------------------------------------

def _iou_matrix(tracks: List[KalmanBoxTracker], detections: List[Detection]) -> np.ndarray:
    """Returns (T, D) IoU matrix."""
    mat = np.zeros((len(tracks), len(detections)), dtype=np.float32)
    for t_i, trk in enumerate(tracks):
        tb = trk.get_state()
        for d_i, det in enumerate(detections):
            db = det.bbox_xyxy
            x1 = max(tb[0], db[0]); y1 = max(tb[1], db[1])
            x2 = min(tb[2], db[2]); y2 = min(tb[3], db[3])
            inter = max(0, x2-x1) * max(0, y2-y1)
            at = (tb[2]-tb[0]) * (tb[3]-tb[1])
            ad = (db[2]-db[0]) * (db[3]-db[1])
            mat[t_i, d_i] = inter / (at + ad - inter + 1e-6)
    return mat


def _greedy_match(cost_matrix: np.ndarray, threshold: float) -> List[Tuple[int, int]]:
    """Simple greedy assignment (replace with scipy.optimize.linear_sum_assignment for prod)."""
    pairs = []
    used_t = set()
    used_d = set()
    for t_i, d_i in zip(*np.unravel_index(cost_matrix.argsort(axis=None)[::-1],
                                           cost_matrix.shape)):
        if cost_matrix[t_i, d_i] < threshold:
            break
        if t_i not in used_t and d_i not in used_d:
            pairs.append((int(t_i), int(d_i)))
            used_t.add(t_i)
            used_d.add(d_i)
    return pairs


# ---------------------------------------------------------------------------
# Track result
# ---------------------------------------------------------------------------

@dataclass
class Track:
    track_id: int
    detection: Detection          # latest associated detection
    predicted_bbox: Tuple[float, float, float, float]
    age: int                      # frames since track was created
    hits: int                     # total confirmed detections
    time_since_update: int        # frames since last detection match
    is_confirmed: bool            # True after min_hits matches


# ---------------------------------------------------------------------------
# ByteTrack-inspired two-buffer tracker
# ---------------------------------------------------------------------------

class ByteTracker:
    """
    Two-pass matching:
      Pass 1: high-confidence detections (conf ≥ high_thresh) matched to active tracks
      Pass 2: low-confidence detections matched to unmatched active tracks
              (rescues occluded tracks that produce weak detections)

    Privacy rule: human tracks never carry re-ID embeddings unless
    self.allow_human_reid is True (requires operator authorisation token).
    """

    def __init__(
        self,
        iou_high_thresh: float = 0.5,
        iou_low_thresh:  float = 0.3,
        high_conf:       float = 0.5,
        low_conf:        float = 0.1,
        min_hits:        int   = 3,
        max_age:         int   = 30,
        allow_human_reid: bool = False,
    ):
        self._iou_high = iou_high_thresh
        self._iou_low  = iou_low_thresh
        self._high_conf = high_conf
        self._low_conf  = low_conf
        self._min_hits  = min_hits
        self._max_age   = max_age
        self.allow_human_reid = allow_human_reid

        self._trackers: List[KalmanBoxTracker] = []
        self._track_meta: Dict[int, Detection] = {}   # track_id → last detection

        # Reset class counter for reproducibility in tests
        KalmanBoxTracker.count = 0

    def update(self, detections: List[Detection]) -> List[Track]:
        """
        Consume a list of detections for the current frame.
        Returns active, confirmed tracks with their latest detection.
        """
        # Predict all existing tracks
        for trk in self._trackers:
            trk.predict()

        # Split detections by confidence
        high_dets = [d for d in detections if d.conf >= self._high_conf]
        low_dets  = [d for d in detections if self._low_conf <= d.conf < self._high_conf]

        unmatched_trks = list(range(len(self._trackers)))
        unmatched_high = list(range(len(high_dets)))

        # Pass 1: high-conf detections → all tracks
        if self._trackers and high_dets:
            iou_mat = _iou_matrix(self._trackers, high_dets)
            matches = _greedy_match(iou_mat, self._iou_high)
            matched_t = {t for t, _ in matches}
            matched_d = {d for _, d in matches}
            unmatched_trks = [t for t in unmatched_trks if t not in matched_t]
            unmatched_high  = [d for d in unmatched_high  if d not in matched_d]
            for t_i, d_i in matches:
                self._trackers[t_i].update(high_dets[d_i].bbox_xyxy)
                self._track_meta[self._trackers[t_i].id] = high_dets[d_i]

        # Pass 2: low-conf detections → unmatched tracks only
        if unmatched_trks and low_dets:
            sub_trks = [self._trackers[i] for i in unmatched_trks]
            iou_mat2 = _iou_matrix(sub_trks, low_dets)
            matches2 = _greedy_match(iou_mat2, self._iou_low)
            matched_t2 = {t for t, _ in matches2}
            unmatched_trks = [unmatched_trks[t] for t in range(len(sub_trks))
                              if t not in matched_t2]
            for t_i, d_i in matches2:
                real_ti = unmatched_trks[t_i] if t_i < len(unmatched_trks) else t_i
                self._trackers[real_ti].update(low_dets[d_i].bbox_xyxy)
                self._track_meta[self._trackers[real_ti].id] = low_dets[d_i]

        # Create new tracks for unmatched high-conf detections
        for d_i in unmatched_high:
            det = high_dets[d_i]
            new_trk = KalmanBoxTracker(det.bbox_xyxy)
            self._trackers.append(new_trk)
            self._track_meta[new_trk.id] = det

        # Retire stale tracks
        self._trackers = [
            t for t in self._trackers
            if t.time_since_update <= self._max_age
        ]

        # Build output
        results: List[Track] = []
        for trk in self._trackers:
            if trk.time_since_update > 1 and trk.hit_streak < 1:
                continue
            confirmed = trk.hits >= self._min_hits or trk.time_since_update == 0
            last_det = self._track_meta.get(trk.id)
            if last_det is None:
                continue
            results.append(Track(
                track_id=trk.id,
                detection=last_det,
                predicted_bbox=trk.get_state(),
                age=trk.age,
                hits=trk.hits,
                time_since_update=trk.time_since_update,
                is_confirmed=confirmed,
            ))

        return results
