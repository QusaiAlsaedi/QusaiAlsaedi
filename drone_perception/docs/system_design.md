# Drone Perception System — System Design

## 1. Hardware Platform

### Recommended: Jetson Orin Nano (8 GB)
| Spec               | Value                                      |
|--------------------|--------------------------------------------|
| CPU                | 6-core Arm Cortex-A78AE                    |
| GPU                | 1024-core Ampere, 32 Tensor Cores          |
| DLA                | 2× Deep Learning Accelerators (optional)  |
| Memory             | 8 GB LPDDR5 unified (CPU + GPU share)      |
| TDP                | 7–15 W (configurable MAXN / 10W / 7W modes)|
| TRT FP16 throughput| ~40 TOPS (GPU) + ~26 TOPS (DLA)           |

Alternative: Qualcomm RB5 (Snapdragon 865)
- Hexagon DSP: INT8 optimised, ~15 TOPS
- Trade-off: SNPE SDK instead of TensorRT; less mature tooling
- Recommend if thermal budget is critical (<5 W)

### Camera Selection
| Priority | Camera                   | Interface | FOV    | Notes                          |
|----------|--------------------------|-----------|--------|-------------------------------|
| 1st      | Sony IMX678 (4K, 60 FPS) | MIPI CSI-2| 94°    | Best low-light (1/1.8" sensor)|
| 2nd      | OV9281 (720p, 120 FPS)   | MIPI CSI-2| 120°   | Global shutter, no rolling-blur|
| 3rd      | ArduCam 8 MP IMX219      | MIPI CSI-2| 77°    | Budget option, COTS            |
| Thermal  | FLIR Lepton 3.5          | SPI/I2C   | 57°    | Low-light SAR complement       |

**Recommended for SAR**: OV9281 (global shutter eliminates rolling-shutter
wobble during drone vibration, which causes significant detection degradation)
paired with FLIR Lepton for night missions.

Frame rate vs resolution trade-offs:
- 720p @ 30 FPS: baseline (all classes, full pipeline)
- 1080p @ 30 FPS: requires INT8 or architecture optimisation (not recommended)
- 720p @ 60 FPS: halves effective exposure per frame → worse low-light
- 480p @ 60 FPS: viable for vehicle/drone detection, poor for small humans

---

## 2. Deployment Procedure

### Model Export (development machine)
```bash
# 1. Export YOLOv9-S to ONNX (opset 17, batch=1, dynamic=False)
python export_yolov9.py \
  --weights checkpoints/yolov9s_drone_v1.pt \
  --img 1280 736 \
  --batch 1 \
  --opset 17 \
  --include onnx \
  --output weights/yolov9s_drone.onnx

# 2. (Optional) Simplify ONNX graph
onnxsim weights/yolov9s_drone.onnx weights/yolov9s_drone_sim.onnx

# 3. Build TRT engine on target Jetson (must run ON device)
trtexec \
  --onnx=weights/yolov9s_drone_sim.onnx \
  --saveEngine=weights/yolov9s_drone_fp16.engine \
  --fp16 \
  --workspace=1024 \
  --verbose
```

### INT8 Calibration
```bash
python calibrate_int8.py \
  --onnx weights/yolov9s_drone_sim.onnx \
  --calib-data data/calibration_1000_frames/ \
  --calib-cache weights/calibration.cache \
  --output weights/yolov9s_drone_int8.engine
```

### On-Device Update Strategy
1. Models stored in `/opt/drone_perception/weights/`
2. New model deployed via OTA update script (delta compression)
3. Shadow deployment: new engine runs on duplicate GPU stream for 5 min
4. Automated A/B comparison: if new mAP on reference dataset < 97% of baseline → rollback
5. Engine hash verified against signed manifest before loading (prevent model poisoning)
6. Maximum OTA cadence: 1 update / 7 days (stability requirement)

---

## 3. Logging and Telemetry

### On-Device Logs
```
/var/log/drone_perception/
  perception.jsonl        # rotating JSON log (20 MB × 5 files)
  audit.jsonl             # operator audit trail (face re-ID events)
  metrics.jsonl           # per-frame timing snapshots
```

### Telemetry Fields per Frame
```json
{
  "frame_id": 12345,
  "fps": 29.8,
  "inference_ms": 4.5,
  "pipeline_ms": 14.2,
  "tracks": 3,
  "blur_score": 0.12,
  "low_light": false,
  "status": "ok",
  "degradation_level": "NORMAL",
  "gps": {"lat": 24.7136, "lon": 46.6753, "alt_m": 85.0}
}
```

### Ground Station Integration
- Push telemetry to ground station via MAVLink telemetry channel (1 Hz)
- Full detection JSON via separate 5.8 GHz video link (30 FPS capable)
- Store local ring buffer of last 300 frames for post-flight analysis

---

## 4. Failure Modes and Mitigations

| Failure Mode            | Detection                    | Mitigation                          |
|-------------------------|------------------------------|-------------------------------------|
| Camera disconnect       | `cap.read()` returns False   | Retry 3×, then safe-land signal     |
| GPU OOM                 | CUDA out-of-memory exception | Switch to ONNX CPU engine           |
| Inference timeout >50ms | WatchdogTimer fires          | Re-use last tracks, flag STALE      |
| FPS drops below 25      | HealthMonitor FPS alert      | Reduce resolution (degradation ladder)|
| Stage thread death      | Heartbeat timeout >3s        | CRITICAL alert, attempt restart     |
| Model file corrupted    | Hash mismatch on load        | Rollback to previous version        |
| Operator token expired  | Token validation at runtime  | Block all biometric outputs         |
| Thermal throttle        | tegrastats CPU/GPU temp >80°C| Reduce power mode (10W→7W)          |
| Vibration / blur        | blur_score >0.8              | Flag MOTION_BLUR, halve confidence  |
| Complete darkness       | mean_lum <5, low_light=True  | Enable CLAHE, flag low confidence   |

---

## 5. Field Test Plan

### Lab Tests (controlled environment)
| Test                          | Pass Criteria                              |
|-------------------------------|--------------------------------------------|
| Latency benchmark             | P95 pipeline_ms ≤ 30 ms at 30 FPS         |
| NMS correctness               | Zero duplicate tracks for non-overlapping objects|
| Privacy: face blur            | All face regions blurred, no pixel leak    |
| Safety: no targeting fields   | test_no_targeting passes with 0 failures   |
| Confidence calibration        | ECE (Expected Calibration Error) ≤ 0.05   |
| Track ID consistency          | ID-switch rate ≤ 2% over 30s sequence     |
| Motion blur handling          | Detections at blur_score=0.7 degrade ≤20% |
| Low-light enhancement         | mAP at 5 lux ≥ 80% of mAP at 500 lux     |

### Field Tests (aerial, outdoor)
| Scenario                      | Acceptance Criteria                        |
|-------------------------------|--------------------------------------------|
| SAR — prone human at 60 m    | Recall ≥ 0.90, <5% false positives        |
| SAR — crowded scene (10+)    | All persons detected, ≤1 ID-switch/minute |
| Vehicle convoy (5 vehicles)  | All tracked, correct type classification   |
| Drone-vs-bird                | Drone FPR ≤ 0.10 (birds not flagged)       |
| Night / thermal off          | Recall ≥ 0.75 for large objects            |
| Glare (direct sunlight)      | mAP degradation ≤ 15% vs overcast          |
| Fog (visibility 200 m)       | Detection range degraded gracefully        |
| Fast manoeuvre (5 m/s lateral)| No track ID switches during manoeuvre     |
| OTA model update mid-flight  | Zero frame drops, <100 ms transition       |

### Adversarial Tests
1. **Camouflage clothing**: test recall on military-pattern vs civilian clothing
2. **Partial occlusion**: person 50% behind tree — still detected
3. **Drone spoofing**: large bird with DJI sticker — should not be classified as drone
4. **Compressed video artefacts**: H.264 stream at 2 Mbps — mAP drop ≤ 5%
5. **Strobe lighting**: 10 Hz strobe — pipeline must not crash

---

## 6. Privacy Compliance Checklist

- [x] Faces blurred in all stored/transmitted frames (default)
- [x] No face embeddings computed without explicit operator token
- [x] Operator audit log for all face re-ID events (GDPR Art. 30)
- [x] Data minimisation: only bbox + class stored in logs, not pixel data
- [x] Frame encryption at rest (Fernet AES-128-CBC)
- [x] Token expiry enforced in code (not just policy)
- [x] test_no_targeting safety invariant tests in CI
- [ ] DPIA (Data Protection Impact Assessment) — required before deployment
- [ ] Operator training on consent requirements
- [ ] Signage/notification when drone operates over public areas (local law)
