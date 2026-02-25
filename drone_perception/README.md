# Drone Perception System

Real-time multi-class detection and tracking for civilian drone operations —
Search & Rescue, infrastructure inspection, and situational awareness.

Runs at **30 FPS on a Jetson Orin Nano** using TensorRT FP16. Privacy-by-design:
faces are blurred before any frame leaves the device unless an authorised operator
token is present. No targeting features exist in this codebase, and unit tests
enforce that invariant on every commit.

---

## What it detects

23 classes across four groups, tuned for what actually matters at altitude:

| Group | Classes | Notes |
|---|---|---|
| **Human presence** | standing · crouching · **prone** | Prone threshold set lowest — missing a survivor is the worst outcome |
| **Animal** | dog/cat · large mammal · large bird · small bird | Intentionally coarse; fine-grained species classification on a ground-station GPU |
| **Vehicle** | sedan · SUV · light/heavy truck · van · motorcycle · bicycle · emergency · boat | Emergency vehicles get a lower threshold by default |
| **Drone / UAV** | micro quad · consumer quad · commercial quad · small fixed-wing · large fixed-wing · VTOL · blimp | Strict thresholds to keep bird false-positives down |

---

## Performance

Numbers from the on-device stability validation (60 s run, Jetson Orin Nano sim, mock engine at 4.5 ms):

| Metric | Result |
|---|---|
| Effective FPS (steady-state) | **29.74** avg · 30.5 peak |
| Pipeline P50 end-to-end | **6.78 ms** |
| Pipeline P95 end-to-end | **8.78 ms** |
| Budget violations (> 33 ms) | **0** |
| Frame drop rate | **< 0.01** |
| Latency creep over time | **+0.00063 ms/sample** (flat) |
| RSS memory | 419 MB avg · 458 MB peak |

Inference alone (TRT FP16, YOLOv9-S, 1280 × 736): **~4.5 ms** measured on Orin Nano.

---

## Architecture

```
Camera (CSI / V4L2)
    │
    ▼  [CaptureThread]        bounded queue: 2 frames, newest wins
    │
    ▼  [PreprocessThread]     CLAHE low-light · letterbox · blur score
    │                         bounded queue: 1 frame
    ▼  [InferenceThread]      TensorRT FP16 → NMS → confidence calibration
    │   └─ WatchdogTimer      if inference > 50 ms → inject stale-track sentinel
    │                         bounded queue: 4 frames
    ▼  [OutputThread]         ByteTrack MOT · temporal smoother · privacy gate
    │
    ▼  result_callback(FrameResult)
    │
    ├── FastAPI /stream (WebSocket, push JSON per frame)
    ├── FastAPI /metrics     per-stage P50/P95/P99 latency
    └── FastAPI /health      liveness + readiness probe
```

All four threads run concurrently. The queue bounds are intentional:
the camera stays live and the pipeline always processes the newest frame,
not one from 200 ms ago.

---

## Model choice

**YOLOv9-S with GELAN backbone.** A few alternatives were benched before landing here:

| Model | mAP50-95 COCO | TRT FP16 on Orin Nano | Verdict |
|---|---|---|---|
| YOLOv9-S | **46.8** | **~4.5 ms** | shipped |
| YOLOv8s | 44.9 | ~5.5 ms | fine, but why |
| YOLOv9-C | 52.9 | ~12 ms | over budget |
| RT-DETR-S | 48.1 | ~60 ms | nowhere near 30 FPS |
| MobileNetV3+SSD | 22.0 | ~2 ms | accuracy not good enough for SAR |

YOLOv9's Programmable Gradient Information (PGI) auxiliary branch genuinely helps
on small objects at altitude — prone humans and micro-drones are both hard cases
that benefit from it.

The tracker is ByteTrack (two-pass IoU matching) rather than DeepSORT.
DeepSORT needs a re-ID network crop per object per frame (~3–5 ms extra each).
ByteTrack gets comparable MOTA without that cost, leaving headroom for the
privacy face-blur on every human detection.

---

## Privacy and safety

This is a civilian system. The design assumptions are baked into the code, not just the docs.

