"""
Benchmark script: measure P50/P95/P99 latency per stage on target hardware.

Usage:
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
import time
from typing import Dict, List

import numpy as np


def run_benchmark(onnx_path: str, n_frames: int = 300) -> None:
    from ..deployment.trt_engine import ONNXEngine
    from ..core.preprocessor import Preprocessor, INPUT_W, INPUT_H
    from ..core.detector import PostProcessor
    from ..core.tracker import ByteTracker
    from ..core.temporal_smoother import TemporalSmoother
    from ..core.confidence_calibrator import ConfidenceCalibrator

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
    p = argparse.ArgumentParser()
    p.add_argument("--onnx",   required=True)
    p.add_argument("--frames", type=int, default=300)
    args = p.parse_args()
    run_benchmark(args.onnx, args.frames)


if __name__ == "__main__":
    main()
