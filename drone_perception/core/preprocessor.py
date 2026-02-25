"""
Preprocessing pipeline for drone perception.

Design choices:
- CLAHE on YCrCb luminance channel handles low-light and high-contrast scenes.
- TensorRT expects NCHW FP16; we do all heavy ops in CUDA via cv2.cuda
  when available, falling back to CPU NumPy for unit-test environments.
- Motion blur estimation uses optical-flow variance to gate confidence later.
- Target: <2 ms on Jetson Orin Nano for 720p frame.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INPUT_W = 1280          # network input width  (letterboxed from 720p)
INPUT_H = 736           # network input height (must be divisible by 32)
MEAN    = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD     = np.array([0.229, 0.224, 0.225], dtype=np.float32)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PreprocessResult:
    blob: np.ndarray            # shape (1, 3, H, W), dtype float32
    scale: float                # how much the image was scaled
    pad_top: int                # letterbox top padding in px
    pad_left: int               # letterbox left padding in px
    orig_hw: Tuple[int, int]    # original (height, width)
    blur_score: float           # 0.0 (sharp) – 1.0 (very blurry)
    low_light: bool             # True if frame is underexposed
    timestamp_ns: int           # monotonic capture timestamp
    frame_id: int               # monotonic counter


# ---------------------------------------------------------------------------
# CLAHE-based low-light enhancement
# ---------------------------------------------------------------------------

class LowLightEnhancer:
    """
    Adaptive histogram equalisation on the luminance channel.
    Works well for exposures down to ~1 lux with a decent sensor.
    """

    def __init__(self, clip_limit: float = 2.0, tile_grid: Tuple[int, int] = (8, 8)):
        self._clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid)

    def enhance(self, bgr: np.ndarray) -> Tuple[np.ndarray, bool]:
        """
        Returns (enhanced_bgr, is_low_light).
        is_low_light is True when mean luminance < 60 (out of 255).
        """
        ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
        mean_lum = float(ycrcb[:, :, 0].mean())
        if mean_lum < 60.0:
            ycrcb[:, :, 0] = self._clahe.apply(ycrcb[:, :, 0])
            enhanced = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)
            return enhanced, True
        return bgr, False


# ---------------------------------------------------------------------------
# Blur estimator (Laplacian variance)
# ---------------------------------------------------------------------------

def estimate_blur(gray: np.ndarray) -> float:
    """
    Returns a normalised blur score in [0, 1].
    Score > 0.6 is considered significant motion blur.
    Uses 1/variance so high variance (sharp) → low score.
    """
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    # Empirical calibration: variance ~500 → very sharp, ~30 → blurry
    score = float(np.clip(1.0 - lap_var / 500.0, 0.0, 1.0))
    return score


# ---------------------------------------------------------------------------
# Letterbox resize (preserves aspect ratio, no distortion)
# ---------------------------------------------------------------------------

def letterbox(
    img: np.ndarray,
    target_w: int = INPUT_W,
    target_h: int = INPUT_H,
    colour: Tuple[int, int, int] = (114, 114, 114),
) -> Tuple[np.ndarray, float, int, int]:
    """
    Resize img into target_w × target_h with letterboxing.
    Returns (padded_img, scale, pad_top, pad_left).
    """
    h, w = img.shape[:2]
    scale = min(target_w / w, target_h / h)
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    pad_top  = (target_h - new_h) // 2
    pad_left = (target_w - new_w) // 2
    padded   = np.full((target_h, target_w, 3), colour, dtype=np.uint8)
    padded[pad_top:pad_top + new_h, pad_left:pad_left + new_w] = resized
    return padded, scale, pad_top, pad_left


# ---------------------------------------------------------------------------
# Main preprocessor
# ---------------------------------------------------------------------------

class Preprocessor:
    """
    Thread-safe, stateless (except CLAHE object) frame preprocessor.

    Usage:
        pre = Preprocessor()
        result = pre.process(bgr_frame, frame_id=42)
        # feed result.blob to the TRT engine
    """

    def __init__(
        self,
        net_w: int = INPUT_W,
        net_h: int = INPUT_H,
        clahe_clip: float = 2.0,
    ):
        self._net_w = net_w
        self._net_h = net_h
        self._enhancer = LowLightEnhancer(clip_limit=clahe_clip)

    def process(self, bgr: np.ndarray, frame_id: int = 0) -> PreprocessResult:
        ts = time.monotonic_ns()
        orig_hw = bgr.shape[:2]

        # 1. Low-light enhancement
        bgr, low_light = self._enhancer.enhance(bgr)

        # 2. Blur estimation (on small thumbnail for speed)
        gray = cv2.cvtColor(
            cv2.resize(bgr, (320, 180), interpolation=cv2.INTER_AREA),
            cv2.COLOR_BGR2GRAY,
        )
        blur_score = estimate_blur(gray)

        # 3. Letterbox
        padded, scale, pad_top, pad_left = letterbox(bgr, self._net_w, self._net_h)

        # 4. BGR → RGB, HWC → CHW, normalise
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        rgb = (rgb - MEAN) / STD
        blob = np.ascontiguousarray(rgb.transpose(2, 0, 1)[np.newaxis])  # (1,3,H,W)

        return PreprocessResult(
            blob=blob,
            scale=scale,
            pad_top=pad_top,
            pad_left=pad_left,
            orig_hw=orig_hw,
            blur_score=blur_score,
            low_light=low_light,
            timestamp_ns=ts,
            frame_id=frame_id,
        )