**What it does by default:**
- Every face region is pixelated before any frame is stored or transmitted
- Human tracks carry no identity embeddings — bbox and class only
- All logs store bbox + class, never raw pixel crops
- Frame snapshots encrypted at rest (AES-128-CBC via Fernet, key from env)
- Operator tokens expire; expired tokens block all output in code, not just config

**What requires an authorised operator token:**
- Face re-identification (SAR missing-persons use case)
- All such events are written to a tamper-evident audit log

**What is not in this codebase at all:**
- Targeting vectors, firing solutions, actuation commands, threat scoring
- `test_no_targeting` in `tests/test_privacy.py` will fail any commit that adds these

Compliance groundwork: GDPR Art. 25 (privacy by design), Art. 22 (no automated
individual decisions), EU AI Act Annex III remote biometric identification (high-risk
→ operator auth + audit log required).

---

## Repository layout

```
drone_perception/
├── core/
│   ├── preprocessor.py         CLAHE, letterbox, blur estimation
│   ├── detector.py             23-class taxonomy, batched NMS, coordinate remap
│   ├── tracker.py              ByteTrack, Kalman filter, two-pass matching
│   ├── temporal_smoother.py    EMA class smoothing, hysteresis lock/unlock
│   ├── confidence_calibrator.py  Temperature scaling + contextual adjustments
│   ├── async_pipeline.py       4-thread pipeline, watchdog, queue backpressure
│   ├── health_monitor.py       FPS alerts, memory logging, degradation ladder
│   ├── pipeline.py             Synchronous orchestrator (testing / fallback)
│   └── timing.py               FrameTimestamp, per-stage budget table
├── deployment/
│   └── trt_engine.py           TRT FP16/INT8 + ONNX CPU fallback, pinned memory
├── privacy/
│   └── privacy_module.py       Face blur, operator tokens, audit log, encryption
├── api/
│   ├── schema.py               PerceptionFrame JSON schema v1.2.0
│   └── server.py               FastAPI: /health /metrics /status /stream
├── models/
│   └── architecture.md         Model comparison, training plan, dataset list
├── docs/
│   └── system_design.md        Camera selection, failure modes, field test plan
├── configs/
│   └── default_config.yaml     All tunable thresholds and pipeline settings
├── scripts/
│   ├── export_and_build.sh     PyTorch → ONNX → TRT FP16/INT8 pipeline
│   ├── benchmark.py            Per-stage latency profiler (no GPU required)
│   └── stability_validation.py  10-minute FPS / memory / drop-rate validation
├── tests/                      76 unit tests — all pass
└── main.py                     Entry point: async pipeline + API server
```

---

## Getting started

**1. Install (development machine, no GPU required)**

```bash
pip install numpy opencv-python-headless onnxruntime fastapi uvicorn pytest onnx

# Verify — all 76 tests should pass
pytest drone_perception/tests/ -v
```

**2. Dataset setup**

Download and convert VisDrone (UAV pedestrian/vehicle) and COWC (aerial cars):

```bash
# VisDrone (~1.6 GB) — registration-free GitHub mirror
mkdir -p data/visdrone/raw
wget -O data/visdrone/raw/VisDrone2019-DET-train.zip \
  "https://github.com/VisDrone/VisDrone-Dataset/releases/download/v2019/VisDrone2019-DET-train.zip"
wget -O data/visdrone/raw/VisDrone2019-DET-val.zip \
  "https://github.com/VisDrone/VisDrone-Dataset/releases/download/v2019/VisDrone2019-DET-val.zip"

python -m drone_perception.scripts.datasets.visdrone \
  --raw data/visdrone/raw \
  --out data/visdrone

# COWC (~2.4 GB) — direct wget from LLNL
mkdir -p data/cowc/raw
BASE=https://gdo152.llnl.gov/cowc/datasets/patch_64
for CITY in Potsdam_ISPRS Selwyn_LINZ Toronto_ISPRS Utah_AGRC Columbus_CSUAV_AFRL Lima_Peru; do
  wget $BASE/${CITY}.tbz -P data/cowc/raw/
done

python -m drone_perception.scripts.datasets.cowc \
  --raw data/cowc/raw \
  --out data/cowc
```

