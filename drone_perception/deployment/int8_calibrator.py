"""
INT8 calibration for TensorRT.

Replaces the _DummyCalibrator stub in trt_engine.py with a real
EntropyCalibrator2 that reads representative frames from disk and
applies the exact same preprocessing pipeline used at inference time.

Why this matters:
  INT8 quantisation collapses FP32 activations into 256 buckets.
  TRT's entropy calibrator finds the optimal scale factor per layer by
  minimising the KL divergence between the FP32 and INT8 activation
  distributions on a calibration dataset (~1,000 representative frames).

  If the calibration frames don't match the real inference distribution
  (wrong brightness, different aspect ratios, wrong domain), INT8 mAP
  will be significantly worse than expected. Using the same letterbox +
  normalisation as Preprocessor is therefore not optional.

Calibration dataset:
  Collect ~1,000 frames from your actual deployment environment:
    - Mix of day / low-light / overcast
    - Mix of altitudes (20 m, 50 m, 100 m)
    - Include typical scenes: crowds, vehicles, open terrain
    - NO labelling required — calibration only uses pixel statistics

  Place frames as JPG/PNG in data/calib/:
    data/calib/
      frame_001.jpg
      frame_002.jpg
      ...

Usage:
  # Build INT8 engine
  python -m drone_perception.deployment.int8_calibrator \
    --input  data/calib \
    --onnx   weights/yolov9s_drone_sim.onnx \
    --cache  weights/calibration.cache \
    --output weights/yolov9s_drone_int8.engine

  # Or just generate the calibration cache (reuse with trtexec):
  python -m drone_perception.deployment.int8_calibrator \
    --input  data/calib \
    --onnx   weights/yolov9s_drone_sim.onnx \
    --cache  weights/calibration.cache \
    --cache-only

  # Then build with trtexec using the cache:
  trtexec \
    --onnx=weights/yolov9s_drone_sim.onnx \
    --saveEngine=weights/yolov9s_drone_int8.engine \
    --int8 \
    --calib=weights/calibration.cache
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Iterator, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Preprocessing (mirrors core/preprocessor.py — same letterbox + normalise)
# ---------------------------------------------------------------------------

import cv2

_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _preprocess_frame(
    bgr:    np.ndarray,
    net_w:  int,
    net_h:  int,
) -> np.ndarray:
    """
    Exact replica of Preprocessor.process() without the blur/lowlight metadata.
    Returns (1, 3, H, W) float32 blob — matches what TRT inference expects.
    """
    # Low-light enhancement (match runtime CLAHE)
    ycrcb    = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    mean_lum = float(ycrcb[:, :, 0].mean())
    if mean_lum < 60.0:
        clahe        = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        ycrcb[:, :, 0] = clahe.apply(ycrcb[:, :, 0])
        bgr = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)

    # Letterbox
    h, w     = bgr.shape[:2]
    scale    = min(net_w / w, net_h / h)
    new_w    = int(round(w * scale))
    new_h    = int(round(h * scale))
    resized  = cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_top  = (net_h - new_h) // 2
    pad_left = (net_w - new_w) // 2
    padded   = np.full((net_h, net_w, 3), 114, dtype=np.uint8)
    padded[pad_top:pad_top + new_h, pad_left:pad_left + new_w] = resized

    # Normalise
    rgb  = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb  = (rgb - _MEAN) / _STD
    blob = np.ascontiguousarray(rgb.transpose(2, 0, 1)[np.newaxis])  # (1,3,H,W)
    return blob


# ---------------------------------------------------------------------------
# Frame iterator
# ---------------------------------------------------------------------------

def iter_calib_frames(
    calib_dir: Path,
    net_w:     int,
    net_h:     int,
    max_frames: int = 1000,
) -> Iterator[np.ndarray]:
    """
    Yield preprocessed blobs from calib_dir, one at a time.
    Skips unreadable files silently.
    """
    exts  = {".jpg", ".jpeg", ".png", ".bmp"}
    paths = sorted(
        p for p in calib_dir.iterdir()
        if p.suffix.lower() in exts
    )[:max_frames]

    if not paths:
        raise FileNotFoundError(
            f"No images found in {calib_dir}. "
            f"Place calibration frames (jpg/png) there and re-run."
        )

    logger.info("Calibration set: %d frames from %s", len(paths), calib_dir)
    for p in paths:
        bgr = cv2.imread(str(p))
        if bgr is None:
            logger.warning("Cannot read %s — skipped", p)
            continue
        yield _preprocess_frame(bgr, net_w, net_h)


# ---------------------------------------------------------------------------
# DatasetInt8Calibrator
# ---------------------------------------------------------------------------

try:
    import tensorrt as trt

    class DatasetInt8Calibrator(trt.IInt8EntropyCalibrator2):
        """
        Real INT8 calibrator using actual deployment frames.

        Usage:
          calibrator = DatasetInt8Calibrator(
              calib_dir=Path("data/calib"),
              cache_file="weights/calibration.cache",
          )
          # Pass to TRT builder:
          config.int8_calibrator = calibrator
        """

        def __init__(
            self,
            calib_dir:   Path,
            cache_file:  str,
            net_w:       int = 1280,
            net_h:       int = 736,
            batch_size:  int = 1,
            max_frames:  int = 1000,
        ):
            super().__init__()
            self._cache      = cache_file
            self._batch_size = batch_size
            self._net_w      = net_w
            self._net_h      = net_h

            # Pre-load all blobs into a list so we can index them
            self._blobs: List[np.ndarray] = list(
                iter_calib_frames(calib_dir, net_w, net_h, max_frames)
            )
            self._index = 0

            # Allocate device buffer once
            try:
                import pycuda.autoinit   # noqa: F401
                import pycuda.driver as cuda
                nbytes = int(np.prod((batch_size, 3, net_h, net_w))) * 4  # float32
                self._d_input = cuda.mem_alloc(nbytes)
                self._cuda    = cuda
            except ImportError:
                raise ImportError(
                    "pycuda is required for INT8 calibration. "
                    "Install it via: pip install pycuda"
                )

            logger.info(
                "INT8 calibrator ready: %d frames, batch_size=%d",
                len(self._blobs), batch_size,
            )

        # ------------------------------------------------------------------
        # TRT calibrator interface
        # ------------------------------------------------------------------

        def get_batch_size(self) -> int:
            return self._batch_size

        def get_batch(self, names: List[str]) -> Optional[List[int]]:
            if self._index >= len(self._blobs):
                return None   # signals end of calibration data to TRT

            blob = self._blobs[self._index].astype(np.float32)
            self._index += 1

            self._cuda.memcpy_htod(self._d_input, np.ascontiguousarray(blob))
            logger.debug("Calibration batch %d/%d", self._index, len(self._blobs))
            return [int(self._d_input)]

        def read_calibration_cache(self) -> Optional[bytes]:
            if os.path.exists(self._cache):
                logger.info("Loading calibration cache from %s", self._cache)
                with open(self._cache, "rb") as f:
                    return f.read()
            return None

        def write_calibration_cache(self, cache: bytes) -> None:
            Path(self._cache).parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache, "wb") as f:
                f.write(cache)
            logger.info("Calibration cache saved → %s  (%d bytes)", self._cache, len(cache))

except ImportError:
    # Not on Jetson / TRT not available — define a stub that gives a clear error
    class DatasetInt8Calibrator:   # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError(
                "TensorRT is not installed. DatasetInt8Calibrator requires "
                "TensorRT + PyCUDA (available via JetPack on Jetson)."
            )


# ---------------------------------------------------------------------------
# CLI: build INT8 engine or generate cache only
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(
        description="Build TRT INT8 engine or generate calibration cache"
    )
    ap.add_argument("--input",       required=True,
                    help="Directory of calibration frames (jpg/png, ~1000 images)")
    ap.add_argument("--onnx",        required=True,
                    help="Simplified ONNX file (output of onnxsim)")
    ap.add_argument("--cache",       default="weights/calibration.cache",
                    help="Path to save/load calibration cache")
    ap.add_argument("--output",      default="weights/yolov9s_drone_int8.engine",
                    help="Output TRT INT8 engine file")
    ap.add_argument("--cache-only",  action="store_true",
                    help="Only generate the calibration cache; do not build the full engine")
    ap.add_argument("--net-w",       type=int, default=1280)
    ap.add_argument("--net-h",       type=int, default=736)
    ap.add_argument("--max-frames",  type=int, default=1000,
                    help="Maximum calibration frames to use (default 1000)")
    args = ap.parse_args()

    calib_dir = Path(args.input)
    if not calib_dir.exists():
        print(f"\nCalibration directory not found: {calib_dir}")
        print("\nCreate it and populate with ~1000 representative deployment frames:")
        print(f"  mkdir -p {calib_dir}")
        print(f"  # Copy or symlink representative frames here")
        return

    try:
        import tensorrt as trt
        import pycuda.autoinit   # noqa: F401
        import pycuda.driver as cuda
    except ImportError:
        print("TensorRT / PyCUDA not available.")
        print("Run this script on the target Jetson device with JetPack installed.")
        return

    calibrator = DatasetInt8Calibrator(
        calib_dir  = calib_dir,
        cache_file = args.cache,
        net_w      = args.net_w,
        net_h      = args.net_h,
        max_frames = args.max_frames,
    )

    if args.cache_only:
        # Force TRT to run through the calibration pass to generate the cache.
        # We build a minimal engine just for calibration, then discard it.
        print("Cache-only mode: building temporary engine to generate calibration cache ...")

    TRT_LOGGER = trt.Logger(trt.Logger.WARNING)
    builder    = trt.Builder(TRT_LOGGER)
    network    = builder.create_network(
        1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
    )
    parser = trt.OnnxParser(network, TRT_LOGGER)
    with open(args.onnx, "rb") as f:
        if not parser.parse(f.read()):
            errors = [str(parser.get_error(i)) for i in range(parser.num_errors)]
            print(f"ONNX parse errors: {errors}")
            return

    config = builder.create_builder_config()
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 30)
    config.set_flag(trt.BuilderFlag.INT8)
    config.int8_calibrator = calibrator

    print(f"Building INT8 engine from {args.onnx} ...")
    print(f"  Calibration frames: {len(calibrator._blobs)}")
    print(f"  Network input: {args.net_w}×{args.net_h}")
    print(f"  Cache: {args.cache}")

    serialised = builder.build_serialized_network(network, config)
    if serialised is None:
        print("Engine build failed. Check TRT logs above.")
        return

    if not args.cache_only:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "wb") as f:
            f.write(serialised)
        print(f"\nINT8 engine saved → {args.output}")

    print(f"Calibration cache → {args.cache}")
    print("\nVerify INT8 accuracy:")
    print(f"  python -m drone_perception.scripts.evaluate \\")
    print(f"    --engine {args.output} \\")
    print(f"    --dataset data/visdrone/annotations/visdrone_val.json \\")
    print(f"    --images  data/visdrone/images/val")


if __name__ == "__main__":
    main()
