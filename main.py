import os
"""
main.py — CodeGloFix Access Control System
-----------------------------------------
Face-recognition access control for gyms.

Key implementation notes:
  - attendance_logger replaced with gym_db
  - GymAccessEngine handles payment check + relay door open/deny
  - RelayController (mock mode by default) controls magnetic door
  - _last_detection payload extended: decision, reason, days_left, valid_until, door_opened
  - detection_loop calls gym_access.decide() instead of db.log_detection()
  - New API routes: /api/members, /api/payments, /api/plans, /api/access_logs,
                    /api/door/*, /api/revenue/*, /api/settings/gym
  - Gate annotation colours updated for gym states (green=granted, red=denied, amber=expiring)
  - Work schedule routes removed (not needed for gym)
  - Attendance export replaced with access log export

Preserved unchanged:
  - Camera watchdog / stale-frame detection
  - MJPEG stream
  - Gate config runtime update (/api/gate_config)
  - Enrolment flow (capture/upload/confirm/cancel)
  - Auth system (auth.py)
  - identity_engine.py (no changes needed)
"""

import csv
import io
import json
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Query, Request, Response, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import gym_db as db
from auth import (
    COOKIE_NAME,
    is_authenticated,
    require_admin,
    router as auth_router,
    validate_auth_config,
)
from face_gate import (
    FaceGate,
    STATE_GRANTED, STATE_GRANTED_EXPIRING,
    STATE_DENIED_EXPIRED, STATE_DENIED_INACTIVE,
    STATE_DENIED_UNKNOWN, STATE_DENIED_EMERGENCY,
    STATE_COOLDOWN,
)
from gym_access import GymAccessEngine
from identity_engine import IdentityEngine
from relay_controller import RelayController

# ──────────────────────────────────────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_auth_config()
    db.init_db()
    _load_gate_config()

    # Initialise relay and access engine
    with _gate_config_lock:
        cfg = dict(_gate_config)
    relay = RelayController.from_config(cfg)
    _access_engine_holder["engine"] = GymAccessEngine(relay)
    _access_engine_holder["engine"].load_emergency_lock_from_db()

    get_engine()

    capture_thread = threading.Thread(
        target=_capture_loop, daemon=True, name="camera-capture"
    )
    capture_thread.start()

    t = threading.Thread(target=detection_loop, daemon=True, name="detection-loop")
    t.start()

    maintenance_thread = threading.Thread(
        target=_maintenance_loop,
        daemon=True,
        name="db-maintenance",
    )
    maintenance_thread.start()

    print("[APP] CodeGloFix Access Control System ready — http://localhost:5000")
    if _IS_VIDEO_FILE:
        print(f"[APP] TEST MODE — playing: {_SOURCE}")
    yield


app = FastAPI(title="CodeGloFix Access Control", lifespan=lifespan)
app.include_router(auth_router)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
BASE_DIR = Path(__file__).parent

# Production sets CODEGLOFIX_ENROLMENTS_DIR (e.g. /var/lib/codeglofix/enrolments/shared)
# so face embeddings persist outside the read-only app code. Unset (dev) → the
# bundled enrolments/shared folder, exactly as before. The legacy
# DIGITGYM_ENROLMENTS_DIR name is honoured as an internal migration fallback.
_ENROLMENTS_DIR = (
    os.environ.get("CODEGLOFIX_ENROLMENTS_DIR")
    or os.environ.get("DIGITGYM_ENROLMENTS_DIR")
    or str(BASE_DIR / "enrolments/shared")
)

SCENARIO = {
    "hardware_backend": "cpu",
    "identity_store": "faces",
    "enrolments_dir": _ENROLMENTS_DIR,
    "identity_threshold": 0.60,
    "confidence_threshold": 0.65,
    "camera_source": {"type": "camera"},
}

BLOCKED_NAMES = {"camera", "test", "unknown"}

# Holds the GymAccessEngine instance (created in lifespan after config loads)
_access_engine_holder: dict = {"engine": None}


def get_access_engine() -> GymAccessEngine:
    eng = _access_engine_holder.get("engine")
    if eng is None:
        raise RuntimeError("[APP] GymAccessEngine not initialised")
    return eng


# ──────────────────────────────────────────────────────────────────────────────
# Performance tuning
# ──────────────────────────────────────────────────────────────────────────────

DETECTION_INTERVAL_SECONDS = 0.20
# MJPEG stream pacing/quality. Env-tunable so a low-power kiosk PC can drop the
# outside-display stream to e.g. 5 fps / quality 50 without a code change:
#   STREAM_INTERVAL_SECONDS=0.20  JPEG_QUALITY=50
STREAM_INTERVAL_SECONDS = float(os.environ.get("STREAM_INTERVAL_SECONDS", "0.10"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "65"))
LOOP_SLEEP_SECONDS = 0.005
# Kiosk display canvas — Waveshare 7" HDMI LCD is 1024x600 (17:10). The camera
# stays 640x480 (4:3) for recognition; only the OUTSIDE kiosk stream is composited
# onto this canvas so the 7" screen fills edge-to-edge with no black side bars and
# WITHOUT cropping any camera content (see make_kiosk_frame). Recognition never
# sees this frame. Overridable via env in case a different panel is ever fitted.
KIOSK_DISPLAY_W = int(os.environ.get("KIOSK_DISPLAY_W", "1024"))
KIOSK_DISPLAY_H = int(os.environ.get("KIOSK_DISPLAY_H", "600"))
# Foreground contain-fit scale inside make_kiosk_frame(). 1.0 = fill the canvas as
# tight as contain allows (no visible blurred margin when the camera aspect ≈ the
# canvas aspect, e.g. a 16:9 capture on the 17:10 panel). <1.0 shrinks the sharp
# foreground slightly so the soft, darkened blurred background stays visible as an
# even border on all four sides — the "blurred fill" look — without cropping any
# camera content. 0.88 gives a ~6% border each side. Display-only: recognition
# never sees this frame. Revert to the old edge-to-edge look with KIOSK_FG_SCALE=1.0.
KIOSK_FG_SCALE = float(os.environ.get("KIOSK_FG_SCALE", "0.88"))
# ── Camera capture format (env-configurable) ──────────────────────────────────
# A 16:9 webcam can run native widescreen (1280x720) for a truer 7" kiosk view.
# get_camera() sets FOURCC+size then READS BACK the negotiated size and, if the
# device did not honour the request, FALLS BACK to the proven 640x480 baseline —
# so a camera that ignores the hint never runs at an unexpected size. Recognition
# stays stable because the distance gate is resolution-normalised (face_gate.py
# scales min_face_area by frame pixels vs a 640x480 reference) and the detector
# input is a fixed 320x320 (identity_engine), so detection cost is ~flat.
# Instant revert without a code change: set CAMERA_WIDTH=640 CAMERA_HEIGHT=480.
CAMERA_WIDTH  = int(os.environ.get("CAMERA_WIDTH",  "1280"))
CAMERA_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", "720"))
CAMERA_FPS    = int(os.environ.get("CAMERA_FPS",    "30"))
CAMERA_FOURCC = os.environ.get("CAMERA_FOURCC", "MJPG").strip()
CAMERA_FALLBACK_WIDTH  = 640
CAMERA_FALLBACK_HEIGHT = 480
# Read-back tolerance: accept the requested mode if within ±10% (covers panels
# that snap 1280x720 to a near neighbour like 1184x656).
CAMERA_SIZE_TOLERANCE = 0.10
# Negotiated capture size — filled in by get_camera() from the device read-back.
_actual_cam_w: int = CAMERA_FALLBACK_WIDTH
_actual_cam_h: int = CAMERA_FALLBACK_HEIGHT
# Capture-thread pacing. A live camera blocks inside read() at the sensor's own
# FPS, so the capture loop only needs a negligible yield to avoid a busy-spin if
# a backend ever returns instantly. Video files return frames immediately, so
# they are separately paced to the file's reported FPS (see _capture_loop) to
# keep TEST MODE playing near real time.
CAPTURE_LIVE_SLEEP_SECONDS = 0.001
CAPTURE_NO_FRAME_SLEEP_SECONDS = 0.02   # detection loop wait when no frame yet

MAX_CONSECUTIVE_CAMERA_FAILURES = 20
CAMERA_RECONNECT_BACKOFF_SECONDS = 1.0

STALE_FRAME_THRESHOLD = 150
TOTAL_FAILURE_TIMEOUT_SECONDS = 60.0

# ──────────────────────────────────────────────────────────────────────────────
# Gate config — mutable at runtime via /api/gate_config
# ──────────────────────────────────────────────────────────────────────────────

