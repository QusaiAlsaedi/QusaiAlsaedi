"""
COWC (Cars Overhead With Context) dataset download, validation, and conversion.

COWC is a top-down aerial vehicle detection dataset covering six cities
at ~15 cm/pixel resolution. It's one of the few aerial car datasets with
real labelled bounding boxes rather than point annotations only.

DOWNLOAD (direct wget, no registration):

  Six city sub-datasets (~2.4 GB total uncompressed):
  All are hosted at gdo152.llnl.gov/cowc/.

  For vehicle detection (the subset we want — labelled patches with bboxes):
    wget -r -np -nH --cut-dirs=2 -A "*.tbz" \
         https://gdo152.llnl.gov/cowc/datasets/patch_64/  \
         -P data/cowc/raw/

  Or download individually (faster, pick what you need):
    BASE=https://gdo152.llnl.gov/cowc/datasets/patch_64

    wget $BASE/Potsdam_ISPRS.tbz         -P data/cowc/raw/
    wget $BASE/Selwyn_LINZ.tbz           -P data/cowc/raw/
    wget $BASE/Toronto_ISPRS.tbz         -P data/cowc/raw/
    wget $BASE/Utah_AGRC.tbz             -P data/cowc/raw/
    wget $BASE/Columbus_CSUAV_AFRL.tbz   -P data/cowc/raw/
    wget $BASE/Lima_Peru.tbz             -P data/cowc/raw/

  Then run:
    python -m drone_perception.scripts.datasets.cowc \
      --raw data/cowc/raw --out data/cowc

COWC patch format:
  Each .tbz contains:
    <city>/
      Train_<city>_<id>.png     64×64 patch (centred on candidate location)
      Train_<city>_<id>.csv     one row: x_centre, y_centre, width, height
                                (positive samples only — negatives have empty CSV)

  Negative samples (empty CSV) are excluded — we only want positive car detections.

  All COWC detections map to car_sedan (class_id=7) in our taxonomy.
  Sub-type (sedan/SUV/truck) is not labelled; use COWC as backbone pre-training
  and VisDrone for vehicle sub-type supervision.

COWC class → our taxonomy:
  car (all) → car_sedan (7)

Note on bbox construction:
  COWC CSVs give a single (cx, cy, w, h) per patch in patch-local coords.
  Patches are 64×64. We create a bbox from the CSV values, clamped to the patch.
"""

from __future__ import annotations

import argparse
import csv as csv_mod
import json
import os
import tarfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COWC_CLASS_ID   = 7          # car_sedan in our taxonomy
COWC_CLASS_NAME = "car_sedan"
PATCH_SIZE      = 64         # all COWC patches are 64×64 pixels

CITIES = [
    "Potsdam_ISPRS",
    "Selwyn_LINZ",
    "Toronto_ISPRS",
    "Utah_AGRC",
    "Columbus_CSUAV_AFRL",
    "Lima_Peru",
]

OUR_CLASS_NAMES = [
    "person_standing", "person_crouching", "person_prone",
    "dog_cat", "large_mammal", "bird_large", "bird_small",
    "car_sedan", "car_suv", "car_truck_light", "car_truck_heavy",
    "car_van", "motorcycle", "bicycle", "emergency_vehicle", "boat",
    "quad_micro", "quad_consumer", "quad_commercial",
    "fixed_wing_small", "fixed_wing_large", "hybrid_vtol", "blimp_balloon",
]

OUR_SUPERCATEGORY = {
    **{i: "human_presence" for i in range(0, 3)},
    **{i: "animal"         for i in range(3, 7)},
    **{i: "vehicle"        for i in range(7, 16)},
    **{i: "drone"          for i in range(16, 23)},
}


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_tbz(tbz_path: Path, dest: Path) -> None:
    city_name = tbz_path.stem
    city_dir  = dest / city_name
    if city_dir.exists() and any(city_dir.iterdir()):
        print(f"  Already extracted: {city_dir.name}")
        return
    city_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Extracting {tbz_path.name} ...")
    with tarfile.open(tbz_path, "r:bz2") as tf:
        tf.extractall(city_dir)
    print(f"  Done → {city_dir}")


# ---------------------------------------------------------------------------
# Parse one COWC patch
# ---------------------------------------------------------------------------

