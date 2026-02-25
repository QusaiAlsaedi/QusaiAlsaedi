"""
VisDrone2019-DET dataset download guide, validation, and conversion.

VisDrone is a UAV-perspective pedestrian/vehicle detection dataset —
the closest publicly available data to what this system actually sees.

DOWNLOAD (registration-free mirrors):
  The official challenge site requires a Baidu account. Use these instead:

  Train (~1.4 GB):
    wget "https://github.com/VisDrone/VisDrone-Dataset/releases/download/v2019/VisDrone2019-DET-train.zip" \
         -O data/visdrone/raw/VisDrone2019-DET-train.zip

  Val (~240 MB):
    wget "https://github.com/VisDrone/VisDrone-Dataset/releases/download/v2019/VisDrone2019-DET-val.zip" \
         -O data/visdrone/raw/VisDrone2019-DET-val.zip

  SHA-256 checksums:
    train: see VisDrone GitHub README for current hash
    val:   see VisDrone GitHub README for current hash

  If those links are down, the dataset is mirrored on:
    - IEEE DataPort (free account): https://ieee-dataport.org/open-access/visdrone
    - Academic Torrents:            https://academictorrents.com (search "VisDrone")

After downloading, run:
  python -m drone_perception.scripts.datasets.visdrone \
    --raw   data/visdrone/raw \
    --out   data/visdrone \
    --split 0.9

VisDrone class → our taxonomy:
  0  ignored_regions   SKIP
  1  pedestrian        → person_standing  (0)
  2  people            → person_standing  (0)  (crowd, distant)
  3  bicycle           → bicycle          (13)
  4  car               → car_sedan        (7)
  5  van               → car_van          (11)
  6  truck             → car_truck_light  (9)
  7  tricycle          → motorcycle       (12)  (closest vehicle type)
  8  awning-tricycle   → motorcycle       (12)
  9  bus               → car_truck_heavy  (10)
  10 motor             → motorcycle       (12)
  11 others            SKIP

Annotation format (per line in .txt):
  <x_left>,<y_top>,<w>,<h>,<score>,<category>,<truncation>,<occlusion>
  score=0 means annotation is ignored (skip).
  truncation: 0=full, 1=partial, 2=heavy
  occlusion:  0=none, 1=partial, 2=heavy
"""

from __future__ import annotations

import argparse
import json
import os
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Class mapping
# ---------------------------------------------------------------------------

# VisDrone category id → our class_id (-1 = skip)
VISDRONE_TO_OURS: Dict[int, int] = {
    0:  -1,   # ignored_regions
    1:   0,   # pedestrian      → person_standing
    2:   0,   # people          → person_standing
    3:  13,   # bicycle         → bicycle
    4:   7,   # car             → car_sedan
    5:  11,   # van             → car_van
    6:   9,   # truck           → car_truck_light
    7:  12,   # tricycle        → motorcycle
    8:  12,   # awning-tricycle → motorcycle
    9:  10,   # bus             → car_truck_heavy
    10: 12,   # motor           → motorcycle
    11: -1,   # others
}

VISDRONE_NAMES = {
    1: "pedestrian", 2: "people", 3: "bicycle", 4: "car",
    5: "van", 6: "truck", 7: "tricycle", 8: "awning-tricycle",
    9: "bus", 10: "motor",
}

# Must match detector.py CLASS_NAMES
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
# Annotation parsing
# ---------------------------------------------------------------------------