_gate_config: dict = {
    "min_face_area":          34_000,   # matches MIN_FACE_AREA_PX in face_gate.py
    "center_roi":             0.75,
    "cooldown_secs":          5.0,
    "relay_port":             "auto",
    "relay_baudrate":         115200,
    "relay_auto_connect":     True,
    "expiry_warning_days":    7,
    "allow_expiring_members": True,
    "emergency_lock":         False,
}
_gate_config_lock = threading.Lock()
# gate_config.json is rewritten at runtime via /api/gate_config, so in production
# it must live in a writable persistent dir (the app code dir is root-owned/RO).
# CODEGLOFIX_GATE_CONFIG points it at /var/lib/codeglofix/gate_config.json there;
# unset (dev) → next to the source as before. The legacy DIGITGYM_GATE_CONFIG name
# is honoured as an internal migration fallback.
_GATE_CONFIG_FILE = Path(
    os.environ.get("CODEGLOFIX_GATE_CONFIG")
    or os.environ.get("DIGITGYM_GATE_CONFIG")
    or str(BASE_DIR / "gate_config.json")
)


def _load_gate_config() -> None:
    if not _GATE_CONFIG_FILE.exists():
        return
    try:
        data = json.loads(_GATE_CONFIG_FILE.read_text())
        with _gate_config_lock:
            for key in _gate_config:
                if key in data:
                    _gate_config[key] = data[key]
        print(f"[CONFIG] Gate config loaded: {dict(_gate_config)}")
    except Exception as e:
        print(f"[CONFIG] Could not load gate_config.json — using defaults: {e}")


