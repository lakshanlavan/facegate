"""
face_gate.py
------------
Production face gate for CodeGloFix Access Control System.

CPU-optimized flow:
  ① IdentityEngine.analyse_face(frame) runs InsightFace ONCE
  ② FaceGate uses returned bbox/embedding/faces_count
  ③ Size / centre / multi-face validation
  ④ Direct embedding match via IdentityEngine.match_face_embedding()
  ⑤ Gate cooldown prevents repeated recognition spam
  ⑥ GymAccessEngine handles payment/relay decision

This module only handles detection validation and recognition.
Access decision (payment check, door relay) is handled by gym_access.py.

Gate state exposed for main.py annotator:
    gate_state  : "too_far" | "no_face" | "access_granted" | "access_granted_expiring"
                  | "access_denied_expired" | "access_denied_inactive"
                  | "access_denied_unknown" | "access_denied_emergency_lock"
                  | "cooldown" | "off_centre" | "multi_face"
    gate_status : human-readable message for the status bar
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

# ── Tunable constants ──────────────────────────────────────────────────────────

MIN_FACE_AREA_PX: int = 34_000
CENTER_ROI_FRACTION: float = 0.75
GATE_COOLDOWN_SECONDS: float = 5.0

# min_face_area is expressed in pixels calibrated against this reference frame.
# The distance gate scales the threshold by (actual frame px / reference px) so
# the SAME physical face distance triggers at any capture resolution. At 640x480
# the factor is exactly 1.0 → behaviour is identical for existing installs; at
# native 1280x720 the effective threshold auto-scales ~3x, so admin-tuned values
# keep their 640x480 meaning and no re-tuning is required after a res change.
REFERENCE_FRAME_AREA: int = 640 * 480

# ── State labels ──────────────────────────────────────────────────────────────
STATE_NO_FACE          = "no_face"
STATE_TOO_FAR          = "too_far"
STATE_OFF_CENTRE       = "off_centre"
STATE_MULTI_FACE       = "multi_face"
STATE_COOLDOWN         = "cooldown"

# Access states — set by main.py after gym_access.decide()
STATE_GRANTED          = "access_granted"
STATE_GRANTED_EXPIRING = "access_granted_expiring"
STATE_DENIED_EXPIRED   = "access_denied_expired"
STATE_DENIED_INACTIVE  = "access_denied_inactive"
STATE_DENIED_UNKNOWN   = "access_denied_unknown"
STATE_DENIED_EMERGENCY = "access_denied_emergency_lock"

# Legacy alias used in annotation colour logic
STATE_ALREADY_PRESENT  = "access_granted"   # kept for import compat with main.py


class FaceGate:
    """
    Distance/alignment gate for gym access.

    Call process(frame, engine, scenario) once per frame.
    Returns list[dict] for annotation/logging.

    NOTE: This gate only validates face geometry and runs recognition.
    The access decision (payment status, door relay) is done in main.py
    using GymAccessEngine after this gate returns STATE_PENDING_ACCESS_CHECK.
    """

    def __init__(
        self,
        min_face_area: int = MIN_FACE_AREA_PX,
        center_roi: float = CENTER_ROI_FRACTION,
        cooldown_secs: float = GATE_COOLDOWN_SECONDS
    ) -> None:
        self.min_face_area = min_face_area
        self.center_roi = center_roi
        self.cooldown_secs = cooldown_secs

        self._last_status: str = "Waiting..."
        self._last_state: str = STATE_NO_FACE

        # Per-person gate cooldown (prevents relay trigger every frame)
        self._cooldowns: Dict[str, float] = {}

    def process(
        self,
        frame: Any,
        engine: Any,
        scenario: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Gate validation flow:
          - analyse face once
          - validate bbox / multi-face / size / centre
          - match embedding
          - return detection dict for main.py to pass to GymAccessEngine
        """
        fh, fw = frame.shape[:2]

        # ── ① Single face analysis call ───────────────────────────────────────
        face_data = engine.analyse_face(frame)
        if face_data is None:
            return self._result([], STATE_NO_FACE, "Waiting...")

        bbox = face_data["bbox"]
        embedding = face_data["embedding"]
        faces_count = int(face_data.get("faces_count", 1))

        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        area = max(0, (x2 - x1) * (y2 - y1))

        # ── ② Multi-face check ────────────────────────────────────────────────
        if faces_count > 1:
            msg = "One person at a time"
            return self._result([{
                "class_name": "Multiple faces",
                "confidence": 0.0,
                "bbox": bbox,
                "_skip_log": True,
            }], STATE_MULTI_FACE, msg)

        # ── ③ Distance / size check ───────────────────────────────────────────
        # Normalise the pixel threshold to the actual frame size (see
        # REFERENCE_FRAME_AREA). Factor is 1.0 at 640x480 → unchanged behaviour;
        # ~3x at 1280x720 → same physical trigger distance without re-tuning.
        effective_min_area = self.min_face_area * (fw * fh) / REFERENCE_FRAME_AREA
        if area < effective_min_area:
            msg = f"Move closer"
            return self._result([{
                "class_name": "Face detected",
                "confidence": 0.0,
                "bbox": bbox,
                "_skip_log": True,
            }], STATE_TOO_FAR, msg)

        # ── ④ Centre check ────────────────────────────────────────────────────
        if not self._is_centred(cx, cy, fw, fh):
            msg = "Centre your face"
            return self._result([{
                "class_name": "Face detected",
                "confidence": 0.0,
                "bbox": bbox,
                "_skip_log": True,
            }], STATE_OFF_CENTRE, msg)
        # ── ⑤ Embedding match ─────────────────────────────────────────────────
        threshold = float(scenario.get("identity_threshold", 0.40))
        name, confidence = engine.match_face_embedding(embedding, threshold)

        if name == "Unknown":
            # Cooldown for unknown faces, mirroring the per-name gate cooldown
            # below. Without this the unknown branch re-asserts DENIED_UNKNOWN
            # every detection interval (~5x/sec) during partial/intermittent
            # detection. Throttle it with a sentinel key so the denied/unknown
            # feedback fires once and only reappears after cooldown_secs.
            now = time.monotonic()
            last_fired = self._cooldowns.get("__unknown__", 0.0)

            if now - last_fired < self.cooldown_secs:
                # Preserve whatever state was last shown (e.g. Waiting after the
                # face leaves) instead of re-firing the denial.
                return self._result([{
                    "class_name": "Unknown Person",
                    "confidence": 0.0,
                    "bbox": bbox,
                    "_skip_log": True,
                }], self._last_state, self._last_status)

            self._cooldowns["__unknown__"] = now
            return self._result([{
                "class_name": "Unknown Person",
                "confidence": 0.0,
                "bbox": bbox,
                "_skip_log": True,
            }], STATE_DENIED_UNKNOWN, "Unknown person — Please register")

        # ── ⑥ Gate cooldown ───────────────────────────────────────────────────
        now = time.monotonic()
        last_fired = self._cooldowns.get(name, 0.0)

        if now - last_fired < self.cooldown_secs:
            remaining = self.cooldown_secs - (now - last_fired)
            # Preserve whatever state was set by the last access decision
            return self._result([{
                "class_name": name,
                "confidence": round(confidence, 4),
                "bbox": bbox,
                "_skip_log": True,
            }], self._last_state, self._last_status)

        self._cooldowns[name] = now

        # ── Return recognition hit — main.py will call GymAccessEngine ────────
        # State is set to a placeholder; main.py overwrites it after access check
        print(f"[GATE] ✔ Recognised '{name}' area={area}px² sim={confidence:.4f}")

        return self._result([{
            "class_name":  name,
            "confidence":  round(confidence, 4),
            "bbox":        bbox,
            "_skip_log":   False,   # tell main.py to process this
            "_needs_access_check": True,
        }], STATE_GRANTED, f"{name}...")

    def _is_centred(self, cx: float, cy: float, fw: int, fh: int) -> bool:
        margin_x = fw * (1 - self.center_roi) / 2
        margin_y = fh * (1 - self.center_roi) / 2
        return (
            margin_x < cx < fw - margin_x
            and margin_y < cy < fh - margin_y
        )

    def _result(
        self,
        detections: List[Dict[str, Any]],
        state: str,
        msg: str,
    ) -> List[Dict[str, Any]]:
        self._last_state = state
        self._last_status = msg
        for d in detections:
            d["gate_state"] = state
            d["gate_status"] = msg
        return detections