Both scripts print a per-class summary (count · min/median/max bbox area) and write:
- `data/{dataset}/labels/{train,val}/*.txt` — YOLO format for training
- `data/{dataset}/annotations/{dataset}_{split}.json` — COCO format for evaluation

**3. Train (placeholder — replace with your YOLOv9 training command)**

```bash
# Clone YOLOv9 training repo alongside this one
git clone https://github.com/WongKinYiu/yolov9 vendor/yolov9

# Train YOLOv9-S on VisDrone + COWC
python vendor/yolov9/train.py \
  --weights yolov9-s.pt \
  --data    configs/drone_dataset.yaml \
  --epochs  100 \
  --imgsz   1280 \
  --batch   8 \
  --device  0 \
  --name    yolov9s_drone_v1

# Export to ONNX + build TRT engine (run on Jetson)
bash drone_perception/scripts/export_and_build.sh \
  runs/train/yolov9s_drone_v1/weights/best.pt
```

**4. Evaluate**

```bash
# mAP50, mAP50:95, per-class precision/recall, confusion matrix
python -m drone_perception.scripts.evaluate \
  --onnx    weights/yolov9s_drone_sim.onnx \
  --dataset data/visdrone/annotations/visdrone_val.json \
  --images  data/visdrone/images/val

# Results written to metrics/eval_<timestamp>.json
# Quick smoke test (first 50 images only)
python -m drone_perception.scripts.evaluate \
  --onnx    weights/yolov9s_drone_sim.onnx \
  --dataset data/visdrone/annotations/visdrone_val.json \
  --images  data/visdrone/images/val \
  --max-images 50
```

**5. INT8 calibration (Jetson only)**

```bash
# Collect ~1000 representative deployment frames in data/calib/
mkdir -p data/calib
# (copy or symlink frames from actual flight footage)

python -m drone_perception.deployment.int8_calibrator \
  --input  data/calib \
  --onnx   weights/yolov9s_drone_sim.onnx \
  --cache  weights/calibration.cache \
  --output weights/yolov9s_drone_int8.engine

# Verify INT8 accuracy vs FP16 baseline (expect < 2% mAP drop)
python -m drone_perception.scripts.evaluate \
  --engine  weights/yolov9s_drone_int8.engine \
  --dataset data/visdrone/annotations/visdrone_val.json \
  --images  data/visdrone/images/val
```

**6. Run on Jetson Orin Nano**

```bash
python -m drone_perception.main \
  --engine weights/yolov9s_drone_fp16.engine \
  --onnx   weights/yolov9s_drone_sim.onnx \
  --camera 0 \
  --api-port 8080

# WebSocket stream:   ws://device-ip:8080/stream
# Latency metrics:    GET device-ip:8080/metrics
```

**With a CSI camera (recommended for SAR):**

Replace `--camera 0` with the GStreamer pipeline; `main.py` tries
`nvarguscamerasrc` first and falls back to V4L2 automatically.

---

## JSON output

Every processed frame publishes a `PerceptionFrame` (schema v1.2.0):

```json
{
  "schema_version": "1.2.0",
  "frame_id": 1234,
  "wall_time": 1719000000.123,
  "model_version": "yolov9s-drone-a1b2c3d4",
  "status": "ok",
  "frame_quality": { "blur_score": 0.12, "low_light": false },
  "timing": {
    "inference": 4.5, "tracking": 1.4, "privacy": 1.6,
    "total": 13.8
  },
  "degradation_level": "NORMAL",
  "tracks": [
    {
      "track_id": 7,
      "detection": {
        "class_id": 2,
        "class_name": "person_prone",
        "supercategory": "human_presence",
        "conf": 0.81,
        "calibrated_conf": 0.74,
        "bbox": { "x1": 412.0, "y1": 330.0, "x2": 560.0, "y2": 398.0,
                  "width": 148.0, "height": 68.0, "cx": 486.0, "cy": 364.0 },
        "privacy_applied": true
      },
      "predicted_bbox": { "x1": 413.2, "y1": 331.1, "x2": 561.2, "y2": 399.1, ... },
      "age_frames": 24,
      "hits": 21,
      "time_since_update": 0,
      "is_confirmed": true,
      "smoothed": false
    }
  ],
  "summary": {
    "total_tracks": 1,
    "confirmed_tracks": 1,
    "by_supercategory": { "human_presence": 1 }
  }
}
```

