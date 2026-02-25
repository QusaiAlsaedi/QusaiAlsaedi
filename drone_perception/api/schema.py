"""
JSON output schema for the drone perception API.

Design goals:
  - Every output is self-describing (includes model_version, schema_version)
  - Timestamps use Unix epoch seconds (float) for cross-system compatibility
  - Monotonic timing for in-pipeline latency is kept separate from wall time
  - bbox uses [x1, y1, x2, y2] in absolute pixels (original frame coords)
  - No PII in default output: faces return class "human_presence" only
  - All floating-point values rounded to 4 decimal places for log efficiency

Schema version: 1.2.0
  - 1.0.0: initial
  - 1.1.0: added timing block, status field
  - 1.2.0: added track.smoothed flag, calibrated_conf, degradation_level
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..core.pipeline import FrameResult, PipelineStatus
from ..core.timing import FrameTimestamp


SCHEMA_VERSION = "1.2.0"


# ---------------------------------------------------------------------------
# Serialisable output types
# ---------------------------------------------------------------------------

@dataclass
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    def to_dict(self) -> Dict:
        return {
            "x1": round(self.x1, 1),
            "y1": round(self.y1, 1),
            "x2": round(self.x2, 1),
            "y2": round(self.y2, 1),
            "width":  round(self.width,  1),
            "height": round(self.height, 1),
            "cx":     round(self.cx,     1),
            "cy":     round(self.cy,     1),
        }


@dataclass
class DetectionOutput:
    """
    A single detection entry in the API output.
    IMPORTANT: For human_presence class, bbox is always provided
    but no identity information is included unless authorised.
    """
    class_id:         int
    class_name:       str
    supercategory:    str
    conf:             float          # raw detector confidence
    calibrated_conf:  Optional[float]   # temperature-scaled confidence
    bbox:             BoundingBox
    privacy_applied:  bool           # True if face was blurred in frame

    def to_dict(self) -> Dict:
        return {
            "class_id":        self.class_id,
            "class_name":      self.class_name,
            "supercategory":   self.supercategory,
            "conf":            round(self.conf, 4),
            "calibrated_conf": round(self.calibrated_conf, 4) if self.calibrated_conf else None,
            "bbox":            self.bbox.to_dict(),
            "privacy_applied": self.privacy_applied,
        }


@dataclass
class TrackOutput:
    """
    A tracked object — superset of DetectionOutput with tracking metadata.
    """
    track_id:         int
    detection:        DetectionOutput
    predicted_bbox:   BoundingBox       # Kalman-predicted position this frame
    age_frames:       int               # frames since track was first created
    hits:             int               # confirmed detection matches
    time_since_update: int              # frames since last detection match
    is_confirmed:     bool              # True after min_hits matches
    smoothed:         bool              # True if temporal smoother changed class

    def to_dict(self) -> Dict:
        return {
            "track_id":          self.track_id,
            "detection":         self.detection.to_dict(),
            "predicted_bbox":    self.predicted_bbox.to_dict(),
            "age_frames":        self.age_frames,
            "hits":              self.hits,
            "time_since_update": self.time_since_update,
            "is_confirmed":      self.is_confirmed,
            "smoothed":          self.smoothed,
        }


@dataclass
class PerceptionFrame:
    """
    Top-level API output for one processed frame.

    JSON example:
    {
      "schema_version": "1.2.0",
      "frame_id": 1234,
      "wall_time": 1719000000.123,
      "model_version": "yolov9s-drone-a1b2c3d4",
      "status": "ok",
      "frame_quality": {
        "blur_score": 0.12,
        "low_light": false
      },
      "timing": {
        "capture_to_publish_ms": 15.4,
        "stages_ms": { ... }
      },
      "degradation_level": "NORMAL",
      "tracks": [ ... ],
      "summary": {
        "total_tracks": 3,
        "by_supercategory": { "human_presence": 1, "vehicle": 2 }
      }
    }
    """
    frame_id:          int
    wall_time:         float           # Unix epoch seconds
    model_version:     str
    status:            str
    blur_score:        float
    low_light:         bool
    timing:            Optional[Dict]
    degradation_level: str
    tracks:            List[TrackOutput]

    @property
    def summary(self) -> Dict:
        by_super: Dict[str, int] = {}
        for t in self.tracks:
            s = t.detection.supercategory
            by_super[s] = by_super.get(s, 0) + 1
        return {
            "total_tracks":      len(self.tracks),
            "confirmed_tracks":  sum(1 for t in self.tracks if t.is_confirmed),
            "by_supercategory":  by_super,
        }

    def to_dict(self) -> Dict:
        return {
            "schema_version":    SCHEMA_VERSION,
            "frame_id":          self.frame_id,
            "wall_time":         round(self.wall_time, 6),
            "model_version":     self.model_version,
            "status":            self.status,
            "frame_quality": {
                "blur_score":    round(self.blur_score, 4),
                "low_light":     self.low_light,
            },
            "timing":            self.timing,
            "degradation_level": self.degradation_level,
            "tracks":            [t.to_dict() for t in self.tracks],
            "summary":           self.summary,
        }

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Converter: FrameResult → PerceptionFrame
# ---------------------------------------------------------------------------

def frame_result_to_output(
    result: FrameResult,
    frame_ts: Optional[FrameTimestamp] = None,
    degradation_level: str = "NORMAL",
) -> PerceptionFrame:
    """Convert internal FrameResult to serialisable PerceptionFrame."""

    track_outputs: List[TrackOutput] = []
    for track in result.tracks:
        det = track.detection
        det_out = DetectionOutput(
            class_id=det.class_id,
            class_name=det.class_name,
            supercategory=det.supercategory,
            conf=det.conf,
            calibrated_conf=det.calibrated_conf,
            bbox=BoundingBox(*det.bbox_xyxy),
            privacy_applied=det.privacy_applied,
        )
        pred_bbox = BoundingBox(*track.predicted_bbox)
        smoothed = (
            det.class_id != track.detection.class_id  # detection was modified by smoother
        )
        track_outputs.append(TrackOutput(
            track_id=track.track_id,
            detection=det_out,
            predicted_bbox=pred_bbox,
            age_frames=track.age,
            hits=track.hits,
            time_since_update=track.time_since_update,
            is_confirmed=track.is_confirmed,
            smoothed=smoothed,
        ))

    timing = frame_ts.to_dict()["stages_ms"] if frame_ts else {
        "inference_ms": round(result.inference_ms, 3),
        "pipeline_ms":  round(result.pipeline_ms, 3),
    }

    return PerceptionFrame(
        frame_id=result.frame_id,
        wall_time=time.time(),
        model_version=result.model_version,
        status=result.status.value,
        blur_score=result.blur_score,
        low_light=result.low_light,
        timing=timing,
        degradation_level=degradation_level,
        tracks=track_outputs,
    )