def parse_cowc_csv(csv_path: Path) -> Optional[Dict]:
    """
    Returns bbox dict or None if patch is negative (no car).
    CSV format: cx, cy, w, h  (all in patch-local pixels, single row)
    """
    if not csv_path.exists():
        return None
    with open(csv_path) as f:
        rows = list(csv_mod.reader(f))
    # Empty or header-only → negative sample
    data_rows = [r for r in rows if r and r[0].strip().lstrip("-").replace(".", "").isdigit()]
    if not data_rows:
        return None

    try:
        row = data_rows[0]
        cx, cy = float(row[0]), float(row[1])
        # w/h may be missing in older COWC versions — use default bbox
        w  = float(row[2]) if len(row) > 2 else 20.0
        h  = float(row[3]) if len(row) > 3 else 20.0
    except (ValueError, IndexError):
        return None

    # Convert cx/cy/w/h → x1/y1/x2/y2 clamped to [0, PATCH_SIZE]
    x1 = max(0.0, cx - w / 2)
    y1 = max(0.0, cy - h / 2)
    x2 = min(float(PATCH_SIZE), cx + w / 2)
    y2 = min(float(PATCH_SIZE), cy + h / 2)

    if x2 <= x1 or y2 <= y1:
        return None

    return {
        "bbox_xyxy":  [x1, y1, x2, y2],
        "bbox_xywh":  [x1, y1, x2 - x1, y2 - y1],
        "class_id":   COWC_CLASS_ID,
        "class_name": COWC_CLASS_NAME,
    }


# ---------------------------------------------------------------------------
# Discover all patches in one city directory
# ---------------------------------------------------------------------------

def collect_city_patches(city_dir: Path) -> List[Tuple[Path, Optional[Dict]]]:
    """
    Walk city_dir for PNG+CSV pairs. Returns list of (img_path, annotation_or_None).
    Includes negative patches too (None annotation) — callers filter them.
    """
    result = []
    # Patches may be directly in city_dir or in a subdirectory
    search_dirs = [city_dir]
    for sub in city_dir.iterdir():
        if sub.is_dir():
            search_dirs.append(sub)

    for d in search_dirs:
        for img_path in sorted(d.glob("*.png")):
            csv_path = img_path.with_suffix(".csv")
            ann      = parse_cowc_csv(csv_path)
            result.append((img_path, ann))
    return result


# ---------------------------------------------------------------------------
# YOLO label writer
# ---------------------------------------------------------------------------

