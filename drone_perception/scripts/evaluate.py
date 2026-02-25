"""
Model evaluation: mAP50, mAP50:95, per-class precision/recall, confusion matrix.

Runs the ONNX (or TRT) model over a COCO-format validation set and writes
a structured JSON result to metrics/eval_<timestamp>.json.

Usage:
  # ONNX (runs on any laptop, no GPU required)
  python -m drone_perception.scripts.evaluate \
    --onnx     weights/yolov9s_drone_sim.onnx \
    --dataset  data/visdrone/annotations/visdrone_val.json \
    --images   data/visdrone/images/val

  # TRT (Jetson only)
  python -m drone_perception.scripts.evaluate \
    --engine   weights/yolov9s_drone_fp16.engine \
    --dataset  data/visdrone/annotations/visdrone_val.json \
    --images   data/visdrone/images/val

  # With pycocotools (optional, more accurate interpolation):
  pip install pycocotools
  python -m drone_perception.scripts.evaluate ... --coco-eval

Output (metrics/eval_<timestamp>.json):
  {
    "model": "yolov9s-drone-a1b2c3d4",
    "dataset": "visdrone_val",
    "n_images": 548,
    "mAP50":    0.713,
    "mAP50_95": 0.448,
    "per_class": {
      "person_standing": {"AP50": 0.81, "precision": 0.74, "recall": 0.88, "f1": 0.80},
      ...
    },
    "confusion_matrix": [[...], ...],
    "confusion_labels":  ["person_standing", ..., "background"],
    "timing": {"mean_ms": 6.2, "p95_ms": 7.8}
  }
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from drone_perception.core.preprocessor import Preprocessor
from drone_perception.core.detector import PostProcessor, CLASS_NAMES, NUM_CLASSES
from drone_perception.deployment.trt_engine import ONNXEngine, create_engine


# ---------------------------------------------------------------------------
# IoU helpers
# ---------------------------------------------------------------------------

def iou_matrix(boxes_a: np.ndarray, boxes_b: np.ndarray) -> np.ndarray:
    """
    Compute IoU between every pair.
    boxes_a: (N, 4) xyxy   boxes_b: (M, 4) xyxy
    Returns: (N, M) float32
    """
    if len(boxes_a) == 0 or len(boxes_b) == 0:
        return np.zeros((len(boxes_a), len(boxes_b)), dtype=np.float32)

    ax1, ay1, ax2, ay2 = boxes_a[:, 0], boxes_a[:, 1], boxes_a[:, 2], boxes_a[:, 3]
    bx1, by1, bx2, by2 = boxes_b[:, 0], boxes_b[:, 1], boxes_b[:, 2], boxes_b[:, 3]

    inter_x1 = np.maximum(ax1[:, None], bx1[None, :])
    inter_y1 = np.maximum(ay1[:, None], by1[None, :])
    inter_x2 = np.minimum(ax2[:, None], bx2[None, :])
    inter_y2 = np.minimum(ay2[:, None], by2[None, :])

    inter_w  = np.maximum(0, inter_x2 - inter_x1)
    inter_h  = np.maximum(0, inter_y2 - inter_y1)
    inter    = inter_w * inter_h

    area_a   = (ax2 - ax1) * (ay2 - ay1)
    area_b   = (bx2 - bx1) * (by2 - by1)
    union    = area_a[:, None] + area_b[None, :] - inter

    return inter / (union + 1e-6)


# ---------------------------------------------------------------------------
# Greedy TP/FP matching for one image
# ---------------------------------------------------------------------------

def match_image(
    pred_boxes:  np.ndarray,   # (N, 4) xyxy
    pred_scores: np.ndarray,   # (N,)
    pred_classes: np.ndarray,  # (N,) int
    gt_boxes:    np.ndarray,   # (M, 4) xyxy
    gt_classes:  np.ndarray,   # (M,) int
    iou_threshold: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns:
      tp     (N,) bool — whether each prediction is a true positive
      fp     (N,) bool — whether each prediction is a false positive
      matched_gt (N,) int — index into gt (or -1 if fp)
    """
    n = len(pred_scores)
    tp          = np.zeros(n, dtype=bool)
    fp          = np.zeros(n, dtype=bool)
    matched_gt  = np.full(n, -1, dtype=int)

    if n == 0:
        return tp, fp, matched_gt

    if len(gt_boxes) == 0:
        fp[:] = True
        return tp, fp, matched_gt

    iou = iou_matrix(pred_boxes, gt_boxes)   # (N, M)
    gt_matched = np.zeros(len(gt_boxes), dtype=bool)

    # Sort predictions by score descending
    order = np.argsort(-pred_scores)
    for pi in order:
        # Only match if class matches
        class_iou = iou[pi].copy()
        class_mask = (gt_classes == pred_classes[pi])
        class_iou[~class_mask] = 0.0

        if class_iou.max() >= iou_threshold:
            best_gt = int(class_iou.argmax())
            if not gt_matched[best_gt]:
                tp[pi]          = True
                matched_gt[pi]  = best_gt
                gt_matched[best_gt] = True
            else:
                fp[pi] = True   # GT already claimed
        else:
            fp[pi] = True

    return tp, fp, matched_gt


