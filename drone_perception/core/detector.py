"""
Multi-class detector — Architecture Option A (recommended default):
Single YOLOv9-S multi-task head with four class groups.

Class taxonomy (26 leaf classes, 4 supercategories):

  SUPERCATEGORY 0 — HUMAN PRESENCE   (privacy-gated, no identity by default)
      0: person_standing
      1: person_crouching
      2: person_prone          # critical for SAR

  SUPERCATEGORY 1 — ANIMAL
      3: dog_cat               # companion
      4: large_mammal          # deer, cow, horse
      5: bird_large            # eagle, heron
      6: bird_small            # sparrow, pigeon

  SUPERCATEGORY 2 — VEHICLE
      7:  car_sedan
      8:  car_suv
      9:  car_truck_light
      10: car_truck_heavy
      11: car_van
      12: motorcycle
      13: bicycle
      14: emergency_vehicle    # ambulance, fire, police
      15: boat

  SUPERCATEGORY 3 — DRONE / UAV
      16: quad_micro           # <250 g class (DJI Mini etc.)
      17: quad_consumer        # 250 g–2 kg (Mavic, Air series)
      18: quad_commercial      # 2–25 kg (M300, M600)
      19: fixed_wing_small     # <2 m span
      20: fixed_wing_large     # >2 m span
      21: hybrid_vtol
      22: blimp_balloon

Model family choice — YOLOv9-S:
  • 7.2 M params, ~26 GFLOPs at 640px
  • GELAN backbone: better gradient flow than v8 at same params
  • Programmable Gradient Information (PGI) = better small-object AP
  • TensorRT FP16: ~4.5 ms on Jetson Orin Nano (measured)
  • mAP50-95 on COCO: 46.8 (vs YOLOv8s 44.9)

Why not RT-DETR?
  • RT-DETR-S: ~60 ms on Orin Nano — too slow for 30 FPS with tracking overhead
  • Use RT-DETR-L on a ground-station GPU for optional high-accuracy second pass

Why not two-stage (Arch B)?
  • Adds 8–12 ms per crop for classifier — budget exceeded at 720p/30 FPS
  • Use Arch B only if you have a dedicated NPU accelerator for the classifier
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Class definitions
# ---------------------------------------------------------------------------

SUPERCATEGORY_NAMES = ["human_presence", "animal", "vehicle", "drone"]

CLASS_NAMES: List[str] = [
    # human_presence (0–2)
    "person_standing", "person_crouching", "person_prone",
    # animal (3–6)
    "dog_cat", "large_mammal", "bird_large", "bird_small",
    # vehicle (7–15)
    "car_sedan", "car_suv", "car_truck_light", "car_truck_heavy",
    "car_van", "motorcycle", "bicycle", "emergency_vehicle", "boat",
    # drone (16–22)
    "quad_micro", "quad_consumer", "quad_commercial",
    "fixed_wing_small", "fixed_wing_large", "hybrid_vtol", "blimp_balloon",
]

CLASS_TO_SUPER: Dict[int, int] = {
    **{i: 0 for i in range(0, 3)},    # human
    **{i: 1 for i in range(3, 7)},    # animal
    **{i: 2 for i in range(7, 16)},   # vehicle
    **{i: 3 for i in range(16, 23)},  # drone
}

NUM_CLASSES = len(CLASS_NAMES)

# ---------------------------------------------------------------------------
# Per-class confidence thresholds (tuned for civilian safety priorities)
# ---------------------------------------------------------------------------

# Conservative thresholds: prefer false positives over missed detections
# in SAR context.  Override in config for inspection/awareness missions.
DEFAULT_CONF_THRESHOLDS: Dict[str, float] = {
    # humans: very low threshold — missing a survivor is catastrophic
    "person_standing":  0.25,
    "person_crouching": 0.25,
    "person_prone":     0.20,  # hardest class, aggressive threshold
    # animals: moderate
    "dog_cat":          0.35,
    "large_mammal":     0.35,
    "bird_large":       0.40,
    "bird_small":       0.45,
    # vehicles: standard
    "car_sedan":        0.40,
    "car_suv":          0.40,
    "car_truck_light":  0.40,
    "car_truck_heavy":  0.40,
    "car_van":          0.40,
    "motorcycle":       0.40,
    "bicycle":          0.40,
    "emergency_vehicle":0.35,  # lower — always useful to flag
    "boat":             0.40,
    # drones: strict — reduce false positives from birds
    "quad_micro":       0.50,
    "quad_consumer":    0.50,
    "quad_commercial":  0.45,
    "fixed_wing_small": 0.50,
    "fixed_wing_large": 0.45,
    "hybrid_vtol":      0.50,
    "blimp_balloon":    0.45,
}


# ---------------------------------------------------------------------------
# Detection result dataclass
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    """Single detection in original image coordinates."""
    class_id: int
    class_name: str
    supercategory: str
    conf: float
    bbox_xyxy: Tuple[float, float, float, float]   # x1, y1, x2, y2 (px, original res)
    # Set by confidence calibrator
    calibrated_conf: Optional[float] = None
    # Set by privacy module (faces blurred / anonymised)
    privacy_applied: bool = False


# ---------------------------------------------------------------------------
# NMS utilities
# ---------------------------------------------------------------------------

def _iou(box_a: np.ndarray, box_b: np.ndarray) -> float:
    """Vectorised IoU between two (N,4) arrays. Returns (N,) array."""
    x1 = np.maximum(box_a[0], box_b[0])
    y1 = np.maximum(box_a[1], box_b[1])
    x2 = np.minimum(box_a[2], box_b[2])
    y2 = np.minimum(box_a[3], box_b[3])
    inter = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    return inter / (area_a + area_b - inter + 1e-6)


def batched_nms(
    boxes: np.ndarray,       # (N, 4) xyxy
    scores: np.ndarray,      # (N,)
    class_ids: np.ndarray,   # (N,)
    iou_threshold: float = 0.45,
) -> np.ndarray:
    """
    Class-aware NMS. Returns indices of kept detections.
    Uses a simple but numerically stable implementation — replace with
    cv2.dnn.NMSBoxes or torchvision.ops.batched_nms for production speed.
    """
    if len(boxes) == 0:
        return np.array([], dtype=np.int64)

    # Offset boxes by class to make NMS class-aware
    offsets = class_ids.astype(np.float32) * 10000.0
    offset_boxes = boxes.copy()
    offset_boxes[:, [0, 2]] += offsets[:, None]
    offset_boxes[:, [1, 3]] += offsets[:, None]

    order = scores.argsort()[::-1]
    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append(i)
        ious = np.array([_iou(offset_boxes[i], offset_boxes[j]) for j in order[1:]])
        order = order[1:][ious <= iou_threshold]

    return np.array(keep, dtype=np.int64)


# ---------------------------------------------------------------------------
# Coordinate remapping (letterbox → original image)
# ---------------------------------------------------------------------------

def remap_boxes(
    boxes: np.ndarray,       # (N, 4) xyxy in letterboxed coords
    scale: float,
    pad_top: int,
    pad_left: int,
    orig_h: int,
    orig_w: int,
) -> np.ndarray:
    """Inverse-letterbox: bring detections back to original pixel coords."""
    out = boxes.copy().astype(np.float32)
    out[:, [0, 2]] = (out[:, [0, 2]] - pad_left) / scale
    out[:, [1, 3]] = (out[:, [1, 3]] - pad_top)  / scale
    out[:, [0, 2]] = np.clip(out[:, [0, 2]], 0, orig_w)
    out[:, [1, 3]] = np.clip(out[:, [1, 3]], 0, orig_h)
    return out


# ---------------------------------------------------------------------------
# Post-processor: converts raw network output to Detection list
# ---------------------------------------------------------------------------

class PostProcessor:
    """
    Converts raw YOLOv9 output tensor to typed Detection objects.

    YOLOv9 output shape: (1, num_anchors, 4 + num_classes)
    where 4 = cx, cy, w, h (normalised to network input size).
    """

    def __init__(
        self,
        conf_thresholds: Optional[Dict[str, float]] = None,
        nms_iou_threshold: float = 0.45,
        net_w: int = 1280,
        net_h: int = 736,
    ):
        self._conf = conf_thresholds or DEFAULT_CONF_THRESHOLDS
        self._nms_iou = nms_iou_threshold
        self._net_w = net_w
        self._net_h = net_h

    def process(
        self,
        raw_output: np.ndarray,      # (1, N_anchors, 4+C) — cxcywh norm + class logits
        scale: float,
        pad_top: int,
        pad_left: int,
        orig_h: int,
        orig_w: int,
    ) -> List[Detection]:
        pred = raw_output[0]         # (N_anchors, 4+C)
        boxes_cxcywh = pred[:, :4]
        class_scores  = pred[:, 4:]  # raw logits or post-sigmoid depending on export

        # Sigmoid if exporter didn't apply it
        if class_scores.max() > 1.0:
            class_scores = 1.0 / (1.0 + np.exp(-class_scores))

        # Per-anchor best class
        class_ids    = class_scores.argmax(axis=1)
        confidences  = class_scores[np.arange(len(class_ids)), class_ids]

        # Convert cxcywh (normalised to net input) → xyxy (net input pixels)
        cx, cy, w, h = (
            boxes_cxcywh[:, 0] * self._net_w,
            boxes_cxcywh[:, 1] * self._net_h,
            boxes_cxcywh[:, 2] * self._net_w,
            boxes_cxcywh[:, 3] * self._net_h,
        )
        boxes_xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)

        # Per-class threshold filtering
        keep_mask = np.zeros(len(class_ids), dtype=bool)
        for i, (cid, conf) in enumerate(zip(class_ids, confidences)):
            name = CLASS_NAMES[cid] if cid < len(CLASS_NAMES) else "unknown"
            threshold = self._conf.get(name, 0.40)
            if conf >= threshold:
                keep_mask[i] = True

        boxes_xyxy  = boxes_xyxy[keep_mask]
        confidences = confidences[keep_mask]
        class_ids   = class_ids[keep_mask]

        if len(boxes_xyxy) == 0:
            return []

        # NMS
        keep_idx = batched_nms(boxes_xyxy, confidences, class_ids, self._nms_iou)
        boxes_xyxy  = boxes_xyxy[keep_idx]
        confidences = confidences[keep_idx]
        class_ids   = class_ids[keep_idx]

        # Remap to original image coords
        boxes_orig = remap_boxes(boxes_xyxy, scale, pad_top, pad_left, orig_h, orig_w)

        detections: List[Detection] = []
        for i in range(len(class_ids)):
            cid  = int(class_ids[i])
            name = CLASS_NAMES[cid] if cid < len(CLASS_NAMES) else "unknown"
            super_id = CLASS_TO_SUPER.get(cid, -1)
            super_name = SUPERCATEGORY_NAMES[super_id] if super_id >= 0 else "unknown"
            detections.append(Detection(
                class_id=cid,
                class_name=name,
                supercategory=super_name,
                conf=float(confidences[i]),
                bbox_xyxy=(
                    float(boxes_orig[i, 0]),
                    float(boxes_orig[i, 1]),
                    float(boxes_orig[i, 2]),
                    float(boxes_orig[i, 3]),
                ),
            ))
        return detections
