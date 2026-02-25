# Model Architecture Design

## Option A — Single Multi-Task Detector (Default / Recommended)

### Backbone: YOLOv9-S with GELAN
```
Input 1280×736×3
  └─ GELAN Stem (Conv-BN-SiLU ×3)
  └─ Stage 1: GELAN-C  (stride 2, 64ch)    ← small-object features
  └─ Stage 2: GELAN-C  (stride 2, 128ch)
  └─ Stage 3: GELAN-C  (stride 2, 256ch)
  └─ Stage 4: GELAN-C  (stride 2, 512ch)   ← large-object features
  └─ SPP-CSP neck (3 scales: P3/P4/P5)
  └─ Detection head (23 classes, 3 anchors × 3 strides = 9 prediction layers)
```

**Why YOLOv9-S over alternatives:**

| Model           | Params  | GFLOPs@640 | mAP50-95 COCO | TRT FP16 Orin Nano |
|-----------------|---------|------------|---------------|--------------------|
| YOLOv8s         | 11.2 M  | 28.6       | 44.9          | ~5.5 ms            |
| **YOLOv9-S**    | **7.2 M** | **26.7** | **46.8**      | **~4.5 ms**        |
| YOLOv9-C        | 25.3 M  | 102.8      | 52.9          | ~12 ms (over budget)|
| RT-DETR-S       | 10.0 M  | 36.0       | 48.1          | ~60 ms (too slow)  |
| MobileNetV3+SSD | 5.0 M   | 0.7        | 22.0          | ~2 ms (too low AP) |

YOLOv9-S fits in 4.5 ms, leaving ~10 ms for tracking/privacy/serialisation
within the 33 ms budget at 30 FPS.

**Programmable Gradient Information (PGI) in YOLOv9:**
The auxiliary reversible branch during training creates richer feature
representations for small objects — critical for detecting prone humans
and micro-drones at altitude.

---

## Option B — Two-Stage (for ground-station GPU only)

```
Stage 1: YOLOv9-S → class-agnostic proposals (23 → 4 superclasses)
Stage 2 (per supercategory):
  - Human:   FaceNet-512 embedding (identity, if authorised)
  - Animal:  EfficientNet-B0 classifier (fine-grained species, 120 classes)
  - Vehicle: ResNet-50 Make/Model head (VMMRdb, 9,170 classes)
  - Drone:   Custom CNN (DroneRF + OpenDroneID signature)
```

Total budget on RTX 3090 ground station: ~8 ms
Not feasible on Orin Nano for 30 FPS — use for batch post-processing.

---

## Option C — Tracking + Re-ID (Extension of A)

Add MobileNetV3-Small re-ID head (128-d embeddings) to Option A:
- Trained on: CompCars (vehicles), DJI Fly marketplace images (drones)
- NOT trained on human identity data (privacy-by-design)
- Triggers only when track.time_since_update > 5 (re-association after occlusion)
- Extra cost: ~1.5 ms per re-ID crop on Orin Nano (amortised over 10+ frames)

---

## Training Plan

### Phase 1: Pretrain backbone on COCO (standard)
- Dataset: COCO 2017 train (~118K images)
- Augmentations: Mosaic, MixUp, Copy-Paste, RandomAffine
- 300 epochs, cosine LR decay, AdamW, label smoothing=0.1

### Phase 2: Domain adaptation — aerial perspective
- **Human datasets:**
  - VisDrone2019-DET (UAV, 10k images, pedestrian focus)
  - HERIDAL (rescue workers, SAR context)
  - SARD (Search And Rescue Dataset, synthetic aerial views)
  - Collect: fly drone over consented volunteers in prone/crouching positions

- **Animal datasets:**
  - iNaturalist (aerial subset, mammals + birds, 675K images)
  - CUB-200-2011 (bird fine-grained, 200 species)
  - Custom: consented aerial wildlife survey footage

