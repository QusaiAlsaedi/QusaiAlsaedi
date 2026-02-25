"""
Unit tests for detector post-processing (NMS, remapping, PostProcessor).
Uses synthetic TRT-like output — no GPU required.
"""

import numpy as np
import pytest

from ..core.detector import (
    CLASS_NAMES, CLASS_TO_SUPER, NUM_CLASSES, SUPERCATEGORY_NAMES,
    Detection, PostProcessor, batched_nms, remap_boxes,
)


class TestTaxonomy:

    def test_class_count(self):
        assert NUM_CLASSES == 23

    def test_all_classes_have_supercategory(self):
        for i in range(NUM_CLASSES):
            assert i in CLASS_TO_SUPER, f"class {i} missing supercategory"

    def test_supercategory_names_valid(self):
        for super_id in CLASS_TO_SUPER.values():
            assert 0 <= super_id < len(SUPERCATEGORY_NAMES)


class TestBatchedNMS:

    def test_removes_duplicates(self):
        boxes = np.array([
            [10, 10, 100, 100],
            [12, 12, 102, 102],   # high IoU with first
            [200, 200, 300, 300],  # different region
        ], dtype=np.float32)
        scores = np.array([0.9, 0.8, 0.7])
        class_ids = np.array([0, 0, 0])
        kept = batched_nms(boxes, scores, class_ids, iou_threshold=0.45)
        assert len(kept) == 2   # keeps box 0 (higher score) and box 2

    def test_keeps_different_classes(self):
        boxes = np.array([
            [10, 10, 100, 100],
            [10, 10, 100, 100],   # same box, different class
        ], dtype=np.float32)
        scores = np.array([0.9, 0.8])
        class_ids = np.array([0, 7])   # human vs vehicle
        kept = batched_nms(boxes, scores, class_ids, iou_threshold=0.45)
        assert len(kept) == 2   # both kept (class-aware NMS)

    def test_empty_input(self):
        kept = batched_nms(
            np.empty((0, 4), dtype=np.float32),
            np.array([]),
            np.array([]),
        )
        assert len(kept) == 0

    def test_single_box(self):
        boxes = np.array([[0, 0, 100, 100]], dtype=np.float32)
        kept = batched_nms(boxes, np.array([0.9]), np.array([0]), iou_threshold=0.45)
        assert len(kept) == 1


class TestRemapBoxes:

    def test_identity_transform(self):
        """scale=1, pad=0 → boxes unchanged."""
        boxes = np.array([[100, 50, 200, 150]], dtype=np.float32)
        out = remap_boxes(boxes, scale=1.0, pad_top=0, pad_left=0,
                          orig_h=720, orig_w=1280)
        np.testing.assert_allclose(out, boxes, atol=1e-3)

    def test_scale_down(self):
        """scale=0.5, no pad → coords doubled."""
        boxes = np.array([[50, 25, 100, 75]], dtype=np.float32)
        out = remap_boxes(boxes, scale=0.5, pad_top=0, pad_left=0,
                          orig_h=720, orig_w=1280)
        np.testing.assert_allclose(out, [[100, 50, 200, 150]], atol=1e-3)

    def test_clamps_to_image_bounds(self):
        boxes = np.array([[-10, -10, 2000, 2000]], dtype=np.float32)
        out = remap_boxes(boxes, scale=1.0, pad_top=0, pad_left=0,
                          orig_h=720, orig_w=1280)
        assert out[0, 0] >= 0
        assert out[0, 1] >= 0
        assert out[0, 2] <= 1280
        assert out[0, 3] <= 720


class TestPostProcessor:

    def _make_raw_output(self, cx, cy, w, h, class_id, conf, net_w=1280, net_h=736):
        """Construct minimal synthetic TRT output for a single object."""
        num_anchors = 5
        raw = np.zeros((1, num_anchors, 4 + NUM_CLASSES), dtype=np.float32)
        raw[0, 0, 0] = cx / net_w
        raw[0, 0, 1] = cy / net_h
        raw[0, 0, 2] = w  / net_w
        raw[0, 0, 3] = h  / net_h
        # Set class scores as post-sigmoid probabilities
        raw[0, 0, 4 + class_id] = conf
        return raw

    def test_single_detection(self):
        pp = PostProcessor(net_w=1280, net_h=736)
        raw = self._make_raw_output(
            cx=640, cy=368, w=200, h=300, class_id=0, conf=0.9
        )
        dets = pp.process(raw, scale=1.0, pad_top=0, pad_left=0,
                          orig_h=736, orig_w=1280)
        assert len(dets) >= 1
        assert dets[0].class_name == CLASS_NAMES[0]
        assert dets[0].conf > 0.8

    def test_below_threshold_filtered(self):
        pp = PostProcessor(net_w=1280, net_h=736)
        raw = self._make_raw_output(
            cx=640, cy=368, w=200, h=300, class_id=0, conf=0.10  # below threshold
        )
        dets = pp.process(raw, scale=1.0, pad_top=0, pad_left=0,
                          orig_h=736, orig_w=1280)
        # Should be empty (threshold for person_standing is 0.25)
        human_dets = [d for d in dets if d.supercategory == "human_presence"]
        assert len(human_dets) == 0

    def test_bbox_in_valid_range(self):
        pp = PostProcessor(net_w=1280, net_h=736)
        raw = self._make_raw_output(
            cx=640, cy=368, w=100, h=200, class_id=7, conf=0.85
        )
        dets = pp.process(raw, scale=1.0, pad_top=0, pad_left=0,
                          orig_h=736, orig_w=1280)
        for det in dets:
            x1, y1, x2, y2 = det.bbox_xyxy
            assert x1 >= 0 and y1 >= 0
            assert x2 <= 1280 and y2 <= 736
            assert x1 < x2 and y1 < y2


class TestPrivacySafety:
    """
    SAFETY INVARIANT: no weaponisation features.
    These tests will fail if anyone adds targeting/firing data to outputs.
    """

    def test_no_targeting_in_detection(self):
        det = Detection(
            class_id=0, class_name="person_standing",
            supercategory="human_presence", conf=0.9,
            bbox_xyxy=(100, 100, 200, 300),
        )
        d = det.__dict__
        forbidden = ["target", "fire", "intercept", "weapon", "trajectory", "aim"]
        for f in forbidden:
            assert not any(f in str(k).lower() for k in d.keys()), \
                f"Forbidden field '{f}' found in Detection"

    def test_no_targeting_in_class_names(self):
        forbidden = ["target", "hostile", "combatant", "weapon"]
        for name in CLASS_NAMES:
            for f in forbidden:
                assert f not in name.lower(), \
                    f"Forbidden term '{f}' in class name '{name}'"