def parse_visdrone_annotation(ann_path: Path) -> List[Dict]:
    """
    Parse a single VisDrone annotation file.
    Returns list of dicts with keys: bbox_xywh, class_id, class_name,
    truncation, occlusion, source_class_id, source_class_name.
    Skips score=0 (marked as ignored) and category 0/11.
    """
    annotations = []
    if not ann_path.exists():
        return annotations

    with open(ann_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 8:
                continue
            try:
                x, y, w, h    = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                score         = int(parts[4])
                vd_cat        = int(parts[5])
                truncation    = int(parts[6])
                occlusion     = int(parts[7])
            except ValueError:
                continue

            if score == 0:
                continue                    # ignored annotation
            if w <= 0 or h <= 0:
                continue                    # degenerate box

            our_id = VISDRONE_TO_OURS.get(vd_cat, -1)
            if our_id == -1:
                continue                    # skip ignored / others

            annotations.append({
                "bbox_xywh":        [x, y, w, h],
                "class_id":         our_id,
                "class_name":       OUR_CLASS_NAMES[our_id],
                "supercategory":    OUR_SUPERCATEGORY[our_id],
                "truncation":       truncation,
                "occlusion":        occlusion,
                "source_class_id":  vd_cat,
                "source_class_name": VISDRONE_NAMES.get(vd_cat, "unknown"),
            })
    return annotations


def bbox_xywh_to_xyxy(xywh: List[int]) -> List[int]:
    x, y, w, h = xywh
    return [x, y, x + w, y + h]


def bbox_xywh_to_yolo(xywh: List[int], img_w: int, img_h: int) -> Tuple[float, ...]:
    """Convert [x, y, w, h] pixel coords to YOLO normalised [cx, cy, w, h]."""
    x, y, w, h = xywh
    return (
        (x + w / 2) / img_w,
        (y + h / 2) / img_h,
        w / img_w,
        h / img_h,
    )


# ---------------------------------------------------------------------------
# Dataset extraction
# ---------------------------------------------------------------------------

def extract_if_needed(zip_path: Path, dest: Path) -> None:
    if dest.exists() and any(dest.iterdir()):
        print(f"  Already extracted: {dest}")
        return
    print(f"  Extracting {zip_path.name} ...")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    print(f"  Done.")


def find_visdrone_root(base: Path) -> Optional[Path]:
    """Find the directory that contains images/ and annotations/ sub-dirs."""
    for candidate in [base, *base.iterdir()]:
        if candidate.is_dir() and (candidate / "images").exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Conversion: VisDrone → YOLO txt + COCO JSON
# ---------------------------------------------------------------------------

def convert_split(
    src_root: Path,
    out_images: Path,
    out_labels: Path,
    split_name: str,
) -> Tuple[List[Dict], Counter]:
    """
    Convert one split (train or val) to:
      out_labels/{split_name}/{image_stem}.txt  — YOLO format
      Returns (coco_annotations_list, class_counter)
    """
    out_labels.mkdir(parents=True, exist_ok=True)
    out_images.mkdir(parents=True, exist_ok=True)

    img_dir = src_root / "images"
    ann_dir = src_root / "annotations"

    image_files = sorted(img_dir.glob("*.jpg")) + sorted(img_dir.glob("*.png"))
    if not image_files:
        raise FileNotFoundError(f"No images found in {img_dir}")

    coco_images      = []
    coco_annotations = []
    ann_id           = 1
    class_counter    = Counter()
    area_by_class: Dict[int, List[float]] = defaultdict(list)

    for img_id, img_path in enumerate(image_files, start=1):
        stem    = img_path.stem
        ann_path = ann_dir / f"{stem}.txt"
        anns    = parse_visdrone_annotation(ann_path)

        # Try to read image dimensions without fully loading it
        img_w, img_h = _read_image_size(img_path)

        # COCO image entry
        coco_images.append({
            "id":        img_id,
            "file_name": str(img_path.relative_to(src_root.parent.parent)),
            "width":     img_w,
            "height":    img_h,
        })

        # YOLO label file
        yolo_lines = []
        for ann in anns:
            cx, cy, nw, nh = bbox_xywh_to_yolo(ann["bbox_xywh"], img_w, img_h)
            # Clamp to [0, 1]
            cx  = max(0.0, min(1.0, cx))
            cy  = max(0.0, min(1.0, cy))
            nw  = max(0.001, min(1.0, nw))
            nh  = max(0.001, min(1.0, nh))
            yolo_lines.append(f"{ann['class_id']} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")

            x, y, w, h = ann["bbox_xywh"]
            area = float(w * h)
            area_by_class[ann["class_id"]].append(area)
            class_counter[ann["class_name"]] += 1

            coco_annotations.append({
                "id":          ann_id,
                "image_id":    img_id,
                "category_id": ann["class_id"],
                "bbox":        [x, y, w, h],
                "area":        area,
                "iscrowd":     0,
                "source_class": ann["source_class_name"],
                "truncation":  ann["truncation"],
                "occlusion":   ann["occlusion"],
            })
            ann_id += 1

        label_file = out_labels / f"{stem}.txt"
        label_file.write_text("\n".join(yolo_lines))

    return coco_images, coco_annotations, class_counter, area_by_class


def _read_image_size(path: Path) -> Tuple[int, int]:
    """Read (width, height) without loading full image. Falls back to OpenCV."""
    try:
        # Fast path: read JPEG/PNG headers only
        import struct
        with open(path, "rb") as f:
            header = f.read(24)
        if header[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", header[16:24])
            return w, h
        if header[:2] == b"\xff\xd8":     # JPEG — need to scan SOF marker
            import cv2
            img = cv2.imread(str(path))
            if img is not None:
                return img.shape[1], img.shape[0]
    except Exception:
        pass
    try:
        import cv2
        img = cv2.imread(str(path))
        if img is not None:
            return img.shape[1], img.shape[0]
    except Exception:
        pass
    return 1360, 765   # VisDrone default fallback


def build_coco_json(
    images: List[Dict],
    annotations: List[Dict],
    source: str,
    split: str,
) -> Dict:
    categories = [
        {"id": i, "name": n, "supercategory": OUR_SUPERCATEGORY.get(i, "unknown")}
        for i, n in enumerate(OUR_CLASS_NAMES)
    ]
    return {
        "info":        {"source": source, "split": split, "version": "1.0"},
        "images":      images,
        "annotations": annotations,
        "categories":  categories,
    }


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def print_summary(
    split: str,
    n_images: int,
    class_counter: Counter,
    area_by_class: Dict[int, List[float]],
) -> None:
    total = sum(class_counter.values())
    print(f"\n  [{split}]  {n_images} images  |  {total} annotations")
    print(f"  {'Class':<22} {'Count':>7}  {'MinArea':>9}  {'MedArea':>9}  {'MaxArea':>9}")
    print(f"  {'-'*62}")
    for name, count in sorted(class_counter.items(), key=lambda x: -x[1]):
        cid    = OUR_CLASS_NAMES.index(name)
        areas  = area_by_class.get(cid, [0])
        a      = np.array(areas)
        print(f"  {name:<22} {count:>7}  "
              f"{int(a.min()):>9}  {int(np.median(a)):>9}  {int(a.max()):>9}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_raw(raw_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Returns (train_root, val_root) or raises with clear instructions.
    """
    expected = {
        "train": "VisDrone2019-DET-train",
        "val":   "VisDrone2019-DET-val",
    }
    roots = {}
    for split, dirname in expected.items():
        d = raw_dir / dirname
        if not d.exists():
            # Try after extraction
            zp = raw_dir / f"{dirname}.zip"
            if zp.exists():
                extract_if_needed(zp, d)
            else:
                print(f"\n  Missing: {d}")
                print(f"  Download with:")
                print(f"    mkdir -p {raw_dir}")
                if split == "train":
                    print(f"    wget -O {raw_dir}/VisDrone2019-DET-train.zip \\")
                    print(f'      "https://github.com/VisDrone/VisDrone-Dataset/releases/download/v2019/VisDrone2019-DET-train.zip"')
                else:
                    print(f"    wget -O {raw_dir}/VisDrone2019-DET-val.zip \\")
                    print(f'      "https://github.com/VisDrone/VisDrone-Dataset/releases/download/v2019/VisDrone2019-DET-val.zip"')
                roots[split] = None
                continue

        root = find_visdrone_root(d)
        if root is None:
            raise FileNotFoundError(f"Cannot find images/ subdir under {d}")
        roots[split] = root

    return roots.get("train"), roots.get("val")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Validate and convert VisDrone2019-DET to YOLO + COCO format"
    )
    ap.add_argument("--raw",   default="data/visdrone/raw",
                    help="Directory containing downloaded .zip files or extracted folders")
    ap.add_argument("--out",   default="data/visdrone",
                    help="Output root (will create images/, labels/, annotations/ under here)")
    args = ap.parse_args()

    raw_dir = Path(args.raw)
    out_dir = Path(args.out)

    print("VisDrone2019-DET — validate + convert")
    print(f"  raw  → {raw_dir}")
    print(f"  out  → {out_dir}")

    train_root, val_root = validate_raw(raw_dir)
    if train_root is None and val_root is None:
        print("\nNeither split found. Download both zips and re-run.")
        return

    summary_path = out_dir / "visdrone_summary.json"
    summary      = {}

    for split, src_root in [("train", train_root), ("val", val_root)]:
        if src_root is None:
            print(f"\n  Skipping {split} (not downloaded)")
            continue

        print(f"\nConverting {split} from {src_root} ...")
        imgs, anns, class_ctr, area_by_class = convert_split(
            src_root,
            out_dir / "images" / split,
            out_dir / "labels" / split,
            split,
        )

        # COCO JSON
        ann_dir  = out_dir / "annotations"
        ann_dir.mkdir(parents=True, exist_ok=True)
        coco     = build_coco_json(imgs, anns, source="visdrone", split=split)
        coco_path = ann_dir / f"visdrone_{split}.json"
        with open(coco_path, "w") as f:
            json.dump(coco, f)
        print(f"  Saved COCO annotations → {coco_path}")

        print_summary(split, len(imgs), class_ctr, area_by_class)
        summary[split] = {
            "n_images":   len(imgs),
            "n_anns":     len(anns),
            "per_class":  dict(class_ctr),
        }

    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSummary saved → {summary_path}")
    print("\nDone. Next step:")
    print("  python -m drone_perception.scripts.datasets.cowc --raw data/cowc/raw --out data/cowc")


if __name__ == "__main__":
    main()
