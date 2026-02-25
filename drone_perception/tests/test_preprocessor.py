"""
Unit tests for Preprocessor.

Run: pytest drone_perception/tests/test_preprocessor.py -v
"""

import numpy as np
import pytest

from ..core.preprocessor import (
    INPUT_H, INPUT_W, LowLightEnhancer, Preprocessor, estimate_blur, letterbox
)


class TestLetterbox:

    def test_output_shape(self):
        img = np.zeros((720, 1280, 3), dtype=np.uint8)
        out, scale, pt, pl = letterbox(img, INPUT_W, INPUT_H)
        assert out.shape == (INPUT_H, INPUT_W, 3)

    def test_scale_is_one_for_exact_aspect(self):
        img = np.zeros((INPUT_H, INPUT_W, 3), dtype=np.uint8)
        _, scale, _, _ = letterbox(img, INPUT_W, INPUT_H)
        assert abs(scale - 1.0) < 1e-4

    def test_no_distortion_square_image(self):
        img = np.zeros((640, 640, 3), dtype=np.uint8)
        out, scale, pt, pl = letterbox(img, INPUT_W, INPUT_H)
        assert out.shape == (INPUT_H, INPUT_W, 3)
        assert scale > 0

    def test_pad_fills_correct_colour(self):
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        colour = (114, 114, 114)
        out, _, pt, pl = letterbox(img, INPUT_W, INPUT_H, colour=colour)
        # Top padding should be the letterbox colour
        if pt > 0:
            np.testing.assert_array_equal(out[0, :], colour)


class TestBlurEstimator:

    def test_sharp_image_low_score(self):
        sharp = np.zeros((180, 320), dtype=np.uint8)
        # Checkerboard pattern = high Laplacian variance
        sharp[::2, ::2] = 255
        score = estimate_blur(sharp)
        assert score < 0.5, f"Sharp checkerboard should have low blur score, got {score}"

    def test_flat_image_high_score(self):
        flat = np.full((180, 320), 128, dtype=np.uint8)
        score = estimate_blur(flat)
        assert score > 0.9, f"Flat image should have high blur score, got {score}"

    def test_score_range(self):
        for _ in range(10):
            img = np.random.randint(0, 255, (180, 320), dtype=np.uint8)
            score = estimate_blur(img)
            assert 0.0 <= score <= 1.0


class TestLowLightEnhancer:

    def test_dark_image_is_enhanced(self):
        dark = np.full((720, 1280, 3), 30, dtype=np.uint8)
        enhancer = LowLightEnhancer()
        enhanced, is_low = enhancer.enhance(dark)
        assert is_low is True
        assert enhanced.mean() > dark.mean()

    def test_bright_image_unchanged(self):
        bright = np.full((720, 1280, 3), 180, dtype=np.uint8)
        enhancer = LowLightEnhancer()
        enhanced, is_low = enhancer.enhance(bright)
        assert is_low is False

    def test_output_shape_preserved(self):
        img = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)
        enhancer = LowLightEnhancer()
        enhanced, _ = enhancer.enhance(img)
        assert enhanced.shape == img.shape


class TestPreprocessor:

    def test_blob_shape(self):
        pre = Preprocessor()
        frame = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)
        result = pre.process(frame, frame_id=1)
        assert result.blob.shape == (1, 3, INPUT_H, INPUT_W)

    def test_blob_dtype_float32(self):
        pre = Preprocessor()
        frame = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)
        result = pre.process(frame, frame_id=1)
        assert result.blob.dtype == np.float32

    def test_frame_id_propagated(self):
        pre = Preprocessor()
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = pre.process(frame, frame_id=42)
        assert result.frame_id == 42

    def test_timestamp_is_set(self):
        import time
        pre = Preprocessor()
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        t_before = time.monotonic_ns()
        result = pre.process(frame, frame_id=1)
        t_after  = time.monotonic_ns()
        assert t_before <= result.timestamp_ns <= t_after

    def test_blur_score_range(self):
        pre = Preprocessor()
        for _ in range(5):
            frame = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)
            result = pre.process(frame)
            assert 0.0 <= result.blur_score <= 1.0

    def test_orig_hw_correct(self):
        pre = Preprocessor()
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = pre.process(frame)
        assert result.orig_hw == (720, 1280)

    @pytest.mark.parametrize("h,w", [(480, 640), (1080, 1920), (360, 640)])
    def test_various_input_sizes(self, h, w):
        pre = Preprocessor()
        frame = np.random.randint(0, 255, (h, w, 3), dtype=np.uint8)
        result = pre.process(frame)
        assert result.blob.shape == (1, 3, INPUT_H, INPUT_W)