# ---------------------------------------------------------------------------
# AP computation (interpolated area under PR curve, 101-point VOC style)
# ---------------------------------------------------------------------------

def compute_ap(
    tp_sorted:     np.ndarray,   # cumulative TP, sorted by score desc
    fp_sorted:     np.ndarray,   # cumulative FP
    n_gt:          int,
) -> float:
    if n_gt == 0:
        return 0.0

    tp_cum = np.cumsum(tp_sorted.astype(np.float64))
    fp_cum = np.cumsum(fp_sorted.astype(np.float64))

    recall    = tp_cum / n_gt
    precision = tp_cum / (tp_cum + fp_cum + 1e-6)

    # Prepend sentinel
    recall    = np.concatenate([[0.0], recall,    [1.0]])
    precision = np.concatenate([[1.0], precision, [0.0]])

    # Monotone decreasing envelope
    for i in range(len(precision) - 2, -1, -1):
        precision[i] = max(precision[i], precision[i + 1])

    # 101-point interpolation
    r_interp = np.linspace(0, 1, 101)
    p_interp = np.interp(r_interp, recall, precision)
    return float(p_interp.mean())


# ---------------------------------------------------------------------------
# Per-class results accumulator
# ---------------------------------------------------------------------------

class ClassResults:
    def __init__(self, n_gt: int):
        self.n_gt    = n_gt
        self.scores: List[float] = []
        self.tp:     List[bool]  = []
        self.fp:     List[bool]  = []

    def add(self, score: float, is_tp: bool) -> None:
        self.scores.append(score)
        self.tp.append(is_tp)
        self.fp.append(not is_tp)

    def ap_at_iou(self) -> float:
        if not self.scores:
            return 0.0
        order = np.argsort(-np.array(self.scores))
        return compute_ap(
            np.array(self.tp, dtype=bool)[order],
            np.array(self.fp, dtype=bool)[order],
            self.n_gt,
        )

    def precision_recall_at_conf(self, conf: float = 0.25) -> Tuple[float, float]:
        tp = sum(s >= conf and t for s, t in zip(self.scores, self.tp))
        fp = sum(s >= conf and f for s, f in zip(self.scores, self.fp))
        fn = self.n_gt - tp
        p  = tp / (tp + fp + 1e-6)
        r  = tp / (tp + fn + 1e-6)
        return float(p), float(r)


# ---------------------------------------------------------------------------
# Confusion matrix builder
# ---------------------------------------------------------------------------

