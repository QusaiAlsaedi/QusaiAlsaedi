#!/usr/bin/env bash
# =============================================================================
# Export YOLOv9-S → ONNX → TensorRT FP16/INT8 engine
# Run on Jetson Orin Nano (must have JetPack 5.x + TensorRT 8.6+)
# =============================================================================
set -euo pipefail

WEIGHTS="${1:-checkpoints/yolov9s_drone_v1.pt}"
OUT_DIR="weights"
NET_W=1280
NET_H=736
OPSET=17

echo "==> Step 1: Export PyTorch → ONNX"
python -c "
import sys
sys.path.insert(0, 'vendor/yolov9')
from export import run
run(weights='${WEIGHTS}', imgsz=[${NET_H}, ${NET_W}], batch_size=1,
    include=['onnx'], opset=${OPSET}, simplify=False)
"
ONNX_RAW="${WEIGHTS%.pt}.onnx"

echo "==> Step 2: Simplify ONNX graph"
onnxsim "${ONNX_RAW}" "${OUT_DIR}/yolov9s_drone_sim.onnx"

echo "==> Step 3: Build TRT FP16 engine"
trtexec \
  --onnx="${OUT_DIR}/yolov9s_drone_sim.onnx" \
  --saveEngine="${OUT_DIR}/yolov9s_drone_fp16.engine" \
  --fp16 \
  --workspace=1024 \
  --verbose \
  2>&1 | tee "${OUT_DIR}/trtexec_fp16.log"

echo "==> Step 4: Profile FP16 engine"
trtexec \
  --loadEngine="${OUT_DIR}/yolov9s_drone_fp16.engine" \
  --iterations=100 \
  --avgRuns=50 \
  2>&1 | grep -E "mean|median|GPU|min|max" | tee "${OUT_DIR}/profile_fp16.txt"

echo ""
echo "✓ FP16 engine: ${OUT_DIR}/yolov9s_drone_fp16.engine"
echo ""
echo "==> Step 5 (optional): Build INT8 engine (run calibration first)"
echo "  python -m drone_perception.scripts.calibrate_int8 \\"
echo "    --onnx ${OUT_DIR}/yolov9s_drone_sim.onnx \\"
echo "    --calib-data data/calibration_1000_frames/ \\"
echo "    --output ${OUT_DIR}/yolov9s_drone_int8.engine"
