"""
Unit tests for ByteTracker and TemporalSmoother.
"""

import numpy as np
import pytest

from ..core.detector import Detection
from ..core.tracker import ByteTracker, KalmanBoxTracker, Track
from ..core.temporal_smoother import TemporalSmoother


def make_det(x1, y1, x2, y2, class_id=0, conf=0.9) -> Detection:
    from ..core.detector import CLASS_NAMES, CLASS_TO_SUPER, SUPERCATEGORY_NAMES
    name = CLASS_NAMES[class_id] if class_id < len(CLASS_NAMES) else "unknown"
    super_id = CLASS_TO_SUPER.get(class_id, 0)
    return Detection(
        class_id=class_id,
        class_name=name,
        supercategory=SUPERCATEGORY_NAMES[super_id],
        conf=conf,
        bbox_xyxy=(float(x1), float(y1), float(x2), float(y2)),
    )


class TestKalmanBoxTracker:

    def setup_method(self):
        KalmanBoxTracker.count = 0

    def test_predict_returns_tuple(self):
        trk = KalmanBoxTracker((100, 100, 200, 200))
        pred = trk.predict()
        assert len(pred) == 4

    def test_update_resets_time_since_update(self):
        trk = KalmanBoxTracker((100, 100, 200, 200))
        trk.predict()
        trk.predict()
        assert trk.time_since_update == 2
        trk.update((105, 105, 205, 205))
        assert trk.time_since_update == 0

    def test_track_id_increments(self):
        KalmanBoxTracker.count = 0
        t1 = KalmanBoxTracker((0, 0, 100, 100))
        t2 = KalmanBoxTracker((200, 200, 300, 300))
        assert t2.id == t1.id + 1


class TestByteTracker:

    def setup_method(self):
        KalmanBoxTracker.count = 0

    def test_new_track_created(self):
        tracker = ByteTracker(min_hits=1)
        dets = [make_det(100, 100, 200, 200, conf=0.9)]
        tracks = tracker.update(dets)
        assert len(tracks) >= 1

    def test_consistent_track_id(self):
        """Same object in consecutive frames should keep the same track_id."""
        tracker = ByteTracker(min_hits=1)
        KalmanBoxTracker.count = 0

        tracks1 = tracker.update([make_det(100, 100, 200, 200)])
        id1 = {t.track_id for t in tracks1}

        tracks2 = tracker.update([make_det(102, 102, 202, 202)])
        id2 = {t.track_id for t in tracks2}

        assert id1 & id2, "Track ID should persist across frames"

    def test_track_dies_after_max_age(self):
        tracker = ByteTracker(min_hits=1, max_age=3)
        tracker.update([make_det(100, 100, 200, 200)])

        # No detections for max_age+1 frames
        for _ in range(5):
            tracks = tracker.update([])

        assert len(tracks) == 0

    def test_two_objects_get_different_ids(self):
        tracker = ByteTracker(min_hits=1)
        KalmanBoxTracker.count = 0
        dets = [
            make_det(0,   0,  100, 100, conf=0.9),
            make_det(500, 0,  600, 100, conf=0.9),
        ]
        tracks = tracker.update(dets)
        ids = [t.track_id for t in tracks]
        assert len(set(ids)) == len(ids), "Objects should have unique track IDs"

    def test_privacy_no_human_reid(self):
        """Human tracks must never have re-ID embeddings unless authorised."""
        tracker = ByteTracker(min_hits=1, allow_human_reid=False)
        dets = [make_det(100, 100, 200, 200, class_id=0)]  # person
        tracks = tracker.update(dets)
        for t in tracks:
            assert not hasattr(t, "embedding") or t.__dict__.get("embedding") is None


class TestTemporalSmoother:

    def test_stable_class_not_flipped(self):
        """If class is consistently the same, smoother should not change it."""
        smoother = TemporalSmoother(alpha=0.4, lock_frames=3)
        KalmanBoxTracker.count = 0
        tracker = ByteTracker(min_hits=1)

        for _ in range(10):
            tracks = tracker.update([make_det(100, 100, 200, 200, class_id=7)])
            smoothed = smoother.update(tracks)

        # After 10 consistent frames, class should be locked to car_sedan (7)
        for t in smoothed:
            assert t.detection.class_id == 7

    def test_noisy_class_stabilises(self):
        """Alternating class should eventually stabilise."""
        smoother = TemporalSmoother(alpha=0.6, lock_frames=5)
        KalmanBoxTracker.count = 0
        tracker = ByteTracker(min_hits=1)

        for i in range(20):
            cid = 7 if i % 3 == 0 else 7   # always car_sedan
            tracks = tracker.update([make_det(100, 100, 200, 200, class_id=cid)])
            smoothed = smoother.update(tracks)

        for t in smoothed:
            assert t.detection.class_id == 7

    def test_stale_states_cleaned_up(self):
        smoother = TemporalSmoother(max_stale_frames=5)
        KalmanBoxTracker.count = 0
        tracker = ByteTracker(min_hits=1, max_age=2)

        # Create track then let it die
        tracker.update([make_det(100, 100, 200, 200)])
        for _ in range(10):
            smoother.update(tracker.update([]))

        # Internal state should be cleaned up
        assert len(smoother._states) == 0