class ConfusionMatrix:
    """
    Rows = ground-truth class (last row = background / unmatched GT)
    Cols = predicted class    (last col = background / false positive)
    """

    def __init__(self, n_classes: int):
        self.n = n_classes
        self.mat = np.zeros((n_classes + 1, n_classes + 1), dtype=np.int64)

    def update(
        self,
        pred_classes: np.ndarray,
        tp:           np.ndarray,
        fp:           np.ndarray,
        matched_gt:   np.ndarray,
        gt_classes:   np.ndarray,
    ) -> None:
        for pi in range(len(pred_classes)):
            pc = int(pred_classes[pi])
            if tp[pi]:
                gc = int(gt_classes[matched_gt[pi]])
                self.mat[gc, pc] += 1    # TP
            elif fp[pi]:
                self.mat[self.n, pc] += 1  # FP (predicted something, no GT)

        # FN: GT not matched by any prediction
        matched_gts = set(matched_gt[tp])
        for gi, gc in enumerate(gt_classes):
            if gi not in matched_gts:
                self.mat[int(gc), self.n] += 1

    def normalised(self) -> np.ndarray:
        row_sums = self.mat.sum(axis=1, keepdims=True)
        return self.mat / (row_sums + 1e-6)


# ---------------------------------------------------------------------------
# Dataset loader (COCO JSON)
# ---------------------------------------------------------------------------

def load_coco_dataset(
    ann_file: Path,
    img_dir:  Path,
) -> Tuple[List[Dict], Dict[int, List[Dict]]]:
    """
    Returns:
      images: list of {id, file_name, width, height}
      gt_by_image: {image_id: [{class_id, bbox_xyxy}]}
    """
    with open(ann_file) as f:
        coco = json.load(f)

    images = coco["images"]
    gt_by_image: Dict[int, List[Dict]] = defaultdict(list)

    for ann in coco.get("annotations", []):
        x, y, w, h = ann["bbox"]
        gt_by_image[ann["image_id"]].append({
            "class_id":  ann["category_id"],
            "bbox_xyxy": [x, y, x + w, y + h],
        })

    return images, gt_by_image


# ---------------------------------------------------------------------------
# Evaluation loop
# ---------------------------------------------------------------------------

