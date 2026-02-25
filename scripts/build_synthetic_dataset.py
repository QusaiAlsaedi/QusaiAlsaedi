#!/usr/bin/env python3
"""
Build a synthetic simulation dataset for end-to-end pipeline smoke-testing.

Creates:
  data/sim/images/val/sim_NNNN.png    — synthetic aerial-look PNG images
  data/sim/annotations/sim_val.json   — COCO-format ground-truth annotations

Usage:
  python scripts/build_synthetic_dataset.py --num-images 20

Then run evaluation:
  python -m drone_perception.scripts.evaluate \\
    --onnx    weights/yolov9s_drone_sim.onnx \\
    --dataset data/sim/annotations/sim_val.json \\
    --images  data/sim/images/val \\
    --max-images 20
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# 23-class taxonomy  (must stay in sync with drone_perception/core/detector.py)
# ---------------------------------------------------------------------------

CLASS_NAMES: List[str] = [
    # human_presence (0-2)
    "person_standing", "person_crouching", "person_prone",
    # animal (3-6)
    "dog_cat", "large_mammal", "bird_large", "bird_small",
    # vehicle (7-15)
    "car_sedan", "car_suv", "car_truck_light", "car_truck_heavy",
    "car_van", "motorcycle", "bicycle", "emergency_vehicle", "boat",
    # drone (16-22)
    "quad_micro", "quad_consumer", "quad_commercial",
    "fixed_wing_small", "fixed_wing_large", "hybrid_vtol", "blimp_balloon",
]
NUM_CLASSES = len(CLASS_NAMES)   # 23

# Supercategory for each class (matches detector.py)
SUPERCATEGORY = (
    ["human_presence"] * 3 +
    ["animal"]         * 4 +
    ["vehicle"]        * 9 +
    ["drone"]          * 7
)

# Sampling probability weights — roughly reflects a realistic SAR/surveillance scene
CLASS_WEIGHTS: List[float] = [
    4.0, 2.0, 1.5,          # human
    1.0, 0.5, 0.5, 0.5,     # animal
    5.0, 4.0, 2.0, 1.0,     # vehicle (cars dominate)
    2.0, 1.5, 1.5, 1.0, 0.5,
    2.0, 2.0, 1.0,          # drone
    0.5, 0.5, 0.5, 0.3,
]

# Object size as fraction of image (w_min, w_max, h_min, h_max)
# Modelled on an aerial drone at ~50-150 m altitude
CLASS_SIZE_FRACS: Dict[int, Tuple[float, float, float, float]] = {
    0:  (0.010, 0.028, 0.030, 0.070),  # person_standing — narrow, tall
    1:  (0.010, 0.025, 0.022, 0.055),  # person_crouching
    2:  (0.030, 0.070, 0.008, 0.020),  # person_prone — wide, flat
    3:  (0.015, 0.035, 0.015, 0.035),  # dog_cat
    4:  (0.040, 0.090, 0.040, 0.090),  # large_mammal
    5:  (0.020, 0.050, 0.020, 0.050),  # bird_large
    6:  (0.008, 0.018, 0.008, 0.018),  # bird_small
    7:  (0.060, 0.120, 0.028, 0.055),  # car_sedan
    8:  (0.065, 0.130, 0.038, 0.070),  # car_suv
    9:  (0.080, 0.160, 0.035, 0.060),  # car_truck_light
    10: (0.120, 0.220, 0.040, 0.065),  # car_truck_heavy
    11: (0.065, 0.125, 0.032, 0.060),  # car_van
    12: (0.018, 0.045, 0.018, 0.040),  # motorcycle
    13: (0.015, 0.038, 0.020, 0.045),  # bicycle
    14: (0.080, 0.160, 0.040, 0.075),  # emergency_vehicle
    15: (0.080, 0.220, 0.045, 0.110),  # boat
    16: (0.010, 0.025, 0.010, 0.025),  # quad_micro
    17: (0.020, 0.055, 0.020, 0.055),  # quad_consumer
    18: (0.035, 0.080, 0.035, 0.080),  # quad_commercial
    19: (0.050, 0.110, 0.018, 0.038),  # fixed_wing_small
    20: (0.090, 0.180, 0.028, 0.055),  # fixed_wing_large
    21: (0.065, 0.130, 0.045, 0.090),  # hybrid_vtol
    22: (0.110, 0.260, 0.055, 0.130),  # blimp_balloon
}

# BGR colours per supercategory (for rectangle fill)
SUPER_COLORS: Dict[str, Tuple[int, int, int]] = {
    "human_presence": (220, 80,  80),   # blue
    "animal":         (60,  180, 60),   # green
    "vehicle":        (60,  60,  210),  # red
    "drone":          (40,  210, 210),  # yellow
}


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def _terrain_background(h: int, w: int, rng: random.Random) -> np.ndarray:
    """Synthetic aerial terrain: noisy gradient in earth tones."""
    base_hue = rng.randint(30, 90)       # green-to-brown band
    base_val = rng.randint(60, 140)
    bg = np.full((h, w, 3), base_val, dtype=np.float32)
    # Horizontal luminance gradient
    grad = np.linspace(0.85, 1.15, w, dtype=np.float32)
    bg *= grad[np.newaxis, :, np.newaxis]
    # Add terrain noise
    noise = np.random.normal(0, 12, (h, w, 3)).astype(np.float32)
    bg = np.clip(bg + noise, 0, 255).astype(np.uint8)
    # Slight green tint on channel 1
    bg[:, :, 1] = np.clip(bg[:, :, 1].astype(int) + 20, 0, 255).astype(np.uint8)
    return bg


def _place_object(
    img: np.ndarray,
    class_id: int,
    rng: random.Random,
    existing: List[Tuple[int, int, int, int]],
) -> Tuple[int, int, int, int] | None:
    """
    Place a coloured rectangle on `img` representing one object.
    Returns (x, y, w, h) in pixel coords, or None if no valid position found.
    Skips placement if it heavily overlaps an existing box (>50% IoU).
    """
    H, W = img.shape[:2]
    wf_min, wf_max, hf_min, hf_max = CLASS_SIZE_FRACS[class_id]

    obj_w = int(rng.uniform(wf_min, wf_max) * W)
    obj_h = int(rng.uniform(hf_min, hf_max) * H)
    obj_w = max(obj_w, 4)
    obj_h = max(obj_h, 4)

    for _ in range(20):  # up to 20 placement attempts
        x = rng.randint(0, max(0, W - obj_w - 1))
        y = rng.randint(0, max(0, H - obj_h - 1))

        # Check overlap with existing boxes
        overlap_ok = True
        for (ex, ey, ew, eh) in existing:
            ix1 = max(x, ex);  iy1 = max(y, ey)
            ix2 = min(x + obj_w, ex + ew); iy2 = min(y + obj_h, ey + eh)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
            area_a = obj_w * obj_h
            if inter > 0.5 * area_a:
                overlap_ok = False
                break

        if overlap_ok:
            color = SUPER_COLORS[SUPERCATEGORY[class_id]]
            # Semi-transparent fill to blend with terrain
            overlay = img.copy()
            cv2.rectangle(overlay, (x, y), (x + obj_w, y + obj_h), color, -1)
            alpha = rng.uniform(0.55, 0.80)
            cv2.addWeighted(overlay, alpha, img, 1 - alpha, 0, img)
            # Thin border for visibility
            cv2.rectangle(img, (x, y), (x + obj_w, y + obj_h),
                          tuple(max(0, c - 60) for c in color), 1)
            return (x, y, obj_w, obj_h)

    return None  # could not place without excessive overlap


# ---------------------------------------------------------------------------
# Dataset builder
# ---------------------------------------------------------------------------

def build_dataset(
    out_img_dir: Path,
    out_ann_file: Path,
    num_images: int,
    img_w: int,
    img_h: int,
    seed: int,
) -> None:
    rng = random.Random(seed)
    np.random.seed(seed)

    out_img_dir.mkdir(parents=True, exist_ok=True)
    out_ann_file.parent.mkdir(parents=True, exist_ok=True)

    # COCO containers
    images_list = []
    annotations_list = []
    ann_id = 1

    classes_norm = [w / sum(CLASS_WEIGHTS) for w in CLASS_WEIGHTS]

    print(f"Generating {num_images} synthetic images ({img_w}×{img_h}) ...")
    for idx in range(num_images):
        img_name = f"sim_{idx:04d}.png"
        img_path = out_img_dir / img_name

        img = _terrain_background(img_h, img_w, rng)

        n_objects = rng.randint(3, 7)
        placed_boxes: List[Tuple[int, int, int, int]] = []

        # Sample class IDs proportional to CLASS_WEIGHTS
        class_ids = rng.choices(range(NUM_CLASSES), weights=classes_norm, k=n_objects)

        for cid in class_ids:
            box = _place_object(img, cid, rng, placed_boxes)
            if box is None:
                continue
            x, y, bw, bh = box
            placed_boxes.append(box)
            annotations_list.append({
                "id":          ann_id,
                "image_id":    idx,
                "category_id": cid,          # 0-indexed, matches CLASS_NAMES directly
                "bbox":        [x, y, bw, bh],   # COCO [x, y, w, h]
                "area":        bw * bh,
                "iscrowd":     0,
            })
            ann_id += 1

        cv2.imwrite(str(img_path), img)
        images_list.append({
            "id":        idx,
            "file_name": img_name,
            "width":     img_w,
            "height":    img_h,
        })

        n_placed = len(placed_boxes)
        if (idx + 1) % 5 == 0 or idx == num_images - 1:
            print(f"  [{idx+1:>4}/{num_images}]  {img_name}  "
                  f"({n_placed} objects: "
                  + ", ".join(CLASS_NAMES[c] for c in class_ids[:n_placed]) + ")")

    # COCO categories (0-indexed ids — evaluate.py reads category_id as class_id directly)
    categories = [
        {"id": i, "name": CLASS_NAMES[i], "supercategory": SUPERCATEGORY[i]}
        for i in range(NUM_CLASSES)
    ]

    coco = {
        "info": {
            "description": "Drone perception synthetic simulation dataset",
            "version": "1.0",
            "num_images": num_images,
            "img_size": f"{img_w}x{img_h}",
            "seed": seed,
        },
        "categories":   categories,
        "images":       images_list,
        "annotations":  annotations_list,
    }

    with open(out_ann_file, "w") as f:
        json.dump(coco, f, indent=2)

    total_ann = len(annotations_list)
    print(f"\nDone.")
    print(f"  Images      : {num_images}  →  {out_img_dir}/")
    print(f"  Annotations : {total_ann} total ({total_ann/num_images:.1f} per image avg)")
    print(f"  COCO JSON   : {out_ann_file}")
    print(f"\nNext step — build the sim ONNX (if not already done):")
    print(f"  python drone_perception/scripts/benchmark.py --build-sim-onnx")
    print(f"\nThen evaluate (--max-images limits to a quick N-image subset):")
    print(f"  python -m drone_perception.scripts.evaluate \\")
    print(f"    --onnx    weights/yolov9s_drone_sim.onnx \\")
    print(f"    --dataset {out_ann_file} \\")
    print(f"    --images  {out_img_dir} \\")
    print(f"    --max-images {num_images}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate a synthetic aerial dataset for pipeline smoke-testing."
    )
    ap.add_argument("--num-images",  type=int, default=20,
                    help="Number of images to generate (default: 20)")
    ap.add_argument("--img-width",   type=int, default=1280,
                    help="Image width  in pixels (default: 1280)")
    ap.add_argument("--img-height",  type=int, default=720,
                    help="Image height in pixels (default: 720)")
    ap.add_argument("--out-images",  default="data/sim/images/val",
                    help="Output image directory (default: data/sim/images/val)")
    ap.add_argument("--out-ann",     default="data/sim/annotations/sim_val.json",
                    help="Output annotation JSON (default: data/sim/annotations/sim_val.json)")
    ap.add_argument("--seed",        type=int, default=42,
                    help="Random seed for reproducibility (default: 42)")
    args = ap.parse_args()

    build_dataset(
        out_img_dir=Path(args.out_images),
        out_ann_file=Path(args.out_ann),
        num_images=args.num_images,
        img_w=args.img_width,
        img_h=args.img_height,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
