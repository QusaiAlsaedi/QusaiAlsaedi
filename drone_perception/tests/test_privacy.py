"""
Privacy and safety invariant tests.

CRITICAL: These tests must NEVER be removed or weakened.
They enforce the safety contract that this system cannot be used
as a targeting or weaponisation tool.
"""

import time

import numpy as np
import pytest

from ..core.detector import CLASS_NAMES, Detection
from ..privacy.privacy_module import OperatorToken, PrivacyModule, _blur_face_region


class TestOperatorToken:

    def test_valid_token(self):
        now = time.time()
        tok = OperatorToken(
            operator_id="op1",
            consent_scope="sar",
            face_id_authorised=False,
            issued_at=now - 10,
            expires_at=now + 3600,
        )
        assert tok.is_valid()

    def test_expired_token(self):
        now = time.time()
        tok = OperatorToken(
            operator_id="op1",
            consent_scope="sar",
            face_id_authorised=False,
            issued_at=now - 7200,
            expires_at=now - 1,   # expired 1 second ago
        )
        assert not tok.is_valid()

    def test_anonymous_token_has_no_face_id(self):
        tok = OperatorToken.anonymous()
        assert tok.face_id_authorised is False

    def test_expired_token_blocks_output(self):
        now = time.time()
        tok = OperatorToken(
            operator_id="op1",
            consent_scope="test",
            face_id_authorised=False,
            issued_at=now - 10,
            expires_at=now - 1,
        )
        module = PrivacyModule(token=tok)
        dets = [Detection(
            class_id=0, class_name="person_standing",
            supercategory="human_presence", conf=0.9,
            bbox_xyxy=(100, 100, 200, 300),
        )]
        result = module.apply(dets, bgr=None)
        assert len(result) == 0, "Expired token must block all outputs"


class TestFaceBlurring:

    def test_blur_modifies_face_region(self):
        bgr = np.full((720, 1280, 3), 128, dtype=np.uint8)
        # Checkerboard pattern in face region — pixelation will change it
        face_region = bgr[100:300, 100:200]
        face_region[::4, ::4] = 0
        face_region[2::4, 2::4] = 255
        original_face = bgr[100:300, 100:200].copy()

        blurred = _blur_face_region(bgr, (100, 100, 200, 300), method="pixelate")
        # The face region should be modified by pixelation
        assert not np.array_equal(blurred[100:300, 100:200], original_face)

    def test_blur_does_not_modify_outside_face(self):
        bgr = np.full((720, 1280, 3), 128, dtype=np.uint8)
        blurred = _blur_face_region(bgr, (100, 100, 200, 200), method="pixelate")
        # Region outside bbox should be unchanged
        np.testing.assert_array_equal(blurred[0:99, :], bgr[0:99, :])

    def test_out_of_bounds_bbox_handled(self):
        bgr = np.zeros((100, 100, 3), dtype=np.uint8)
        # Bbox extends beyond image — should not raise
        result = _blur_face_region(bgr, (-10, -10, 200, 200), method="pixelate")
        assert result.shape == bgr.shape

    def test_privacy_applied_flag_set(self):
        module = PrivacyModule()
        det = Detection(
            class_id=0, class_name="person_standing",
            supercategory="human_presence", conf=0.9,
            bbox_xyxy=(100, 100, 200, 300),
        )
        bgr = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = module.apply([det], bgr=bgr)
        assert result[0].privacy_applied is True


class TestNoTargeting:
    """
    SAFETY INVARIANT TESTS — DO NOT MODIFY OR DELETE.

    These tests verify that no targeting, weaponisation, or actuation
    data can be produced by this system.
    """

    _FORBIDDEN_TERMS = [
        "target", "fire", "shoot", "weapon", "intercept",
        "trajectory_intercept", "firing_solution", "aim",
        "hostile", "combatant", "threat_vector", "engagement",
        "kill", "neutralise", "neutralize",
    ]

    def test_class_names_contain_no_targeting_terms(self):
        for name in CLASS_NAMES:
            for term in self._FORBIDDEN_TERMS:
                assert term not in name.lower(), (
                    f"SAFETY VIOLATION: class name '{name}' contains "
                    f"forbidden term '{term}'"
                )

    def test_detection_has_no_targeting_fields(self):
        det = Detection(
            class_id=0, class_name="person_standing",
            supercategory="human_presence", conf=0.9,
            bbox_xyxy=(0, 0, 100, 100),
        )
        fields = det.__dict__
        for term in self._FORBIDDEN_TERMS:
            assert not any(term in str(k).lower() for k in fields.keys()), (
                f"SAFETY VIOLATION: Detection has field matching forbidden term '{term}'"
            )

    def test_privacy_module_has_no_targeting_methods(self):
        module = PrivacyModule()
        method_names = [m for m in dir(module) if not m.startswith("__")]
        for term in self._FORBIDDEN_TERMS:
            assert not any(term in m.lower() for m in method_names), (
                f"SAFETY VIOLATION: PrivacyModule has method matching '{term}'"
            )

    def test_output_schema_has_no_actuation_fields(self):
        from ..api.schema import DetectionOutput, TrackOutput, PerceptionFrame
        for cls in (DetectionOutput, TrackOutput, PerceptionFrame):
            fields = cls.__dataclass_fields__.keys()
            for term in self._FORBIDDEN_TERMS:
                assert not any(term in f.lower() for f in fields), (
                    f"SAFETY VIOLATION: {cls.__name__} has field matching '{term}'"
                )