def evaluate(
    engine,
    images:       List[Dict],
    gt_by_image:  Dict[int, List[Dict]],
    img_dir:      Path,
    iou_thresholds: List[float],
    conf_threshold: float = 0.001,   # low for mAP: collect all detections
    conf_report:    float = 0.25,    # for precision/recall table
    max_images:     Optional[int] = None,
) -> Dict:
    pre        = Preprocessor()
    post       = PostProcessor()

    # Per-class, per-IoU-threshold result accumulators
    # results[iou_thresh][class_id] = ClassResults
    results: Dict[float, Dict[int, ClassResults]] = {}
    for iou_t in iou_thresholds:
        gt_counts: Dict[int, int] = defaultdict(int)
        for gt_list in gt_by_image.values():
            for g in gt_list:
                gt_counts[g["class_id"]] += 1
        results[iou_t] = {
            cid: ClassResults(gt_counts.get(cid, 0))
            for cid in range(NUM_CLASSES)
        }

    cm   = ConfusionMatrix(NUM_CLASSES)
    timing: List[float] = []

    import cv2
    n = min(len(images), max_images) if max_images else len(images)

    for idx, img_meta in enumerate(images[:n]):
        if idx % 50 == 0:
            print(f"  [{idx:>4}/{n}] evaluating ...")

        img_path = img_dir / Path(img_meta["file_name"]).name
        if not img_path.exists():
            # Try full relative path
            img_path = img_dir / img_meta["file_name"]
        if not img_path.exists():
            continue

        bgr = cv2.imread(str(img_path))
        if bgr is None:
            continue

        # Inference
        t0     = time.perf_counter()
        pre_r  = pre.process(bgr)
        raw    = engine.infer(pre_r.blob)
        timing.append((time.perf_counter() - t0) * 1000)

        # Post-process (use very low threshold to collect all detections for mAP)
        from drone_perception.core.detector import DEFAULT_CONF_THRESHOLDS
        orig_thresh = {}
        # Temporarily lower thresholds for full-recall mAP sweep
        low_post = PostProcessor(
            conf_thresholds={k: conf_threshold for k in DEFAULT_CONF_THRESHOLDS},
        )
        dets = low_post.process(
            raw, pre_r.scale, pre_r.pad_top, pre_r.pad_left,
            pre_r.orig_hw[0], pre_r.orig_hw[1],
        )

        img_id    = img_meta["id"]
        gt_list   = gt_by_image.get(img_id, [])
        gt_boxes  = np.array([g["bbox_xyxy"] for g in gt_list], dtype=np.float32) \
                    if gt_list else np.empty((0, 4), dtype=np.float32)
        gt_classes = np.array([g["class_id"] for g in gt_list], dtype=np.int64) \
                     if gt_list else np.empty(0, dtype=np.int64)

        if not dets:
            pred_boxes   = np.empty((0, 4), dtype=np.float32)
            pred_scores  = np.empty(0, dtype=np.float32)
            pred_classes = np.empty(0, dtype=np.int64)
        else:
            pred_boxes   = np.array([d.bbox_xyxy for d in dets], dtype=np.float32)
            pred_scores  = np.array([d.conf for d in dets], dtype=np.float32)
            pred_classes = np.array([d.class_id for d in dets], dtype=np.int64)

        # Match at each IoU threshold
        for iou_t in iou_thresholds:
            tp, fp, matched_gt = match_image(
                pred_boxes, pred_scores, pred_classes,
                gt_boxes, gt_classes, iou_t,
            )
            for pi in range(len(pred_scores)):
                cid = int(pred_classes[pi])
                if cid < NUM_CLASSES:
                    results[iou_t][cid].add(float(pred_scores[pi]), bool(tp[pi]))

        # Confusion matrix at IoU=0.5 with conf_report threshold
        mask = pred_scores >= conf_report
        if mask.any():
            tp_c, fp_c, mg_c = match_image(
                pred_boxes[mask], pred_scores[mask], pred_classes[mask],
                gt_boxes, gt_classes, 0.5,
            )
            cm.update(pred_classes[mask], tp_c, fp_c, mg_c, gt_classes)

    # Compute APs
    ap_50    = {cid: results[0.5][cid].ap_at_iou() for cid in range(NUM_CLASSES)}
    ap_all   = {
        cid: np.mean([results[t][cid].ap_at_iou() for t in iou_thresholds])
        for cid in range(NUM_CLASSES)
    }

    # Only include classes that appeared in GT
    valid_classes = [cid for cid in range(NUM_CLASSES)
                     if results[0.5][cid].n_gt > 0]
    map50    = float(np.mean([ap_50[c]  for c in valid_classes])) if valid_classes else 0.0
    map5095  = float(np.mean([ap_all[c] for c in valid_classes])) if valid_classes else 0.0

    per_class_out = {}
    for cid in valid_classes:
        p, r = results[0.5][cid].precision_recall_at_conf(conf_report)
        f1   = 2 * p * r / (p + r + 1e-6)
        per_class_out[CLASS_NAMES[cid]] = {
            "AP50":      round(ap_50[cid], 4),
            "AP50_95":   round(ap_all[cid], 4),
            "precision": round(p, 4),
            "recall":    round(r, 4),
            "f1":        round(f1, 4),
            "n_gt":      results[0.5][cid].n_gt,
        }

    t_arr = np.array(timing)
    return {
        "mAP50":     round(map50,   4),
        "mAP50_95":  round(map5095, 4),
        "per_class": per_class_out,
        "confusion_matrix": cm.mat.tolist(),
        "confusion_labels": CLASS_NAMES + ["background"],
        "timing": {
            "n_images":  len(timing),
            "mean_ms":   round(float(t_arr.mean()), 2) if len(t_arr) else 0,
            "p95_ms":    round(float(np.percentile(t_arr, 95)), 2) if len(t_arr) else 0,
        },
    }


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------

def print_results(r: Dict, model_version: str) -> None:
    print(f"\n{'='*60}")
    print(f"  Model:   {model_version}")
    print(f"  Images:  {r['timing']['n_images']}")
    print(f"  mAP@0.50:       {r['mAP50']:.4f}")
    print(f"  mAP@0.50:0.95:  {r['mAP50_95']:.4f}")
    print(f"  Inference mean: {r['timing']['mean_ms']:.1f} ms  "
          f"P95: {r['timing']['p95_ms']:.1f} ms")
    print(f"\n  {'Class':<22} {'AP50':>6}  {'Prec':>6}  {'Rec':>6}  {'F1':>6}  {'GT':>6}")
    print(f"  {'-'*56}")
    for name, m in sorted(r["per_class"].items(), key=lambda x: -x[1]["AP50"]):
        print(f"  {name:<22} {m['AP50']:>6.3f}  {m['precision']:>6.3f}  "
              f"{m['recall']:>6.3f}  {m['f1']:>6.3f}  {m['n_gt']:>6}")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Optional: pycocotools evaluation