def ann_to_yolo(ann: Dict, img_w: int = PATCH_SIZE, img_h: int = PATCH_SIZE) -> str:
    x1, y1, x2, y2 = ann["bbox_xyxy"]
    cx = ((x1 + x2) / 2) / img_w
    cy = ((y1 + y2) / 2) / img_h
    nw = (x2 - x1) / img_w
    nh = (y2 - y1) / img_h
    return f"{ann['class_id']} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}"


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(
    raw_dir: Path,
    out_dir: Path,
    train_frac: float = 0.85,
    seed: int = 42,
) -> None:
    rng = np.random.default_rng(seed)

    # Collect all positive samples across all cities
    all_pairs: List[Tuple[Path, Dict, str]] = []   # (img_path, ann, city)
    total_negatives = 0

    for city in CITIES:
        city_dir = _find_city_dir(raw_dir, city)
        if city_dir is None:
            print(f"  City not found (skip): {city}")
            continue

        patches = collect_city_patches(city_dir)
        pos     = [(p, a) for p, a in patches if a is not None]
        neg     = [(p, a) for p, a in patches if a is None]
        total_negatives += len(neg)
        all_pairs.extend((p, a, city) for p, a in pos)
        print(f"  {city:<25}  {len(pos):>5} positive  {len(neg):>5} negative (excluded)")

    if not all_pairs:
        print("\nNo positive samples found. Check that .tbz files were extracted.")
        return

    # Shuffle and split
    indices   = rng.permutation(len(all_pairs))
    n_train   = int(len(indices) * train_frac)
    train_idx = indices[:n_train]
    val_idx   = indices[n_train:]

    splits = {"train": train_idx, "val": val_idx}
    summary: Dict = {}
    all_areas: List[float] = []

    for split, idx_arr in splits.items():
        lbl_dir = out_dir / "labels" / split
        img_dir = out_dir / "images" / split
        ann_dir = out_dir / "annotations"
        lbl_dir.mkdir(parents=True, exist_ok=True)
        img_dir.mkdir(parents=True, exist_ok=True)
        ann_dir.mkdir(parents=True, exist_ok=True)

        coco_images = []
        coco_anns   = []
        ann_id      = 1

        for img_rank, i in enumerate(idx_arr, start=1):
            img_path, ann, city = all_pairs[i]

            # Copy / symlink image
            dest_img = img_dir / f"{city}_{img_path.stem}.png"
            if not dest_img.exists():
                import shutil
                shutil.copy2(img_path, dest_img)

            # YOLO label
            lbl_file = lbl_dir / f"{city}_{img_path.stem}.txt"
            lbl_file.write_text(ann_to_yolo(ann))

            # COCO entry
            x1, y1, x2, y2 = ann["bbox_xyxy"]
            w_box = x2 - x1
            h_box = y2 - y1
            area  = w_box * h_box
            all_areas.append(area)

            coco_images.append({
                "id":        img_rank,
                "file_name": str(dest_img),
                "width":     PATCH_SIZE,
                "height":    PATCH_SIZE,
            })
            coco_anns.append({
                "id":          ann_id,
                "image_id":    img_rank,
                "category_id": COWC_CLASS_ID,
                "bbox":        [x1, y1, w_box, h_box],
                "area":        area,
                "iscrowd":     0,
            })
            ann_id += 1

        categories = [
            {"id": i, "name": n, "supercategory": OUR_SUPERCATEGORY.get(i, "unknown")}
            for i, n in enumerate(OUR_CLASS_NAMES)
        ]
        coco_json = {
            "info":        {"source": "cowc", "split": split, "version": "1.0"},
            "images":      coco_images,
            "annotations": coco_anns,
            "categories":  categories,
        }
        coco_path = ann_dir / f"cowc_{split}.json"
        with open(coco_path, "w") as f:
            json.dump(coco_json, f)

        summary[split] = {
            "n_images": len(idx_arr),
            "n_anns":   len(idx_arr),
            "per_class": {COWC_CLASS_NAME: len(idx_arr)},
        }
        print(f"\n  [{split}]  {len(idx_arr)} images  |  {len(idx_arr)} annotations")
        print(f"  COCO annotations → {coco_path}")

    if all_areas:
        a = np.array(all_areas)
        print(f"\n  car_sedan bbox area  min={int(a.min())}  "
              f"median={int(np.median(a))}  max={int(a.max())} px²")

    out_dir.mkdir(parents=True, exist_ok=True)
    sum_path = out_dir / "cowc_summary.json"
    with open(sum_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  Summary → {sum_path}")


def _find_city_dir(raw_dir: Path, city_name: str) -> Optional[Path]:
    """Search for city directory, handling different extraction layouts."""
    candidates = [
        raw_dir / city_name,
        raw_dir / city_name / city_name,
    ]
    # Also search one level deep
    if raw_dir.exists():
        for child in raw_dir.iterdir():
            if child.is_dir() and city_name.lower() in child.name.lower():
                candidates.append(child)
    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert COWC dataset to YOLO + COCO format"
    )
    ap.add_argument("--raw",        default="data/cowc/raw",
                    help="Directory containing .tbz files or extracted city directories")
    ap.add_argument("--out",        default="data/cowc",
                    help="Output root for images/, labels/, annotations/")
    ap.add_argument("--train-frac", type=float, default=0.85,
                    help="Fraction of positive samples for training split (default 0.85)")
    ap.add_argument("--seed",       type=int, default=42)
    args = ap.parse_args()

    raw_dir = Path(args.raw)
    out_dir = Path(args.out)

    print("COWC — validate + convert")
    print(f"  raw → {raw_dir}")
    print(f"  out → {out_dir}")

    if not raw_dir.exists():
        print(f"\n  {raw_dir} does not exist.")
        print("\n  Download with:")
        print(f"    mkdir -p {raw_dir}")
        print(f"    BASE=https://gdo152.llnl.gov/cowc/datasets/patch_64")
        for city in CITIES:
            print(f"    wget $BASE/{city}.tbz -P {raw_dir}/")
        print(f"\n  Then re-run this script.")
        return

    # Extract any .tbz files found
    for tbz in sorted(raw_dir.glob("*.tbz")):
        extract_tbz(tbz, raw_dir)

    print()
    convert(raw_dir, out_dir, train_frac=args.train_frac, seed=args.seed)

    print("\nDone. Next step:")
    print("  python -m drone_perception.scripts.evaluate \\")
    print("    --onnx weights/yolov9s_drone_sim.onnx \\")
    print("    --dataset data/visdrone/annotations/visdrone_val.json \\")
    print("    --images  data/visdrone/images/val")


if __name__ == "__main__":
    main()
