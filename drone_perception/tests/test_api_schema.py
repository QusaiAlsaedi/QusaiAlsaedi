"""
Unit tests for JSON API schema and serialisation.
"""

import json
import time

import pytest

from ..api.schema import (
    BoundingBox, DetectionOutput, PerceptionFrame,
    TrackOutput, frame_result_to_output, SCHEMA_VERSION,
)
from ..core.detector import Detection
from ..core.pipeline import FrameResult, PipelineStatus
from ..core.tracker import Track


def make_mock_track(track_id: int = 1) -> Track:
    det = Detection(
        class_id=7, class_name="car_sedan",
        supercategory="vehicle", conf=0.82,
        calibrated_conf=0.77,
        bbox_xyxy=(100.0, 200.0, 300.0, 400.0),
    )
    return Track(
        track_id=track_id,
        detection=det,
        predicted_bbox=(102.0, 202.0, 302.0, 402.0),
        age=10,
        hits=8,
        time_since_update=0,
        is_confirmed=True,
    )


def make_mock_result() -> FrameResult:
    return FrameResult(
        frame_id=42,
        timestamp_ns=time.monotonic_ns(),
        tracks=[make_mock_track(1), make_mock_track(2)],
        raw_detections=[],
        status=PipelineStatus.OK,
        inference_ms=4.5,
        pipeline_ms=14.2,
        blur_score=0.1,
        low_light=False,
        model_version="yolov9s-drone-test",
    )


class TestBoundingBox:

    def test_width_height(self):
        bb = BoundingBox(100, 200, 300, 400)
        assert bb.width  == 200
        assert bb.height == 200

    def test_centroid(self):
        bb = BoundingBox(100, 200, 300, 400)
        assert bb.cx == 200
        assert bb.cy == 300

    def test_to_dict_has_required_keys(self):
        bb = BoundingBox(10, 20, 110, 120)
        d = bb.to_dict()
        for key in ("x1", "y1", "x2", "y2", "width", "height", "cx", "cy"):
            assert key in d


class TestPerceptionFrame:

    def test_to_json_is_valid_json(self):
        result = make_mock_result()
        frame = frame_result_to_output(result)
        raw = frame.to_json()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    def test_schema_version_present(self):
        result = make_mock_result()
        frame = frame_result_to_output(result)
        d = frame.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION

    def test_frame_id_propagated(self):
        result = make_mock_result()
        frame = frame_result_to_output(result)
        assert frame.frame_id == 42

    def test_summary_counts_correct(self):
        result = make_mock_result()
        frame = frame_result_to_output(result)
        s = frame.summary
        assert s["total_tracks"] == 2
        assert s["by_supercategory"].get("vehicle") == 2

    def test_all_tracks_have_track_id(self):
        result = make_mock_result()
        frame = frame_result_to_output(result)
        for t in frame.tracks:
            assert t.track_id > 0

    def test_no_raw_pixels_in_output(self):
        """Verify no numpy arrays slip into the JSON output."""
        result = make_mock_result()
        frame = frame_result_to_output(result)
        raw = frame.to_json()
        # Should not contain numpy array representations
        assert "dtype" not in raw
        assert "ndarray" not in raw

    def test_status_is_string(self):
        result = make_mock_result()
        frame = frame_result_to_output(result)
        d = frame.to_dict()
        assert isinstance(d["status"], str)

    def test_model_version_present(self):
        result = make_mock_result()
        frame = frame_result_to_output(result)
        d = frame.to_dict()
        assert "model_version" in d
        assert len(d["model_version"]) > 0