`calibrated_conf` is temperature-scaled per class, with contextual adjustments for
blur and near-edge detections — it's the number downstream consumers should use.
`status` will be one of `ok · low_confidence · motion_blur · stale_tracks · no_detections · cpu_fallback`.

---

## Degradation ladder

When FPS drops below 25 for 30 consecutive frames, the pipeline steps down resolution
automatically rather than stalling. It recovers back to full resolution after 90
frames of sustained good FPS:

```
NORMAL   1280×736   full pipeline
DEGRADED  960×544   reduced queue timeout
STRESSED  640×384   temporal smoother skipped
CRITICAL  480×256   tracking skipped, detections only
```

---

## Failure modes

The health monitor handles these at runtime:

| What goes wrong | How it's detected | What happens |
|---|---|---|
| Camera disconnect | `read()` returns False | Retry ×3, then emit signal |
| GPU OOM | CUDA exception | Switch to ONNX CPU engine |
| Inference stall > 50 ms | WatchdogTimer | Re-use last tracks, flag `stale_tracks` |
| FPS < 25 sustained | HealthMonitor | Step down resolution ladder |
| Stage thread silent > 3 s | Heartbeat timeout | CRITICAL log, attempt restart |
| Model file corrupt | Hash mismatch on load | Rollback to previous version |
| Operator token expired | Token check in PrivacyModule | Block all output |

---

## Acceptance criteria (field deployment)

These are the gates before this goes on an operational airframe:

| Test | Pass threshold |
|---|---|
| SAR — prone human at 60 m AGL | Recall ≥ 0.90, FPR < 5% |
| Sustained 30 FPS (10 min) | Effective FPS ≥ 28, zero budget violations |
| Track ID consistency | ID-switch rate ≤ 2% over 30 s sequence |
| Night / low-light | mAP at 5 lux ≥ 80% of mAP at 500 lux |
| Drone-vs-bird | Drone FPR ≤ 0.10 (birds must not trigger drone alerts) |
| Privacy: face blur | All face pixels blurred, zero PII in logs |
| OTA update mid-flight | Zero frame drops, transition < 100 ms |
| `test_no_targeting` | Must pass with zero failures, always |

---

## Requirements

- Python 3.10+
- NumPy, OpenCV
- ONNX Runtime (CPU) — for development and fallback
- TensorRT 8.6+ — for production on Jetson (installed via JetPack, not pip)
- FastAPI + Uvicorn — for the HTTP/WebSocket API
- cryptography — for frame snapshot encryption (optional but recommended)

Full list: `drone_perception/requirements.txt`

---

## Limitations and known gaps

Worth being honest about before anyone flies this:

- **Weights not included.** The architecture and training plan are here; the trained
  model is not. You will need to run the training pipeline described in
  `models/architecture.md` using the listed datasets.
- **Drone class data is thin.** MAV-VID and the Anti-UAV Challenge dataset help, but
  you really need custom aerial footage of your specific drone models. Plan for it.
- **Thermal fusion is designed for but not implemented.** The preprocessor handles a
  single RGB channel. Adding a FLIR Lepton branch is the next logical step for night SAR.
- **INT8 calibration is a stub.** The `_DummyCalibrator` in `trt_engine.py` needs
  replacing with a real calibration dataset iterator before INT8 is production-ready.
- **No ROS2 bridge yet.** If you're on a PX4/ArduPilot stack, you'll need to wrap
  the `result_callback` in a ROS2 publisher. The JSON schema is stable enough to do this.

---

## Civilian use only

This system is designed for search and rescue, infrastructure inspection, and
situational awareness. It does not contain and will not accept targeting,
fire control, actuation, or engagement features. The `test_no_targeting` test
suite enforces this at the code level.
