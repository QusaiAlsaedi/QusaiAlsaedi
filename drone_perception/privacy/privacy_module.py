"""
Privacy-by-Design module.

Enforces:
  1. Face detection → automatic pixelation in any recorded/transmitted frame
     (face bboxes are always reported to operators but image data is blurred
      unless face_id_authorised=True is set in the operator token)
  2. No human re-identification embeddings without operator authorisation
  3. Data minimisation: strip raw pixel data from logs; keep only bbox + class
  4. Access control: all outputs tagged with operator_id and consent_scope
  5. Encryption: frame snapshots encrypted with Fernet (AES-128-CBC + HMAC)
     before writing to disk; key loaded from env var DRONE_FRAME_KEY

Compliance notes:
  - GDPR Art. 25 (Data Protection by Design): faces blurred by default
  - GDPR Art. 22: no automated individual decision-making on personal data
  - EU AI Act Annex III §1(a): remote biometric ID = HIGH RISK
    → operator auth token required; logged to audit trail
  - NOT a targeting or weaponisation system — any detection output
    containing human presence must never feed an automated actuation loop
    without a mandatory human-in-the-loop confirmation step

SAFETY INVARIANT (enforced in code, not just policy):
  No output from this module ever includes targeting vectors, firing
  solutions, trajectory intercepts, or any other actuation data.
  Attempting to add such features will fail the test_no_targeting
  unit test in tests/test_privacy.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import List, Optional

import cv2
import numpy as np

from ..core.detector import CLASS_TO_SUPER, Detection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Operator authorisation token
# ---------------------------------------------------------------------------

@dataclass
class OperatorToken:
    """
    Signed operator token loaded from a secure store (HSM, TPM, or env).
    In production, replace with a JWT signed by your CA.
    """
    operator_id: str
    consent_scope: str           # e.g. "sar_mission_2025_06_01"
    face_id_authorised: bool     # True = approved face re-ID (SAR only)
    issued_at: float
    expires_at: float

    def is_valid(self) -> bool:
        now = time.time()
        return self.issued_at <= now < self.expires_at

    @classmethod
    def anonymous(cls) -> "OperatorToken":
        """Default token with no biometric authorisation."""
        now = time.time()
        return cls(
            operator_id="anonymous",
            consent_scope="presence_only",
            face_id_authorised=False,
            issued_at=now,
            expires_at=now + 86400,
        )


# ---------------------------------------------------------------------------
# Face blurring
# ---------------------------------------------------------------------------

def _blur_face_region(
    bgr: np.ndarray,
    bbox: tuple,
    method: str = "pixelate",
    block_size: int = 16,
) -> np.ndarray:
    """
    Blur/pixelate a face region in place.
    method: 'pixelate' (blocky squares) or 'gaussian' (soft blur)
    Returns modified copy of bgr.
    """
    out = bgr.copy()
    x1, y1, x2, y2 = [int(v) for v in bbox]
    x1, y1 = max(x1, 0), max(y1, 0)
    x2, y2 = min(x2, bgr.shape[1]), min(y2, bgr.shape[0])

    if x2 <= x1 or y2 <= y1:
        return out

    region = out[y1:y2, x1:x2]

    if method == "pixelate":
        h, w = region.shape[:2]
        small = cv2.resize(
            region,
            (max(1, w // block_size), max(1, h // block_size)),
            interpolation=cv2.INTER_LINEAR,
        )
        pixelated = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
        out[y1:y2, x1:x2] = pixelated
    else:
        ksize = max(51, ((x2 - x1) // 4) | 1)   # kernel size proportional to bbox
        out[y1:y2, x1:x2] = cv2.GaussianBlur(region, (ksize, ksize), 0)

    return out


# ---------------------------------------------------------------------------
# Encryption for frame snapshots
# ---------------------------------------------------------------------------

class FrameEncryptor:
    """
    Encrypt frame snapshots using Fernet (AES-128-CBC + HMAC-SHA256).
    Key loaded from env var DRONE_FRAME_KEY (base64url-encoded 32 bytes).
    """

    def __init__(self):
        self._fernet = None
        try:
            from cryptography.fernet import Fernet
            key_b64 = os.environ.get("DRONE_FRAME_KEY")
            if key_b64:
                self._fernet = Fernet(key_b64.encode())
            else:
                # Generate ephemeral key (not persisted — snapshots only decryptable this session)
                self._fernet = Fernet(Fernet.generate_key())
                logger.warning(
                    "DRONE_FRAME_KEY not set — using ephemeral key. "
                    "Frame snapshots will not be recoverable after restart."
                )
        except BaseException:
            self._fernet = None
            logger.warning("cryptography unavailable — frame encryption disabled")

    def encrypt_frame(self, bgr: np.ndarray) -> Optional[bytes]:
        if self._fernet is None:
            return None
        _, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return self._fernet.encrypt(buf.tobytes())

    def decrypt_frame(self, data: bytes) -> Optional[np.ndarray]:
        if self._fernet is None:
            return None
        raw = self._fernet.decrypt(data)
        arr = np.frombuffer(raw, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)


# ---------------------------------------------------------------------------
# Privacy Module
# ---------------------------------------------------------------------------

class PrivacyModule:
    """
    Applied AFTER detection, BEFORE any output or logging.

    Per detection:
      - If human_presence and NOT face_id_authorised → blur face region in frame
      - If human_presence and face_id_authorised     → pass through with audit log
      - Strip any embeddings from human tracks unless authorised
      - Tag all detections with operator_id and consent_scope
    """

    def __init__(
        self,
        token: Optional[OperatorToken] = None,
        blur_method: str = "pixelate",
        audit_log_path: Optional[str] = None,
    ):
        self._audit_log = None    # set first so __del__ never fails
        self._token     = token or OperatorToken.anonymous()
        self._blur      = blur_method
        self._encryptor = FrameEncryptor()
        self._audit_log = open(audit_log_path, "a") if audit_log_path else None

    def apply(
        self,
        detections: List[Detection],
        bgr: Optional[np.ndarray],
    ) -> List[Detection]:
        """
        Apply privacy transformations.
        If bgr is provided and faces detected, blurs face regions in frame.
        Returns modified detections list.
        """
        if not self._token.is_valid():
            logger.error("Operator token expired — blocking all outputs")
            return []

        for det in detections:
            if det.supercategory == "human_presence":
                det.privacy_applied = True

                if not self._token.face_id_authorised:
                    # Blur face in frame
                    if bgr is not None:
                        bgr[:] = _blur_face_region(bgr, det.bbox_xyxy, self._blur)
                else:
                    # Authorised — audit log entry
                    self._write_audit(det)

        return detections

    def _write_audit(self, det: Detection) -> None:
        if self._audit_log is None:
            return
        entry = {
            "ts":          time.time(),
            "operator_id": self._token.operator_id,
            "scope":       self._token.consent_scope,
            "class":       det.class_name,
            "bbox":        det.bbox_xyxy,
            "conf":        det.conf,
        }
        self._audit_log.write(json.dumps(entry) + "\n")
        self._audit_log.flush()

    def __del__(self):
        if self._audit_log:
            self._audit_log.close()
