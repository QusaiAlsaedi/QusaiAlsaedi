"""
Confidence calibration via temperature scaling.

Why calibrate?
  Modern detectors (YOLOv9, RT-DETR) are systematically overconfident.
  Without calibration, conf=0.92 may correspond to 65% empirical precision.
  Platt scaling / temperature scaling fixes this so downstream consumers
  can interpret confidence as probability.

Method: Temperature scaling (Guo et al. 2017)
  - Fit a single scalar T per class on a held-out validation set
  - Calibrated prob = sigmoid(logit(p_raw) / T)
  - Simple, numerically stable, no additional GPU overhead

Also applies contextual adjustments:
  - blur_score > 0.6 → reduce confidence by 15%
  - low_light=True and class not in thermal-friendly list → reduce by 10%
  - detection near frame edge (< 5% margin) → reduce by 5% (clipping artefact)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from .detector import Detection

logger = logging.getLogger(__name__)

# Default temperatures (T=1.0 = no calibration; T>1 = softer/more calibrated)
# These are placeholder values — replace after running calibration on your val set.
DEFAULT_TEMPERATURES: Dict[str, float] = {
    "person_standing":   1.15,
    "person_crouching":  1.20,
    "person_prone":      1.25,
    "dog_cat":           1.10,
    "large_mammal":      1.08,
    "bird_large":        1.12,
    "bird_small":        1.18,
    "car_sedan":         1.05,
    "car_suv":           1.05,
    "car_truck_light":   1.07,
    "car_truck_heavy":   1.06,
    "car_van":           1.07,
    "motorcycle":        1.12,
    "bicycle":           1.14,
    "emergency_vehicle": 1.08,
    "boat":              1.10,
    "quad_micro":        1.22,
    "quad_consumer":     1.18,
    "quad_commercial":   1.15,
    "fixed_wing_small":  1.20,
    "fixed_wing_large":  1.15,
    "hybrid_vtol":       1.18,
    "blimp_balloon":     1.10,
}


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-x))


def _logit(p: float, eps: float = 1e-6) -> float:
    p = float(np.clip(p, eps, 1.0 - eps))
    return np.log(p / (1.0 - p))


class ConfidenceCalibrator:
    """
    Apply temperature scaling + contextual adjustments to raw detections.
    Mutates detection.calibrated_conf in place.
    """

    def __init__(
        self,
        temperatures: Optional[Dict[str, float]] = None,
        calibration_file: Optional[Path] = None,
    ):
        self._T = dict(DEFAULT_TEMPERATURES)
        if temperatures:
            self._T.update(temperatures)
        if calibration_file and calibration_file.exists():
            with open(calibration_file) as f:
                self._T.update(json.load(f))
            logger.info("Loaded calibration from %s", calibration_file)

    def calibrate(
        self,
        detections: List[Detection],
        blur_score: float = 0.0,
        low_light: bool = False,
        frame_w: int = 1280,
        frame_h: int = 720,
    ) -> List[Detection]:
        """Mutates detections in place, returns same list."""
        for det in detections:
            T = self._T.get(det.class_name, 1.0)
            cal = _sigmoid(_logit(det.conf) / T)

            # Contextual penalties (multiplicative)
            if blur_score > 0.6:
                penalty = 0.85 + 0.15 * (1.0 - blur_score)   # 0.85–1.0
                cal *= penalty

            if low_light and det.supercategory not in ("vehicle",):
                cal *= 0.90

            # Near-edge penalty
            x1, y1, x2, y2 = det.bbox_xyxy
            margin_frac = 0.05
            if (x1 < frame_w * margin_frac or x2 > frame_w * (1 - margin_frac) or
                    y1 < frame_h * margin_frac or y2 > frame_h * (1 - margin_frac)):
                cal *= 0.95

            det.calibrated_conf = float(np.clip(cal, 0.0, 1.0))

        return detections

    def save(self, path: Path) -> None:
        with open(path, "w") as f:
            json.dump(self._T, f, indent=2)
        logger.info("Saved calibration to %s", path)