# ---------------------------------------------------------------------------

def coco_eval(ann_file: Path, results_file: Path) -> Dict:
    try:
        from pycocotools.coco import COCO
        from pycocotools.cocoeval import COCOeval
    except ImportError:
        print("  pycocotools not installed. Run: pip install pycocotools")
        return {}

    coco_gt   = COCO(str(ann_file))
    coco_dt   = coco_gt.loadRes(str(results_file))
    evaluator = COCOeval(coco_gt, coco_dt, "bbox")
    evaluator.evaluate()
    evaluator.accumulate()
    evaluator.summarize()
    stats = evaluator.stats
    return {
        "coco_mAP50_95": round(float(stats[0]), 4),
        "coco_mAP50":    round(float(stats[1]), 4),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Evaluate drone perception model: mAP, precision/recall, confusion matrix"
    )
    ap.add_argument("--engine",      default=None, help="TRT .engine file")
    ap.add_argument("--onnx",        default=None, help="ONNX model file")
    ap.add_argument("--dataset",     required=True,
                    help="COCO-format annotations JSON "
                         "(e.g. data/visdrone/annotations/visdrone_val.json)")
    ap.add_argument("--images",      required=True, help="Directory containing val images")
    ap.add_argument("--conf",        type=float, default=0.25,
                    help="Confidence threshold for precision/recall table (default 0.25)")
    ap.add_argument("--max-images",  type=int, default=None,
                    help="Limit evaluation to N images (useful for quick smoke tests)")
    ap.add_argument("--coco-eval",   action="store_true",
                    help="Also run pycocotools COCO eval (requires pip install pycocotools)")
    ap.add_argument("--out-dir",     default="metrics",
                    help="Output directory for eval JSON (default: metrics/)")
    args = ap.parse_args()

    # Load engine
    if not args.engine and not args.onnx:
        ap.error("Provide --engine or --onnx")
    engine = create_engine(engine_path=args.engine, onnx_path=args.onnx, fp16=True)

    # Load dataset
    ann_path = Path(args.dataset)
    img_dir  = Path(args.images)
    if not ann_path.exists():
        print(f"Dataset not found: {ann_path}")
        print("Run dataset scripts first:")
        print("  python -m drone_perception.scripts.datasets.visdrone ...")
        return

    print(f"Loading dataset: {ann_path.name}")
    images, gt_by_image = load_coco_dataset(ann_path, img_dir)
    print(f"  {len(images)} images  |  "
          f"{sum(len(v) for v in gt_by_image.values())} annotations")

    # IoU thresholds for mAP50:0.95
    iou_thresholds = [round(t, 2) for t in np.arange(0.5, 1.0, 0.05)]

    print(f"\nRunning inference (conf_threshold=0.001 for full PR curve)...")
    metrics = evaluate(
        engine, images, gt_by_image, img_dir,
        iou_thresholds=iou_thresholds,
        conf_threshold=0.001,
        conf_report=args.conf,
        max_images=args.max_images,
    )
    metrics["model"]   = engine.version
    metrics["dataset"] = ann_path.stem

    print_results(metrics, engine.version)

    # Save results
    out_dir  = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"eval_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"Results saved → {out_path}")

    # Optional pycocotools eval
    if args.coco_eval:
        print("\nRunning pycocotools COCO eval ...")
        # Build COCO-format detection results
        coco_dets = []
        # (requires a second pass; skip here — save det JSON and re-evaluate)
        print("  (COCO eval requires detection JSON — re-run with --coco-eval after "
              "generating coco_dets. See pycocotools docs.)")


if __name__ == "__main__":
    main()