def _save_gate_config() -> None:
    """Atomically persist gate_config.json so power loss cannot corrupt it."""
    try:
        with _gate_config_lock:
            data = dict(_gate_config)
        tmp = _GATE_CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_GATE_CONFIG_FILE)
        print(f"[CONFIG] Gate config saved: {data}")
    except Exception as e:
        print(f"[CONFIG] Could not save gate_config.json: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Source config
# ──────────────────────────────────────────────────────────────────────────────

import os as _os_mod


def _parse_video_source(raw_value: str):
    """
    Parse VIDEO_SOURCE safely.

    Supported values:
      - "0", "1", "2", "3"       → webcam index for OpenCV
      - "/dev/video3"             → Linux camera device path
      - "/path/to/test.mp4"        → video file test mode
      - RTSP/HTTP camera URL        → camera stream

    The previous logic treated every non-numeric value as a video file.
    That caused /dev/video3 to be handled as TEST MODE. This parser keeps
    Linux video devices and camera URLs in webcam/stream mode.
    """
    value = (raw_value or "0").strip()
    lower = value.lower()

    if value.isdigit():
        return int(value), False, "WEBCAM"

    if lower.startswith("/dev/video"):
        return value, False, "WEBCAM"

    if lower.startswith(("rtsp://", "rtmp://", "http://", "https://")):
        return value, False, "STREAM"

    return value, True, "VIDEO FILE"


_VIDEO_SOURCE_ENV = _os_mod.environ.get("VIDEO_SOURCE", "0")
_SOURCE, _IS_VIDEO_FILE, _SOURCE_MODE_LABEL = _parse_video_source(_VIDEO_SOURCE_ENV)

print(f"[APP] Source mode : {_SOURCE_MODE_LABEL}")
print(f"[APP] Source value: {_SOURCE}")

# ──────────────────────────────────────────────────────────────────────────────
# Shared state
# ──────────────────────────────────────────────────────────────────────────────

_engine: Optional[IdentityEngine] = None
_engine_lock = threading.Lock()

_camera: Optional[cv2.VideoCapture] = None
_cap_lock = threading.Lock()

# Newest-frame capture buffer. The dedicated _capture_loop thread is the sole
# producer (reads the camera continuously); detection_loop is the consumer. Only
# the single most-recent frame is kept — never a growing queue — so the consumer
# never replays stale backlog and memory stays flat. _capture_seq increments on
# every new frame so the consumer can tell a fresh frame from a repeat poll.
_capture_frame = None                          # latest np.ndarray frame (or None)
_capture_seq: int = 0
_capture_ok: bool = False
_capture_lock = threading.Lock()

_latest_frame: Optional[bytes] = None          # annotated frame for live gate page
_latest_enrol_frame: Optional[bytes] = None    # clean frame for enrolment page/capture
_latest_kiosk_frame: Optional[bytes] = None    # annotated frame composited to 1024x600 for the 7" kiosk
_frame_lock = threading.Lock()

_detection_active = threading.Event()
_detection_active.set()

_gate_status: str = "Waiting..."
_gate_status_lock = threading.Lock()

_last_detection: dict = {}
_last_detection_lock = threading.Lock()

_enrol_sessions: dict = {}
_enrol_lock = threading.Lock()

_last_gate_detections = []
_last_gate_detections_lock = threading.Lock()

_db_result_override: dict = {}
_db_result_lock = threading.Lock()

_gate_reset_requested = threading.Event()

_watchdog_last_fresh: float = 0.0
_watchdog_lock = threading.Lock()

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _normalise_name(name: str) -> str:
    return " ".join(name.strip().split())


def _cv_safe_text(text) -> str:
    """
    Convert text to OpenCV-safe ASCII before drawing it with cv2.putText.

    OpenCV's built-in Hershey fonts do not reliably render Unicode symbols
    such as em dashes, bullets, arrows and tick marks. Without this conversion,
    labels like "lasi — No payment record" can appear on the video as
    "lasi ??? No payment record".
    """
    text = str(text)
    replacements = {
        "—": "-",
        "–": "-",
        "−": "-",
        "·": "-",
        "•": "-",
        "→": "->",
        "←": "<-",
        "↔": "<->",
        "✓": "OK",
        "✔": "OK",
        "✅": "OK",
        "⚠": "!",
        "❌": "X",
    }
    for old, replacement in replacements.items():
        text = text.replace(old, replacement)
    return text.encode("ascii", "ignore").decode("ascii")


def _validate_enrolment_name(name: str) -> str:
    name = _normalise_name(name)
    if not name:
        raise HTTPException(400, "Name cannot be empty")
    if name.lower() in BLOCKED_NAMES:
        raise HTTPException(400, "This name is not allowed for enrolment.")
    return name


def _check_name_not_already_enrolled(name: str) -> None:
    """
    Raise HTTPException 409 if the name is already enrolled as an active member.
    Called at capture-time AND confirm-time to give early feedback.
    """
    status = db.member_name_check(name)
    if status == "active":
        raise HTTPException(
            409,
            f"'{name}' is already enrolled as an active member. "
            "Remove them first before re-enrolling."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Engine / camera helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_engine() -> IdentityEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = IdentityEngine(SCENARIO)
    return _engine


def _apply_camera_format(cam: cv2.VideoCapture, w: int, h: int,
                         fps: int, fourcc: Optional[str]) -> None:
    """Apply FOURCC → size → fps → buffersize to an open capture.

    Order matters on V4L2: the pixel format (MJPG) must be set BEFORE the frame
    size so high-res compressed modes become selectable. BUFFERSIZE=1 keeps only
    the newest frame so read() never returns a stale queued frame (low live lag);
    backends that ignore this hint are unaffected.
    """
    if fourcc:
        try:
            cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        except Exception as e:
            print(f"[CAM] FOURCC '{fourcc}' not applied ({e}) — using driver default")
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    if fps:
        cam.set(cv2.CAP_PROP_FPS, fps)
    cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)


def get_camera() -> cv2.VideoCapture:
    global _camera, _actual_cam_w, _actual_cam_h
    if _camera is None or not _camera.isOpened():
        _camera = cv2.VideoCapture(_SOURCE)
        if not _IS_VIDEO_FILE:
            # 1. Request the configured (default 1280x720 MJPG) mode.
            _apply_camera_format(_camera, CAMERA_WIDTH, CAMERA_HEIGHT,
                                 CAMERA_FPS, CAMERA_FOURCC or None)
            aw = int(_camera.get(cv2.CAP_PROP_FRAME_WIDTH)  or 0)
            ah = int(_camera.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            # 2. Accept only if the device honoured it within tolerance; else fall
            #    back to the proven 640x480 baseline so we never run at an
            #    unexpected size a webcam silently substituted.
            ok_w = abs(aw - CAMERA_WIDTH)  <= CAMERA_WIDTH  * CAMERA_SIZE_TOLERANCE
            ok_h = abs(ah - CAMERA_HEIGHT) <= CAMERA_HEIGHT * CAMERA_SIZE_TOLERANCE
            if not (aw > 0 and ah > 0 and ok_w and ok_h):
                print(f"[CAM] Requested {CAMERA_WIDTH}x{CAMERA_HEIGHT} not honoured "
                      f"(device reported {aw}x{ah}) — falling back to "
                      f"{CAMERA_FALLBACK_WIDTH}x{CAMERA_FALLBACK_HEIGHT}")
                _apply_camera_format(_camera, CAMERA_FALLBACK_WIDTH,
                                     CAMERA_FALLBACK_HEIGHT, CAMERA_FPS, None)
                aw = int(_camera.get(cv2.CAP_PROP_FRAME_WIDTH)  or CAMERA_FALLBACK_WIDTH)
                ah = int(_camera.get(cv2.CAP_PROP_FRAME_HEIGHT) or CAMERA_FALLBACK_HEIGHT)
            _actual_cam_w, _actual_cam_h = aw, ah
            print(f"[CAM] Capture resolution: {aw}x{ah} "
                  f"(requested {CAMERA_WIDTH}x{CAMERA_HEIGHT}, fourcc '{CAMERA_FOURCC}')")
        if not _camera.isOpened():
            print(f"[CAM] WARNING: Cannot open source: {_SOURCE}")
    return _camera


def reset_camera() -> None:
    global _camera
    with _cap_lock:
        if _camera is not None:
            try:
                _camera.release()
            except Exception:
                pass
        _camera = None
    print("[CAM] Camera handle reset")


# ──────────────────────────────────────────────────────────────────────────────
# Camera capture thread  (producer — keeps only the newest frame)
# ──────────────────────────────────────────────────────────────────────────────

def _capture_loop() -> None:
    """
    Dedicated camera reader.

    Continuously pulls frames from the source and publishes ONLY the newest one
    so the detection/recognition/JPEG loop never blocks waiting on read(). This
    removes the live-stream "tiny stuck / late-frame" lag that built up while a
    single thread paused reads during the ~0.2s recognition + JPEG-encode steps:
    the camera no longer queues stale frames behind a busy consumer.

    This thread owns the camera lifecycle — open, EOF-looping for video files,
    and live-camera read-failure handling/reconnect/self-restart — exactly the
    behaviour that previously lived inline in detection_loop, unchanged.
    """
    global _capture_frame, _capture_seq, _capture_ok

    consecutive_camera_failures = 0
    _camera_dead_since = 0.0

    while True:
        try:
            with _cap_lock:
                cam = get_camera()
                ok, frame = cam.read()
                if not ok and _IS_VIDEO_FILE:
                    cam.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = cam.read()

            if ok and (frame is None or frame.size == 0 or len(frame.shape) < 3):
                ok = False

            if not ok:
                consecutive_camera_failures += 1
                print(f"[CAM] Read failed ({consecutive_camera_failures}/{MAX_CONSECUTIVE_CAMERA_FAILURES})")
                if _camera_dead_since == 0.0:
                    _camera_dead_since = time.monotonic()
                dead_for = time.monotonic() - _camera_dead_since
                if dead_for >= TOTAL_FAILURE_TIMEOUT_SECONDS:
                    print(f"[CAM] Unrecoverable for {dead_for:.0f}s — self-restart")
                    os._exit(1)
                with _capture_lock:
                    _capture_ok = False
                if consecutive_camera_failures >= MAX_CONSECUTIVE_CAMERA_FAILURES:
                    reset_camera()
                    consecutive_camera_failures = 0
                    time.sleep(CAMERA_RECONNECT_BACKOFF_SECONDS)
                else:
                    time.sleep(0.5)
                continue

            consecutive_camera_failures = 0
            _camera_dead_since = 0.0

            # Publish the newest frame (replace, never append → no queue growth).
            with _capture_lock:
                _capture_frame = frame
                _capture_seq += 1
                _capture_ok = True

            # Pace playback. A live camera already blocked in read() at sensor
            # FPS, so only a tiny yield is needed. A video file returns instantly,
            # so pace it to its own FPS to keep TEST MODE near real time.
            if _IS_VIDEO_FILE:
                fps = cam.get(cv2.CAP_PROP_FPS)
                time.sleep(1.0 / fps if fps and fps > 1.0 else 0.033)
            else:
                time.sleep(CAPTURE_LIVE_SLEEP_SECONDS)

        except Exception as e:
            print(f"[CAPTURE LOOP] Unexpected error: {e} — continuing")
            time.sleep(0.5)


# ──────────────────────────────────────────────────────────────────────────────
# Detection background thread
# ──────────────────────────────────────────────────────────────────────────────

def detection_loop() -> None:
    global _latest_frame, _latest_enrol_frame, _latest_kiosk_frame, _gate_status, _last_detection, _db_result_override

    eng = get_engine()
    access_engine = get_access_engine()

    with _gate_config_lock:
        cfg = dict(_gate_config)
    gate = FaceGate(
        min_face_area=cfg["min_face_area"],
        center_roi=cfg["center_roi"],
        cooldown_secs=cfg["cooldown_secs"],
    )

    last_detect_time = 0.0

    _last_frame_hash: int = -1
    _stale_count: int = 0
    _stale_since: float = time.monotonic()
    _last_capture_seq: int = -1   # last frame seq consumed from the capture thread

    with _watchdog_lock:
        global _watchdog_last_fresh
        _watchdog_last_fresh = time.monotonic()

    while True:
        try:
            # ── Gate config reset ─────────────────────────────────────────────
            if _gate_reset_requested.is_set():
                with _gate_config_lock:
                    cfg = dict(_gate_config)
                gate = FaceGate(
                    min_face_area=cfg["min_face_area"],
                    center_roi=cfg["center_roi"],
                    cooldown_secs=cfg["cooldown_secs"],
                )
                last_detect_time = 0.0
                with _gate_status_lock:
                    _gate_status = "Waiting..."
                with _last_gate_detections_lock:
                    _last_gate_detections.clear()
                _gate_reset_requested.clear()
                print("[APP] Gate rebuilt — clean slate")

            # ── Newest frame ──────────────────────────────────────────────────
            # Consume the latest frame produced by _capture_loop instead of
            # reading the camera here. Camera open/read/failure/reconnect and the
            # self-restart timeout now live in that thread, so recognition + JPEG
            # encoding never pause the camera and no stale frame backlog forms.
            with _capture_lock:
                ok = _capture_ok
                frame = _capture_frame
                seq = _capture_seq

            if not ok or frame is None:
                # No frame yet (startup) or capture is in a failure/reconnect
                # window — wait briefly for the producer; do not re-run any of the
                # camera-recovery logic here (it is owned by _capture_loop).
                time.sleep(CAPTURE_NO_FRAME_SLEEP_SECONDS)
                continue

            if seq == _last_capture_seq:
                # No new frame since the previous iteration. Skip reprocessing and
                # re-encoding the identical image: this avoids false stale-frame
                # watchdog trips and wasted JPEG encodes, with no UI change (the
                # last encoded frame is already being served).
                time.sleep(LOOP_SLEEP_SECONDS)
                continue
            _last_capture_seq = seq

            # ── Stale-frame watchdog ──────────────────────────────────────────
            fh_s, fw_s = frame.shape[:2]
            crop_y1 = max(0, fh_s // 2 - 32)
            crop_y2 = min(fh_s, fh_s // 2 + 32)
            crop_x1 = max(0, fw_s // 2 - 32)
            crop_x2 = min(fw_s, fw_s // 2 + 32)
            sample = frame[crop_y1:crop_y2, crop_x1:crop_x2]
            if sample.size > 0:
                sample_small = cv2.resize(
                    cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY),
                    (64, 64), interpolation=cv2.INTER_AREA,
                )
                frame_hash = hash(sample_small.tobytes())
            else:
                frame_hash = -1

            now_mono = time.monotonic()
            if frame_hash != _last_frame_hash:
                _stale_count = 0
                _last_frame_hash = frame_hash
                _stale_since = now_mono
                with _watchdog_lock:
                    _watchdog_last_fresh = now_mono
            else:
                _stale_count += 1

            stale_wall_seconds = now_mono - _stale_since
            if _stale_count >= STALE_FRAME_THRESHOLD and stale_wall_seconds >= 30.0:
                print(f"[CAM] Stale frame — resetting camera")
                reset_camera()
                _stale_count = 0
                _last_frame_hash = -1
                _stale_since = time.monotonic()
                time.sleep(CAMERA_RECONNECT_BACKOFF_SECONDS)
                continue

            # ── Detection ─────────────────────────────────────────────────────
            detections = []
            fresh_detection = False

            if _detection_active.is_set():
                now = time.monotonic()
                if (now - last_detect_time) >= DETECTION_INTERVAL_SECONDS:

                    # ── Emergency lock fast-path ──────────────────────────────
                    # Skip all face analysis when emergency lock is active.
                    # No recognition runs, no relay commands sent — just hold
                    # the DENIED_EMERGENCY state and show the status bar message.
                    if access_engine.is_emergency_locked():
                        gate._last_state  = STATE_DENIED_EMERGENCY
                        gate._last_status = "Emergency Lock Active"
                        detections        = []
                        last_detect_time  = now
                        fresh_detection   = True
                        with _last_gate_detections_lock:
                            _last_gate_detections.clear()
                        with _db_result_lock:
                            _db_result_override = {}
                    else:
                        try:
                            detections = gate.process(frame, eng, SCENARIO)
                            last_detect_time = now
                            fresh_detection = True
                            with _last_gate_detections_lock:
                                _last_gate_detections.clear()
                                _last_gate_detections.extend(detections)
                        except Exception as e:
                            print(f"[GATE] Error: {e}")
                            with _last_gate_detections_lock:
                                detections = list(_last_gate_detections)
                            fresh_detection = False
                else:
                    with _last_gate_detections_lock:
                        detections = list(_last_gate_detections)

                if fresh_detection and not detections:
                    with _db_result_lock:
                        _db_result_override = {}

                if fresh_detection:
                    for d in detections:
                        name = d.get("class_name", "")

                        with _db_result_lock:
                            override = dict(_db_result_override)

                        if override and name and name != override.get("name"):
                            with _db_result_lock:
                                _db_result_override = {}
                            override = {}

                        if d.get("_skip_log"):
                            if override and name == override.get("name"):
                                gate._last_status = override["msg"]
                                gate._last_state = override["state"]
                            continue

                        conf = float(d.get("confidence", 0.0))

                        if name and name not in ("Unknown Person", "Multiple faces", "Face detected"):
                            # ── GYM ACCESS DECISION ──────────────────────────
                            result = access_engine.decide(name, conf)

                            gate._last_state = result.decision
                            gate._last_status = result.message

                            with _db_result_lock:
                                _db_result_override = {
                                    "name":  name,
                                    "msg":   result.message,
                                    "state": result.decision,
                                }

                            with _last_detection_lock:
                                _last_detection = {
                                    "name":        name,
                                    "conf":        round(conf, 3),
                                    "time":        datetime.now().strftime("%H:%M:%S"),
                                    "decision":    result.decision,
                                    "reason":      result.reason,
                                    "allowed":     result.allowed,
                                    "door_opened": result.door_opened,
                                    "days_left":   result.days_left,
                                    "valid_until": result.valid_until,
                                    "message":     result.message,
                                }

                            print(
                                f"[GATE] {result.decision.upper()}: {name} "
                                f"conf={conf:.3f} door={result.door_opened}"
                            )

                        else:
                            with _db_result_lock:
                                _db_result_override = {}

                with _gate_status_lock:
                    _gate_status = gate._last_status
            else:
                with _last_gate_detections_lock:
                    detections = []

            # ── Clean enrolment frame ──────────────────────────────────────────
            # Keep a plain camera frame for enrolment UI and capture.
            # This prevents recognition labels/status overlays from appearing on enrol page
            # and stops labelled frames being saved as face enrolment previews.
            ok_clean_jpg, clean_buf = cv2.imencode(
                ".jpg", frame,
                [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
            )
            if ok_clean_jpg:
                with _frame_lock:
                    _latest_enrol_frame = clean_buf.tobytes()

            # ── Annotation ────────────────────────────────────────────────────
            annotated = IdentityEngine.annotate_frame(frame, detections)

            if _detection_active.is_set():
                with _gate_status_lock:
                    msg = _cv_safe_text(_gate_status)

                gate_state = getattr(gate, "_last_state", "no_face")

                # Gym colour scheme
                if gate_state in (STATE_GRANTED, STATE_GRANTED_EXPIRING, STATE_COOLDOWN):
                    msg_colour = (34, 197, 94)   # green
                elif gate_state in (STATE_DENIED_EXPIRED, STATE_DENIED_INACTIVE, STATE_DENIED_EMERGENCY):
                    msg_colour = (60, 60, 220)   # red
                elif gate_state == STATE_DENIED_UNKNOWN:
                    msg_colour = (60, 60, 220)   # red
                else:
                    msg_colour = (180, 180, 180) # neutral

                fh, fw = annotated.shape[:2]
                bar_h = 32
                overlay = annotated.copy()
                cv2.rectangle(overlay, (0, 0), (fw, bar_h), (10, 10, 10), -1)
                cv2.addWeighted(overlay, 0.75, annotated, 0.25, 0, annotated)
                cv2.rectangle(annotated, (0, bar_h - 2), (fw, bar_h), msg_colour, -1)

                font = cv2.FONT_HERSHEY_SIMPLEX
                font_scale = 0.52
                thickness = 1
                (tw, th), _ = cv2.getTextSize(msg, font, font_scale, thickness)
                tx = max(10, (fw - tw) // 2)
                ty = (bar_h + th) // 2 - 2
                cv2.putText(
                    annotated, msg, (tx, ty),
                    font, font_scale, msg_colour, thickness, cv2.LINE_AA,
                )

                roi = gate.center_roi
                roi_colour = (
                    (34, 197, 94)
                    if gate_state in (STATE_GRANTED, STATE_GRANTED_EXPIRING, STATE_COOLDOWN)
                    else (60, 60, 220)
                    if gate_state in (
                        STATE_DENIED_EXPIRED,
                        STATE_DENIED_INACTIVE,
                        STATE_DENIED_UNKNOWN,
                        STATE_DENIED_EMERGENCY,
                    )
                    else (180, 180, 180)
                )
                mx = int(fw * (1 - roi) / 2)
                my = int(fh * (1 - roi) / 2)
                rx1, ry1 = mx, my + bar_h
                rx2, ry2 = fw - mx, fh - my
                seg, gap = 12, 6

                def draw_dashed_rect(img, p1, p2, col, t=1):
                    x1d, y1d = p1
                    x2d, y2d = p2
                    for x in range(x1d, x2d, seg + gap):
                        cv2.line(img, (x, y1d), (min(x + seg, x2d), y1d), col, t)
                        cv2.line(img, (x, y2d), (min(x + seg, x2d), y2d), col, t)
                    for y in range(y1d, y2d, seg + gap):
                        cv2.line(img, (x1d, y), (x1d, min(y + seg, y2d)), col, t)
                        cv2.line(img, (x2d, y), (x2d, min(y + seg, y2d)), col, t)

                draw_dashed_rect(annotated, (rx1, ry1), (rx2, ry2), roi_colour, 1)

            ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
            fh, fw = annotated.shape[:2]
            ts_font = cv2.FONT_HERSHEY_SIMPLEX
            ts_scale = 0.38
            ts_thick = 1
            (tsw, tsh), _ = cv2.getTextSize(ts, ts_font, ts_scale, ts_thick)
            pad = 4
            cv2.rectangle(
                annotated,
                (8, fh - tsh - pad * 2 - 4),
                (8 + tsw + pad * 2, fh - 4),
                (10, 10, 10), -1,
            )
            cv2.putText(
                annotated, ts, (8 + pad, fh - pad - 6),
                ts_font, ts_scale, (120, 120, 120), ts_thick, cv2.LINE_AA,
            )

            if _IS_VIDEO_FILE:
                cv2.putText(
                    annotated, "TEST MODE", (10, fh - 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA,
                )

            ok_jpg, buf = cv2.imencode(
                ".jpg", annotated,
                [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
            )
            if ok_jpg:
                with _frame_lock:
                    _latest_frame = buf.tobytes()

            # Composite the SAME annotated frame onto the 1024x600 kiosk canvas for
            # the outside 7" display. Purely a display artefact — recognition already
            # ran on the untouched 640x480 `annotated`/capture frame above.
            try:
                kiosk = make_kiosk_frame(annotated)
                ok_k, kbuf = cv2.imencode(
                    ".jpg", kiosk,
                    [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
                )
                if ok_k:
                    with _frame_lock:
                        _latest_kiosk_frame = kbuf.tobytes()
            except Exception as e:
                print(f"[KIOSK] frame composite failed: {e} — kiosk falls back to live feed")

            time.sleep(LOOP_SLEEP_SECONDS)

        except Exception as e:
            print(f"[DETECTION LOOP] Unexpected error: {e} — continuing")
            time.sleep(1.0)



# ──────────────────────────────────────────────────────────────────────────────
# Long-term DB maintenance
# ──────────────────────────────────────────────────────────────────────────────

def _maintenance_loop() -> None:
    """
    Run daily long-term SQLite maintenance.

    Policy: keep the latest 12 months of access_logs and automatically
    remove only older door/access logs. Members, payments, embeddings and
    settings are never removed here.
    """
    last_cleanup_date = ""

    while True:
        try:
            today = date.today().isoformat()
            if today != last_cleanup_date:
                deleted = db.cleanup_old_access_logs(retention_months=12)
                if deleted > 0:
                    db.optimize_db_after_cleanup()
                last_cleanup_date = today
        except Exception as e:
            print(f"[MAINTENANCE] Access-log cleanup failed: {e}")

        time.sleep(3600)


# ──────────────────────────────────────────────────────────────────────────────
# MJPEG stream
# ──────────────────────────────────────────────────────────────────────────────

def make_kiosk_frame(frame: "np.ndarray") -> "np.ndarray":
    """
    Composite a camera frame onto the 1024x600 kiosk canvas for the outside 7"
    Waveshare LCD — WITHOUT cropping any camera content and WITHOUT black bars.

    Layout (professional "blurred fill" look, as used by modern video players):
      • Background: the same frame scaled to COVER the full 1024x600 and softened
        (cheap downscale→upscale blur) + darkened, so the panel fills edge to edge.
      • Foreground: the FULL frame scaled to CONTAIN (no crop) and centred on top.

    For the 640x480 (4:3) source this yields an 800x600 sharp centre with soft,
    darkened side panels — the entire camera image stays visible. This is a pure
    display transform; the recognition pipeline never sees it.
    """
    kw, kh = KIOSK_DISPLAY_W, KIOSK_DISPLAY_H
    h, w = frame.shape[:2]
    if w <= 0 or h <= 0:
        return np.zeros((kh, kw, 3), dtype=np.uint8)

    # ── Soft, darkened background that covers the whole canvas ──────────────────
    # Downscale to a tiny thumbnail then upscale: gives a smooth blurred look for a
    # fraction of a GaussianBlur's cost (keeps the added CPU per frame minimal).
    small = cv2.resize(frame, (48, 28), interpolation=cv2.INTER_AREA)
    bg = cv2.resize(small, (kw, kh), interpolation=cv2.INTER_LINEAR)
    bg = (bg * 0.45).astype(np.uint8)

    # ── Foreground: contain-fit (no crop), centred ─────────────────────────────
    # KIOSK_FG_SCALE (<1.0) leaves an even blurred border so the "blurred fill"
    # look stays visible even when the camera aspect ≈ the canvas aspect.
    scale = min(kw / w, kh / h) * KIOSK_FG_SCALE
    fw, fh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    fg = cv2.resize(frame, (fw, fh), interpolation=cv2.INTER_AREA)
    x = (kw - fw) // 2
    y = (kh - fh) // 2
    bg[y:y + fh, x:x + fw] = fg
    return bg


def _mjpeg_gen(clean: bool = False, kiosk: bool = False):
    while True:
        with _frame_lock:
            if kiosk:
                # Fall back to the plain live frame until the first kiosk frame
                # is composited, so the 7" screen never shows a blank stream.
                frame = _latest_kiosk_frame or _latest_frame
            else:
                frame = _latest_enrol_frame if clean else _latest_frame
        if frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
        time.sleep(STREAM_INTERVAL_SECONDS)


@app.get("/video_feed")
def video_feed(clean: bool = Query(False)):
    return StreamingResponse(
        _mjpeg_gen(clean=clean),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/video_feed_kiosk")
def video_feed_kiosk():
    """1024x600 composited stream for the outside 7" kiosk display only."""
    return StreamingResponse(
        _mjpeg_gen(kiosk=True),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Detection pause / resume
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/detection/pause")
def detection_pause(_: None = Depends(require_admin)):
    global _gate_status, _last_detection, _db_result_override
    _detection_active.clear()
    _gate_reset_requested.set()
    with _gate_status_lock:
        _gate_status = "Waiting..."
    with _last_detection_lock:
        _last_detection = {}
    with _db_result_lock:
        _db_result_override = {}
    with _last_gate_detections_lock:
        _last_gate_detections.clear()
    print("[APP] Detection PAUSED")
    return JSONResponse({"status": "paused"})


@app.post("/api/detection/resume")
def detection_resume(_: None = Depends(require_admin)):
    global _gate_status, _last_detection, _db_result_override
    _gate_reset_requested.set()
    with _gate_status_lock:
        _gate_status = "Waiting..."
    with _last_detection_lock:
        _last_detection = {}
    with _db_result_lock:
        _db_result_override = {}
    with _last_gate_detections_lock:
        _last_gate_detections.clear()
    _detection_active.set()
    print("[APP] Detection RESUMED")
    return JSONResponse({"status": "resumed"})


@app.get("/api/detection/status")
def detection_status(_: None = Depends(require_admin)):
    return JSONResponse({"active": _detection_active.is_set()})


# ──────────────────────────────────────────────────────────────────────────────
# Gate config API
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/gate_config")
def api_get_gate_config(_: None = Depends(require_admin)):
    with _gate_config_lock:
        return JSONResponse(dict(_gate_config))


@app.post("/api/gate_config")
async def api_set_gate_config(request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    with _gate_config_lock:
        current = dict(_gate_config)

    min_area   = int(body.get("min_face_area",   current["min_face_area"]))
    center_roi = float(body.get("center_roi",    current["center_roi"]))
    cooldown   = float(body.get("cooldown_secs", current["cooldown_secs"]))

    if not (5_000 <= min_area <= 200_000):
        raise HTTPException(400, "min_face_area must be between 5 000 and 200 000")
    if not (0.40 <= center_roi <= 1.00):
        raise HTTPException(400, "center_roi must be between 0.40 and 1.00")
    if not (1.0 <= cooldown <= 60.0):
        raise HTTPException(400, "cooldown_secs must be between 1 and 60")

    with _gate_config_lock:
        _gate_config["min_face_area"] = min_area
        _gate_config["center_roi"]    = center_roi
        _gate_config["cooldown_secs"] = cooldown

        # Relay controller config
        if "relay_baudrate" in body:
            _gate_config["relay_baudrate"] = int(body["relay_baudrate"])
        if "relay_port" in body:
            _gate_config["relay_port"] = str(body["relay_port"]).strip()
        if "relay_auto_connect" in body:
            _gate_config["relay_auto_connect"] = bool(body["relay_auto_connect"])
        if "expiry_warning_days" in body:
            _gate_config["expiry_warning_days"] = int(body["expiry_warning_days"])

    _save_gate_config()
    _gate_reset_requested.set()
    return JSONResponse({"ok": True, **_gate_config})


# ──────────────────────────────────────────────────────────────────────────────
# Snapshot
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/snapshot")
def snapshot(clean: bool = Query(False)):
    with _frame_lock:
        frame = _latest_enrol_frame if clean else _latest_frame
    if frame is None:
        raise HTTPException(503, "Camera not ready")
    return Response(content=frame, media_type="image/jpeg")


# ──────────────────────────────────────────────────────────────────────────────
# Member APIs  (renamed from /api/staff)
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/members")
def api_members(_: None = Depends(require_admin)):
    return JSONResponse(db.list_members())


@app.get("/api/enrolled_count")
def api_enrolled_count(_: None = Depends(require_admin)):
    if hasattr(db, "count_active_members"):
        return JSONResponse({"count": db.count_active_members()})
    return JSONResponse({"count": len(db.list_members())})


@app.get("/api/members/expiring")
def api_members_expiring(
    days: Optional[int] = Query(None),
    _: None = Depends(require_admin),
):
    if days is None:
        with _gate_config_lock:
            days = int(_gate_config.get("expiry_warning_days", 7))
    return JSONResponse(db.get_expiring_members(days))


@app.get("/api/members/expired")
def api_members_expired(_: None = Depends(require_admin)):
    return JSONResponse(db.get_expired_members())


@app.get("/api/members/{member_id}")
def api_member_detail(member_id: int, _: None = Depends(require_admin)):
    if hasattr(db, "get_member_detail"):
        found = db.get_member_detail(member_id)
    else:
        members = db.list_members()
        found = next((m for m in members if m["id"] == member_id), None)
    if not found:
        raise HTTPException(404, "Member not found")
    return JSONResponse(found)


@app.post("/api/members/create")
async def api_member_create(
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    _: None = Depends(require_admin),
):
    name = _validate_enrolment_name(name)
    try:
        member_id = db.add_member(name, phone, email)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"ok": True, "id": member_id, "name": name})


@app.post("/api/members/update")
async def api_member_update(
    request: Request,
    _: None = Depends(require_admin),
):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    phone = body.get("phone", "")

    # When member_code is present in the payload, this is the "Edit Details"
    # flow: update the user-facing Member ID (member_code) and phone together.
    # The internal numeric members.id and the member name are never changed here.
    if "member_code" in body:
        member_code = (body.get("member_code") or "").strip()
        if not member_code:
            raise HTTPException(400, "Member ID cannot be blank")
        # email preserved unless explicitly supplied
        email = body.get("email", None)
        try:
            updated = db.update_member_details(name, member_code, phone, email)
        except ValueError as e:
            # Duplicate member_code (UNIQUE violation) → 409
            raise HTTPException(409, str(e))
        if not updated:
            raise HTTPException(404, f"Member '{name}' not found")
        return JSONResponse({"ok": True})

    # Legacy contact-only update (phone/email).
    email = body.get("email", "")
    updated = db.update_member_contact(name, phone, email)
    if not updated:
        raise HTTPException(404, f"Member '{name}' not found")

    return JSONResponse({"ok": True})


@app.post("/api/members/remove")
async def api_member_remove(
    name: str = Form(...),
    _: None = Depends(require_admin),
):
    """
    Remove an active member from the system.

    Consistency rule:
      1. Deactivate member in gym.db first.
      2. Remove face embeddings second.

    If the DB member is not found/active, this endpoint stops and does not
    modify embeddings. This prevents the admin "Members" page from creating
    embedding-only or DB-only mismatches accidentally.
    """
    name = _normalise_name(name)
    if not name:
        raise HTTPException(400, "name is required")

    removed_db = db.remove_member(name)
    if not removed_db:
        raise HTTPException(404, f"'{name}' not found or not active")

    eng = get_engine()
    try:
        removed_emb = eng.remove_enrolled(name)
    except Exception as e:
        print(f"[REMOVE] WARNING — DB deactivated but embedding removal failed for '{name}': {e}")
        removed_emb = False

    print(f"[REMOVE] name={name} db_removed={removed_db} embedding_removed={removed_emb}")
    return JSONResponse({
        "ok": True,
        "name": name,
        "db_removed": removed_db,
        "embedding_removed": removed_emb,
    })

@app.post("/api/rename")
async def api_rename(
    old_name: str = Form(...),
    new_name: str = Form(...),
    _: None = Depends(require_admin),
):
    old_name = _normalise_name(old_name)
    new_name = _normalise_name(new_name)
    if not old_name or not new_name:
        raise HTTPException(400, "Both old_name and new_name are required")
    if new_name.lower() in BLOCKED_NAMES:
        raise HTTPException(400, f"'{new_name}' is not an allowed name")
    try:
        found = db.rename_member(old_name, new_name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not found:
        raise HTTPException(404, f"'{old_name}' not found or not active")
    eng = get_engine()
    try:
        eng.rename_enrolled(old_name, new_name)
    except Exception as e:
        print(f"[RENAME] Embedding rename warning: {e}")
    return JSONResponse({"ok": True, "old_name": old_name, "new_name": new_name})


# ──────────────────────────────────────────────────────────────────────────────
# Member rename  (POST /api/members/rename)
# Mirrors /api/rename but uses JSON body for consistency with admin UI
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/members/rename")
async def api_member_rename(
    request: Request,
    _: None = Depends(require_admin),
):
    """
    Rename an active member consistently.

    Update order:
      1. Rename DB member + access_logs through gym_db.rename_member().
      2. Rename the face embedding key in identity_engine.
      3. If embedding rename fails after DB rename, attempt DB rollback.

    This prevents the common mismatch where the face store and SQLite DB use
    different names after a failed/partial rename.
    """
    body = await request.json()
    old_name = _normalise_name(body.get("old_name", ""))
    new_name = _normalise_name(body.get("new_name", ""))

    if not old_name or not new_name:
        raise HTTPException(400, "Both old_name and new_name are required")
    if old_name == new_name:
        raise HTTPException(400, "New name is the same as current name")
    if new_name.lower() in BLOCKED_NAMES:
        raise HTTPException(400, f"'{new_name}' is not an allowed name")

    # DB first. If this fails, do not touch embeddings.
    try:
        found = db.rename_member(old_name, new_name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        print(f"[RENAME] DB rename failed: '{old_name}' → '{new_name}': {e}")
        raise HTTPException(500, f"Database rename failed: {e}")

    if not found:
        raise HTTPException(404, f"'{old_name}' not found or not active")

    eng = get_engine()
    embedding_renamed = False

    try:
        eng.rename_enrolled(old_name, new_name)
        embedding_renamed = True
        print(f"[RENAME] Embedding key updated: '{old_name}' → '{new_name}'")
    except Exception as e:
        print(f"[RENAME] WARNING — embedding rename failed after DB rename: {e}")

        # Roll back DB rename to avoid DB=new_name while embedding=old_name.
        try:
            rolled_back = db.rename_member(new_name, old_name)
            print(f"[RENAME] DB rollback {'OK' if rolled_back else 'FAILED'}: '{new_name}' → '{old_name}'")
        except Exception as rollback_error:
            print(f"[RENAME] CRITICAL — DB rollback failed: {rollback_error}")

        raise HTTPException(
            500,
            "Embedding rename failed, so database rename was rolled back. Try again."
        )

    print(f"[RENAME] Member renamed successfully: '{old_name}' → '{new_name}'")
    return JSONResponse({
        "ok": True,
        "old_name": old_name,
        "new_name": new_name,
        "db_renamed": True,
        "embedding_renamed": embedding_renamed,
    })

# ──────────────────────────────────────────────────────────────────────────────
# Member role  (POST /api/members/role)
# Set a member as 'member', 'owner', or 'staff'
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/members/role")
async def api_member_role(
    request: Request,
    _: None = Depends(require_admin),
):
    """
    Set the role for an active member.

    Body: { "name": "...", "role": "member" | "owner" | "staff" }

    Role behaviour:
        member  — normal member, door access requires valid payment
        owner   — door ALWAYS opens, no payment needed, labelled "Owner"
        staff   — door ALWAYS opens, no payment needed, labelled "Staff"

    Errors:
        400 — missing / invalid fields
        404 — member not found
    """
    body = await request.json()
    name = _normalise_name(body.get("name", ""))
    role = (body.get("role") or "").strip().lower()

    if not name:
        raise HTTPException(400, "name is required")
    if role not in ("member", "owner", "staff"):
        raise HTTPException(400, "role must be one of: member, owner, staff")

    try:
        found = db.set_member_role(name, role)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not found:
        raise HTTPException(404, f"'{name}' not found or not active")

    print(f"[ROLE] '{name}' → {role}")
    return JSONResponse({"ok": True, "name": name, "role": role})


# ──────────────────────────────────────────────────────────────────────────────
# Membership plan APIs
# ──────────────────────────────────────────────────────────────────────────────

def _validate_plan_body(body: dict) -> tuple:
    """
    Validate/normalise plan fields shared by create and update.
    Returns (plan_name, duration_days, price, description) or raises HTTPException.
    """
    plan_name = str(body.get("plan_name", "")).strip()
    if not plan_name:
        raise HTTPException(400, "plan_name cannot be empty")
    try:
        duration_days = int(body.get("duration_days"))
    except (TypeError, ValueError):
        raise HTTPException(400, "duration_days must be a whole number")
    if duration_days <= 0:
        raise HTTPException(400, "duration_days must be greater than 0")
    try:
        price = float(body.get("price"))
    except (TypeError, ValueError):
        raise HTTPException(400, "price must be a number")
    if price < 0:
        raise HTTPException(400, "price must be 0 or greater")
    description = str(body.get("description", "") or "").strip()
    return plan_name, duration_days, price, description


@app.get("/api/plans")
def api_plans(_: None = Depends(require_admin)):
    # Active plans only — feeds the Payments tab dropdown so deactivated
    # plans cannot be selected for new payments.
    return JSONResponse(db.list_plans(active_only=True))


@app.get("/api/plans/manage")
def api_plans_manage(_: None = Depends(require_admin)):
    # All plans (active + inactive) for the Plans management tab.
    return JSONResponse(db.list_plans())


@app.post("/api/plans/create")
async def api_plan_create(
    request: Request,
    _: None = Depends(require_admin),
):
    body = await request.json()
    plan_name, duration_days, price, description = _validate_plan_body(body)
    if db.plan_name_exists(plan_name):
        raise HTTPException(409, f"A plan named '{plan_name}' already exists")
    plan_id = db.add_plan(
        plan_name=plan_name,
        duration_days=duration_days,
        price=price,
        description=description,
    )
    return JSONResponse({"ok": True, "id": plan_id})


@app.post("/api/plans/update")
async def api_plan_update(
    request: Request,
    _: None = Depends(require_admin),
):
    body = await request.json()
    try:
        plan_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "id is required")
    plan_name, duration_days, price, description = _validate_plan_body(body)
    if db.plan_name_exists(plan_name, exclude_id=plan_id):
        raise HTTPException(409, f"A plan named '{plan_name}' already exists")
    if not db.update_plan(plan_id, plan_name, duration_days, price, description):
        raise HTTPException(404, "Plan not found")
    return JSONResponse({"ok": True})


@app.post("/api/plans/toggle-active")
async def api_plan_toggle_active(
    request: Request,
    _: None = Depends(require_admin),
):
    body = await request.json()
    try:
        plan_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "id is required")
    is_active = 1 if bool(body.get("is_active")) else 0
    if not db.set_plan_active(plan_id, is_active):
        raise HTTPException(404, "Plan not found")
    return JSONResponse({"ok": True, "is_active": is_active})


# ──────────────────────────────────────────────────────────────────────────────
# Payment APIs
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/payments")
def api_payments(_: None = Depends(require_admin)):
    return JSONResponse(db.get_all_payments())


@app.post("/api/payments/add")
async def api_payment_add(
    request: Request,
    _: None = Depends(require_admin),
):
    body = await request.json()

    member_name = (body.get("member_name") or "").strip()
    if not member_name:
        raise HTTPException(400, "member_name is required")

    member = db.get_member_by_name(member_name)
    if not member:
        raise HTTPException(404, f"Member '{member_name}' not found")

    payment_id = db.add_payment(
        member_id=member["id"],
        amount=float(body["amount"]),
        valid_from=body["valid_from"],
        valid_until=body["valid_until"],
        plan_id=body.get("plan_id"),
        payment_method=body.get("payment_method", "cash"),
        received_by=body.get("received_by", ""),
        note=body.get("note", ""),
    )
    return JSONResponse({"ok": True, "id": payment_id})


@app.get("/api/payments/member/{member_id}")
def api_member_payments(member_id: int, _: None = Depends(require_admin)):
    return JSONResponse(db.get_member_payments(member_id))


@app.get("/api/revenue/summary")
def api_revenue_summary(_: None = Depends(require_admin)):
    data = db.get_revenue_summary()
    with _gate_config_lock:
        data["expiry_warning_days"] = int(_gate_config.get("expiry_warning_days", 7))
    return JSONResponse(data)


@app.get("/api/revenue/range")
def api_revenue_range(
    date_from: str = Query(...),
    date_to: str = Query(...),
    _: None = Depends(require_admin),
):
    return JSONResponse(db.get_revenue_range(date_from, date_to))


@app.get("/api/revenue/daily")
def api_revenue_daily(
    days: int = Query(30),
    _: None = Depends(require_admin),
):
    return JSONResponse(db.get_daily_revenue_chart(days))


# ──────────────────────────────────────────────────────────────────────────────
# Access log APIs
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/access_logs")
def api_access_logs(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_admin),
):
    return JSONResponse(db.get_access_logs(date_from, date_to, name, limit=limit, offset=offset))


@app.get("/api/access/today")
def api_access_today(_: None = Depends(require_admin)):
    return JSONResponse(db.get_today_access())


@app.get("/api/access/last")
def api_access_last(_: None = Depends(require_admin)):
    return JSONResponse(db.get_last_access() or {})


# Fields the public 7" kiosk popup actually needs (see static/js/index.js).
# Everything else (conf, valid_until, door_opened, internal message) is withheld
# from the outside display and only returned to a logged-in admin browser.
_KIOSK_DETECTION_FIELDS = ("name", "time", "decision", "reason", "allowed", "days_left")


@app.get("/api/last_detection")
def api_last_detection(admin_session: Optional[str] = Cookie(default=None)):
    with _last_detection_lock:
        data = dict(_last_detection)
    if is_authenticated(admin_session):
        return JSONResponse(data)
    # Outside display: minimum safe subset only.
    return JSONResponse({k: data[k] for k in _KIOSK_DETECTION_FIELDS if k in data})


@app.get("/api/gate_status")
def api_gate_status():
    with _gate_status_lock:
        return JSONResponse({"status": _gate_status})


@app.get("/api/export_csv")
def export_csv(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    _: None = Depends(require_admin),
):
    records = db.get_access_logs(date_from, date_to, name)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["member_name", "ts", "decision", "reason", "door_opened"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(records)
    month_str = date.today().strftime("%B_%Y").lower()
    if name:
        safe_name = "".join(c if c.isalnum() else "_" for c in name.strip())
        fname = f"access_{safe_name}_{month_str}.csv"
    else:
        fname = f"access_logs_{month_str}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Door control APIs
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/door/status")
def api_door_status(_: None = Depends(require_admin)):
    return JSONResponse(get_access_engine().door_status())


@app.post("/api/door/open")
async def api_door_open(
    _: None = Depends(require_admin),
):
    # Sends OPEN signal to relay controller — relay hold time is set in the controller firmware
    opened = get_access_engine().manual_open()
    return JSONResponse({"ok": opened, "message": "Door open signal sent to relay controller"})


@app.post("/api/door/lock")
def api_door_lock(_: None = Depends(require_admin)):
    get_access_engine().force_lock()
    return JSONResponse({"ok": True, "message": "Door locked"})


@app.post("/api/door/emergency_lock")
def api_emergency_lock(_: None = Depends(require_admin)):
    eng = get_access_engine()
    eng.set_emergency_lock(True)
    with _gate_config_lock:
        _gate_config["emergency_lock"] = True
    _save_gate_config()
    return JSONResponse({"ok": True, "emergency_lock": True})


@app.post("/api/door/emergency_unlock")
def api_emergency_unlock(_: None = Depends(require_admin)):
    eng = get_access_engine()
    eng.set_emergency_lock(False)
    with _gate_config_lock:
        _gate_config["emergency_lock"] = False
    _save_gate_config()
    return JSONResponse({"ok": True, "emergency_lock": False})


@app.post("/api/door/test")
def api_door_test(_: None = Depends(require_admin)):
    result = get_access_engine().relay_test()
    return JSONResponse({"ok": result, "message": "Relay test pulse sent (1 second)"})


# ──────────────────────────────────────────────────────────────────────────────
# Expiry helpers
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Enrolment  (same flow as attendance system — member name replaces staff name)
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/enrol/capture")
async def api_enrol_capture(
    name: str = Form(...),
    slot: int = Form(...),
    _: None = Depends(require_admin),
):
    name = _validate_enrolment_name(name)
    if slot not in (1, 2, 3):
        raise HTTPException(400, "Slot must be 1, 2 or 3")

    # Duplicate guard — reject at first capture attempt, not just at confirm
    _check_name_not_already_enrolled(name)

    with _frame_lock:
        # Use the clean enrolment frame, never the annotated live-feed frame.
        frame_bytes = _latest_enrol_frame or _latest_frame
    if frame_bytes is None:
        raise HTTPException(503, "Camera not ready")

    nparr = np.frombuffer(frame_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(503, "Could not decode camera frame")

    eng = get_engine()
    face_data = eng.analyse_face(frame)
    if face_data is None:
        raise HTTPException(422, "No face detected. Move closer and ensure good lighting.")

    emb = face_data["embedding"]
    bbox = face_data["bbox"]
    faces_count = int(face_data["faces_count"])

    if faces_count > 1:
        raise HTTPException(422, "Multiple faces detected. One person only.")

    with _enrol_lock:
        if name not in _enrol_sessions:
            _enrol_sessions[name] = {}
        _enrol_sessions[name][slot] = emb
        count = len(_enrol_sessions[name])

    print(f"[ENROL] {name} — slot {slot} captured ({count}/3)")

    thumb = frame.copy()
    if bbox and len(bbox) == 4:
        x1, y1, x2, y2 = bbox
        cv2.rectangle(thumb, (x1, y1), (x2, y2), (34, 197, 94), 2)
        cv2.putText(thumb, f"Pose {slot} OK", (x1, max(y1 - 8, 16)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (34, 197, 94), 2, cv2.LINE_AA)

    ok_jpg, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok_jpg:
        raise HTTPException(500, "Failed to create preview image")

    return Response(
        content=buf.tobytes(), media_type="image/jpeg",
        headers={"X-Slot": str(slot), "X-Total": str(count)},
    )


@app.post("/api/enrol/upload")
async def api_enrol_upload(
    name: str = Form(...),
    slot: int = Form(...),
    photo: UploadFile = File(...),
    _: None = Depends(require_admin),
):
    name = _validate_enrolment_name(name)
    if slot not in (1, 2, 3):
        raise HTTPException(400, "Slot must be 1, 2 or 3")

    _check_name_not_already_enrolled(name)

    data = await photo.read()
    nparr = np.frombuffer(data, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "Invalid image upload")

    eng = get_engine()
    face_data = eng.analyse_face(frame)
    if face_data is None:
        raise HTTPException(422, "No face detected. Use a clear front-facing photo.")

    emb = face_data["embedding"]
    bbox = face_data["bbox"]
    faces_count = int(face_data["faces_count"])

    if faces_count > 1:
        raise HTTPException(422, "Multiple faces detected. Upload one person only.")

    with _enrol_lock:
        if name not in _enrol_sessions:
            _enrol_sessions[name] = {}
        _enrol_sessions[name][slot] = emb
        count = len(_enrol_sessions[name])

    thumb = frame.copy()
    if bbox and len(bbox) == 4:
        x1, y1, x2, y2 = bbox
        cv2.rectangle(thumb, (x1, y1), (x2, y2), (34, 197, 94), 2)
        cv2.putText(thumb, f"Photo {slot} OK", (x1, max(y1 - 8, 16)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (34, 197, 94), 2, cv2.LINE_AA)

    ok_jpg, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok_jpg:
        raise HTTPException(500, "Failed to create preview image")

    return Response(
        content=buf.tobytes(), media_type="image/jpeg",
        headers={"X-Slot": str(slot), "X-Total": str(count)},
    )


@app.post("/api/enrol/remove_slot")
async def api_enrol_remove_slot(
    name: str = Form(...),
    slot: int = Form(...),
    _: None = Depends(require_admin),
):
    name = _normalise_name(name)
    with _enrol_lock:
        if name in _enrol_sessions and slot in _enrol_sessions[name]:
            del _enrol_sessions[name][slot]
            count = len(_enrol_sessions[name])
            return JSONResponse({"ok": True, "remaining": count})
    raise HTTPException(404, "Slot not found")


@app.post("/api/enrol/confirm")
async def api_enrol_confirm(
    name: str = Form(...),
    phone: str = Form(""),
    member_code: str = Form(""),
    _: None = Depends(require_admin),
):
    """
    Confirm enrolment using a DB-first save order.

    Important consistency fix:
      - Old code saved ArcFace embeddings first.
      - Then it added the DB member only when member_name_check(name) == 'not_found'.
      - If a member had been removed before, DB status was 'inactive', so the
        embedding was saved but the DB member stayed inactive.

    New behaviour:
      1. Require 3 captured/uploaded embeddings.
      2. Reject only active duplicate names.
      3. Call db.add_member(name) first. That function inserts new members and
         reactivates inactive members.
      4. Save/replace the embeddings.
      5. If embedding save fails, deactivate the DB member again.
    """
    name = _validate_enrolment_name(name)
    phone = (phone or "").strip()

    # Member ID (member_code) is optional. Blank/whitespace-only → auto-generate.
    member_code = (member_code or "").strip()

    if len(phone) > 30:
        raise HTTPException(400, "Phone number is too long")

    with _enrol_lock:
        session = dict(_enrol_sessions.get(name, {}))

    if len(session) < 3:
        raise HTTPException(422, f"Need 3 captures. Currently have {len(session)}.")

    # Only active duplicates are blocked. Inactive names are allowed because
    # db.add_member() reactivates them instead of creating duplicates.
    _check_name_not_already_enrolled(name)

    try:
        # Phone is optional. Email is kept blank during face enrolment.
        # member_code is optional — blank auto-generates the legacy GYM<ts> code.
        member_id = db.add_member(name, phone, "", member_code=member_code)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        print(f"[ENROL] DB add/reactivate failed for '{name}': {e}")
        raise HTTPException(500, f"Database enrolment failed: {e}")

    eng = get_engine()

    try:
        # Replace any old/orphan embedding key for this name with the current
        # three confirmed captures. This avoids duplicate stacked embeddings
        # when a removed/inactive member is re-enrolled.
        # Guard with _enrolled_lock so the detection thread cannot read a
        # partially updated dict during this mutation.
        with eng._enrolled_lock:
            eng._enrolled[name] = []
            for slot in sorted(session.keys()):
                eng._enrolled[name].append(session[slot])
        eng._save_enrolled()
    except Exception as e:
        print(f"[ENROL] Embedding save failed for '{name}': {e}")

        # Roll back DB activation so we do not leave an active DB member with
        # no usable embedding.
        try:
            db.remove_member(name)
            print(f"[ENROL] DB rollback/deactivation completed for '{name}'")
        except Exception as rollback_error:
            print(f"[ENROL] CRITICAL — DB rollback failed for '{name}': {rollback_error}")

        raise HTTPException(500, f"Embedding save failed: {e}")

    with _enrol_lock:
        _enrol_sessions.pop(name, None)

    print(f"[ENROL] {name} — confirmed, DB member_id={member_id}, 3 embeddings saved")
    return JSONResponse({
        "ok": True,
        "name": name,
        "phone": phone,
        "member_id": member_id,
        "captures": 3,
        "db_saved": True,
        "embeddings_saved": True,
    })

@app.post("/api/enrol/cancel")
async def api_enrol_cancel(
    name: str = Form(...),
    _: None = Depends(require_admin),
):
    name = _normalise_name(name)
    with _enrol_lock:
        _enrol_sessions.pop(name, None)
    return JSONResponse({"ok": True})


@app.post("/api/remove")
async def api_remove(
    name: str = Form(...),
    _: None = Depends(require_admin),
):
    """
    Legacy remove endpoint.

    Kept for compatibility, but now follows the same DB-first rule as
    /api/members/remove.
    """
    name = _normalise_name(name)
    if not name:
        raise HTTPException(400, "name is required")

    removed_db = db.remove_member(name)
    if not removed_db:
        raise HTTPException(404, f"'{name}' not found or not active")

    eng = get_engine()
    try:
        removed_emb = eng.remove_enrolled(name)
    except Exception as e:
        print(f"[REMOVE] WARNING — DB deactivated but embedding removal failed for '{name}': {e}")
        removed_emb = False

    print(f"[REMOVE] name={name} db_removed={removed_db} embedding_removed={removed_emb}")
    return JSONResponse({
        "ok": True,
        "name": name,
        "db_removed": removed_db,
        "embedding_removed": removed_emb,
    })

# ──────────────────────────────────────────────────────────────────────────────
# Gym settings
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/settings/gym")
def api_gym_settings(_: None = Depends(require_admin)):
    return JSONResponse(db.get_all_settings())


@app.post("/api/settings/gym")
async def api_set_gym_settings(
    request: Request,
    _: None = Depends(require_admin),
):
    body = await request.json()
    allowed_keys = {"door_open_seconds", "expiry_warning_days", "allow_expiring"}
    for key, val in body.items():
        if key in allowed_keys:
            db.set_setting(key, str(val))
    return JSONResponse({"ok": True})


# ──────────────────────────────────────────────────────────────────────────────
# Password change  (unchanged from attendance system)
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/settings/password")
async def api_change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    _: None = Depends(require_admin),
):

    # Must match systemd EnvironmentFile and backup_db.sh (single source of truth).
    # Prefer the current production env; fall back to the legacy name only for
    # un-migrated installs, then to a local .env for dev.
    system_env = Path("/etc/codeglofix-access.env")
    if not system_env.exists():
        system_env = Path("/etc/gym-access.env")   # legacy installs only
    local_env  = BASE_DIR / ".env"
    env_path   = system_env if system_env.exists() else local_env

    actual_current = os.environ.get("ADMIN_PASSWORD", "")
    if current_password != actual_current:
        raise HTTPException(403, "Current password is incorrect")
    if len(new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters")
    if new_password == current_password:
        raise HTTPException(400, "New password must differ from current password")

    if env_path.exists():
        lines = env_path.read_text().splitlines()
        updated = []
        found = False
        for line in lines:
            if line.startswith("ADMIN_PASSWORD="):
                updated.append(f"ADMIN_PASSWORD={new_password}")
                found = True
            else:
                updated.append(line)
        if not found:
            updated.append(f"ADMIN_PASSWORD={new_password}")
        try:
            env_path.write_text("\n".join(updated) + "\n")
        except PermissionError:
            result = subprocess.run(
                ["sudo", "tee", str(env_path)],
                input=("\n".join(updated) + "\n").encode(),
                capture_output=True,
            )
            if result.returncode != 0:
                raise HTTPException(500, "Could not write to system config")
    else:
        env_path.write_text(f"ADMIN_PASSWORD={new_password}\n")

    os.environ["ADMIN_PASSWORD"] = new_password
    print("[AUTH] Admin password changed successfully")
    return JSONResponse({"ok": True})


# ──────────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def api_health(admin_session: Optional[str] = Cookie(default=None)):
    with _watchdog_lock:
        last_fresh = _watchdog_last_fresh
    age = round(time.monotonic() - last_fresh, 1) if last_fresh > 0 else None
    camera_ok = age is not None and age < 30.0

    # Public/kiosk payload: ONLY camera liveness, which the outside display's
    # watchdog needs to auto-reload if the feed dies. No operational state
    # (enrolled count, door/relay status, emergency lock) is exposed here.
    payload = {
        "ok":                       camera_ok,
        "camera_fresh_seconds_ago": age,
    }

    # Admin browsers (valid cookie) additionally get operational details used
    # by the admin dashboard health widgets.
    if is_authenticated(admin_session):
        eng = get_access_engine()
        payload.update({
            "detection_active": _detection_active.is_set(),
            "enrolled_count":   len(get_engine()._enrolled),
            "emergency_lock":   eng.is_emergency_locked(),
            "door_status":      eng.door_status(),
            "camera_resolution": (
                f"{_actual_cam_w}x{_actual_cam_h}" if not _IS_VIDEO_FILE else "video-file"
            ),
        })

    return JSONResponse(payload)


# ──────────────────────────────────────────────────────────────────────────────
# HTML pages
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def page_home(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ── PWA assets ────────────────────────────────────────────────────────────────
# sw.js must be served at root scope so the service worker can intercept
# requests for all pages, not just /static/* paths.

@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/static/manifest.json")
async def pwa_manifest():
    return FileResponse(
        BASE_DIR / "static" / "manifest.json",
        media_type="application/manifest+json",
    )


# ── Admin-only HTML pages ──────────────────────────────────────────────────────
# These pages contain staff/owner controls (member list, payments, enrolment,
# relay test, settings, logs). They must NEVER be served to an unauthenticated
# browser — including the public outside-door kiosk. If the admin_session cookie
# is missing or invalid we return a standalone login page (HTTP 401) instead of
# the real page markup, so no admin UI is shipped to the outside display.

def _admin_page_or_login(request: Request, template_name: str) -> HTMLResponse:
    if not is_authenticated(request.cookies.get(COOKIE_NAME)):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": request.url.path},
            status_code=401,
        )
    return templates.TemplateResponse(request, template_name)


@app.get("/enrol", response_class=HTMLResponse)
async def page_enrol(request: Request):
    return _admin_page_or_login(request, "enrol.html")


@app.get("/enrol_remote", response_class=HTMLResponse)
async def page_enrol_remote(request: Request):
    return _admin_page_or_login(request, "enrol_remote.html")


@app.get("/admin", response_class=HTMLResponse)
async def page_admin(request: Request):
    return _admin_page_or_login(request, "admin.html")
