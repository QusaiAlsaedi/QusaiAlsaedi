"""
Benchmark script: measure P50/P95/P99 latency per stage on target hardware.

Usage:
  # Build sim ONNX first (one-time):
  python drone_perception/scripts/benchmark.py --build-sim-onnx

  # Then benchmark:
  python -m drone_perception.scripts.benchmark \
    --onnx weights/yolov9s_drone_sim.onnx \
    --frames 300

Output (example on Orin Nano FP16):
  Stage          P50    P95    P99
  preprocess     1.8    2.1    2.4  ms
  inference      4.3    5.0    5.6  ms
  postprocess    0.6    0.8    1.1  ms
  tracking       1.3    1.8    2.4  ms
  smoothing      0.2    0.4    0.5  ms
  pipeline_total 10.1   12.4   14.1 ms
  effective_fps  29.8 FPS
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
import time
from typing import Dict, List

import numpy as np

# Allow running as `python drone_perception/scripts/benchmark.py` (not just as a module)
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def build_sim_onnx(out_path: str = "weights/yolov9s_drone_sim.onnx") -> str:
    """
    Build a minimal ONNX stub that matches the YOLOv9-S production I/O shapes.

    Input  : (1, 3, INPUT_H, INPUT_W)  float32  — matches Preprocessor letterbox size
    Output : (1, N_ANCHORS, 4+NUM_CLASSES) float32 — all zeros (untrained stub)

    The model uses ConstantOfShape to produce zeros without storing any weight
    matrices, so the .onnx file is only a few KB. Use for pipeline smoke-testing
    and end-to-end evaluation with synthetic datasets.
    """
    try:
        import onnx
        from onnx import helper, TensorProto
    except ImportError:
        print("ERROR: onnx not installed.  Run: pip install onnx")
        sys.exit(1)

    try:
        from drone_perception.core.preprocessor import INPUT_W, INPUT_H
        from drone_perception.core.detector import NUM_CLASSES
    except ImportError:
        INPUT_W, INPUT_H, NUM_CLASSES = 1280, 736, 23

    NET_W, NET_H = INPUT_W, INPUT_H
    N_ANCHORS = (
        (NET_W // 8)  * (NET_H // 8) +   # P3 stride-8  : 160×92 = 14 720
        (NET_W // 16) * (NET_H // 16) +   # P4 stride-16 :  80×46 =  3 680
        (NET_W // 32) * (NET_H // 32)     # P5 stride-32 :  40×23 =    920
    )                                     # Total         :         19 320
    OUT_DIM = 4 + NUM_CLASSES             # 4 bbox coords + 23 class scores = 27

    input_vi  = helper.make_tensor_value_info(
        "input",  TensorProto.FLOAT, [1, 3, NET_H, NET_W])
    output_vi = helper.make_tensor_value_info(
        "output", TensorProto.FLOAT, [1, N_ANCHORS, OUT_DIM])

    # Node 1: Constant holding the target output shape [1, N_ANCHORS, OUT_DIM]
    shape_node = helper.make_node(
        "Constant", inputs=[], outputs=["const_shape"],
        value=helper.make_tensor("", TensorProto.INT64, [3],
                                 [1, N_ANCHORS, OUT_DIM]),
    )
    # Node 2: ConstantOfShape → fills a tensor of that shape with 0.0
    cos_node = helper.make_node(
        "ConstantOfShape", inputs=["const_shape"], outputs=["output"],
        value=helper.make_tensor("fill_val", TensorProto.FLOAT, [1], [0.0]),
    )

    graph = helper.make_graph(
        [shape_node, cos_node], "yolov9s_drone_sim",
        [input_vi], [output_vi],
    )
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 11)])
    model.doc_string = (
        f"YOLOv9-S drone perception sim model (zero-weight stub).\n"
        f"Input: (1,3,{NET_H},{NET_W})  "
        f"Output: (1,{N_ANCHORS},{OUT_DIM})\n"
        f"Use for pipeline smoke-testing only — not a trained model."
    )
    onnx.checker.check_model(model)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(out))

    size_kb = out.stat().st_size // 1024
    print(f"Sim ONNX saved → {out}")
    print(f"  Input  : (1, 3, {NET_H}, {NET_W})")
    print(f"  Output : (1, {N_ANCHORS}, {OUT_DIM})"
          f"  [{N_ANCHORS} anchors | 4 bbox + {NUM_CLASSES} classes]")
    print(f"  Size   : {size_kb} KB  (zero-weight stub, passes onnx.checker)")
    return str(out)


def run_benchmark(onnx_path: str, n_frames: int = 300) -> None:
    from drone_perception.deployment.trt_engine import ONNXEngine
    from drone_perception.core.preprocessor import Preprocessor, INPUT_W, INPUT_H
    from drone_perception.core.detector import PostProcessor
    from drone_perception.core.tracker import ByteTracker
    from drone_perception.core.temporal_smoother import TemporalSmoother
    from drone_perception.core.confidence_calibrator import ConfidenceCalibrator

    engine     = ONNXEngine(onnx_path, use_cuda=False)
    pre        = Preprocessor()
    post       = PostProcessor()
    calibrator = ConfidenceCalibrator()
    tracker    = ByteTracker(min_hits=1)
    smoother   = TemporalSmoother()

    timings: Dict[str, List[float]] = {
        "preprocess": [], "inference": [], "postprocess": [],
        "tracking": [], "smoothing": [], "pipeline_total": [],
    }

    print(f"Benchmarking {n_frames} frames on {onnx_path} ...")
    for i in range(n_frames):
        frame = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)

        t0 = time.perf_counter()
        pre_result = pre.process(frame, i)
        timings["preprocess"].append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        raw = engine.infer(pre_result.blob)
        timings["inference"].append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        dets = post.process(raw, pre_result.scale, pre_result.pad_top,
                            pre_result.pad_left, *pre_result.orig_hw)
        dets = calibrator.calibrate(dets)
        timings["postprocess"].append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        tracks = tracker.update(dets)
        timings["tracking"].append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        tracks = smoother.update(tracks)
        timings["smoothing"].append((time.perf_counter() - t0) * 1000)

        total = sum(v[-1] for v in timings.values() if v)
        timings["pipeline_total"].append(total)

    print(f"\n{'Stage':<20} {'P50':>8} {'P95':>8} {'P99':>8}  ms")
    print("-" * 50)
    for stage, vals in timings.items():
        a = np.array(vals)
        print(f"{stage:<20} {np.percentile(a,50):>8.2f} "
              f"{np.percentile(a,95):>8.2f} {np.percentile(a,99):>8.2f}")

    total_arr = np.array(timings["pipeline_total"])
    fps = 1000.0 / total_arr.mean()
    print(f"\nEffective FPS (mean): {fps:.1f}")


def main():
    p = argparse.ArgumentParser(
        description="Benchmark drone perception pipeline latency per stage, "
                    "or build the sim ONNX stub."
    )
    p.add_argument(
        "--build-sim-onnx", metavar="OUT",
        nargs="?", const="weights/yolov9s_drone_sim.onnx",
        help="Build a zero-weight sim ONNX (default path: weights/yolov9s_drone_sim.onnx) "
             "and exit.  No --onnx required.",
    )
    p.add_argument("--onnx",   default=None,
                   help="ONNX model to benchmark (required unless --build-sim-onnx)")
    p.add_argument("--frames", type=int, default=300)
    args = p.parse_args()

    if args.build_sim_onnx is not None:
        build_sim_onnx(args.build_sim_onnx)
        return

    if not args.onnx:
        p.error("--onnx is required for benchmarking (or use --build-sim-onnx)")
    run_benchmark(args.onnx, args.frames)


if __name__ == "__main__":
    main()