- **Vehicle datasets:**
  - COWC (Cars Overhead With Context, aerial, 32K labeled)
  - VEDAI (vehicle detection in aerial imagery)
  - VMMRdb (make/model recognition, 9,170 classes) — crop-level
  - UA-DETRAC (ground-level, for pre-training vehicle classifier)

- **Drone datasets:**
  - MAV-VID (micro aerial vehicles, 53K frames)
  - DroneRF (RF signatures, not visual — for fusion only)
  - Anti-UAV Challenge dataset (thermal + RGB)
  - Custom: photograph/video 15+ drone models at 30–150 m range
    (DJI Mini 4 Pro, Mavic 3, Air 2S, M300, Autel EVO, Skydio 2,
     Parrot ANAFI, custom FPV, fixed-wing trainer)

### Phase 3: Fine-tune with domain-specific augmentation
```python
augmentation_pipeline = [
    # Perspective / altitude simulation
    RandomAffine(degrees=30, translate=0.1, scale=(0.3, 1.5), shear=5),
    # Lighting variation
    ColorJitter(brightness=0.6, contrast=0.6, saturation=0.4, hue=0.1),
    # Atmospheric simulation
    RandomFog(fog_coef=(0.1, 0.5)),
    RandomSunFlare(num_flares=1, src_radius=200),
    # Motion blur (simulate drone vibration + fast movement)
    MotionBlur(blur_limit=(3, 15), p=0.4),
    # Compression artefacts (common in drone video streams)
    ImageCompression(quality_lower=60, quality_upper=100, p=0.3),
    # Noise (sensor noise in low light)
    GaussNoise(var_limit=(10, 50), p=0.3),
    # Horizontal flip (no semantic meaning change for aerial)
    HorizontalFlip(p=0.5),
    # Scale-aware cutout (simulate partial occlusion)
    CoarseDropout(max_holes=8, max_height=64, max_width=64, p=0.3),
]
```

### Labeling Strategy
1. **Tool**: CVAT (self-hosted) or Label Studio for aerial annotation
2. **Format**: YOLO format (.txt) with class index + bbox
3. **Quality gates:**
   - Inter-annotator agreement (Cohen's kappa) > 0.85 before acceptance
   - Minimum bounding box area: 6×6 pixels (discard smaller — unreliable)
   - Ambiguous cases (< 4 px face, drone > 80% occluded): label as "uncertain"
     and exclude from evaluation (but include in training with lower weight)
4. **Active learning loop:**
   - Run inference on unlabeled pool → flag high-entropy predictions
   - Send top-N uncertain frames to annotators each week
   - Reduces labeling cost by ~60% compared to random sampling

### Bias Mitigation
- Ensure geographic diversity: tropics, arctic, urban, rural, coastal
- Ensure demographic diversity for human presence detection
  (skin tone, body type, clothing) — use FairFace + WIDER FACE for face validation
- Drone class balance: micro-drones are underrepresented — oversample 3×
- Validate mAP separately per demographic group; flag >5% gap for re-training

---

## Metrics & Acceptance Criteria

| Metric           | Human Presence | Animal | Vehicle | Drone |
|------------------|---------------|--------|---------|-------|
| mAP@0.5          | ≥ 0.72        | ≥ 0.65 | ≥ 0.78  | ≥ 0.70|
| mAP@0.5:0.95     | ≥ 0.45        | ≥ 0.40 | ≥ 0.55  | ≥ 0.45|
| Recall (SAR)     | ≥ 0.90        | —      | —       | —     |
| False Pos. Rate  | ≤ 0.08        | ≤ 0.12 | ≤ 0.08  | ≤ 0.10|
| ID-switch rate   | ≤ 2%          | ≤ 3%   | ≤ 1.5%  | ≤ 2%  |
| Inference P95    | ≤ 6 ms (TRT FP16)                        |
| Pipeline P95     | ≤ 30 ms (end-to-end at 30 FPS)           |
