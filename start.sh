#!/bin/bash
set -e

# ── Resolve paths ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
# Venv: production sets CODEGLOFIX_VENV=/opt/codeglofix/venv; dev defaults to
# ../venv. Legacy DIGITGYM_VENV is honoured as an internal migration fallback.
VENV_DIR="${CODEGLOFIX_VENV:-${DIGITGYM_VENV:-$PROJECT_DIR/../venv}}"
# Log: production sets CODEGLOFIX_LOG=/var/log/codeglofix/backend.log; dev keeps
# the log next to the source. Legacy DIGITGYM_LOG honoured as internal fallback.
LOG="${CODEGLOFIX_LOG:-${DIGITGYM_LOG:-$PROJECT_DIR/codeglofix_backend.log}}"

# If the configured log is not writable (e.g. a root-owned backend.log left by a
# previous run), DON'T die under `set -e`. Warn on stderr — which systemd still
# captures — and fall back to /tmp so the backend can start and be diagnosed.
if ! ( : >> "$LOG" ) 2>/dev/null; then
    echo "[WARN] log '$LOG' is not writable by $(id -un); falling back to /tmp/codeglofix_backend.log" >&2
    LOG="/tmp/codeglofix_backend.log"
    if ! ( : >> "$LOG" ) 2>/dev/null; then
        echo "[ERROR] fallback log '$LOG' also not writable; logging to stderr only" >&2
        LOG="/dev/stderr"
    fi
fi

echo "=== CodeGloFix Access Control starting at $(date) ===" >> "$LOG"
echo "[INFO] Project dir: $PROJECT_DIR" >> "$LOG"
echo "[INFO] Venv dir: $VENV_DIR" >> "$LOG"

cd "$PROJECT_DIR"

# Make `import main` resolve regardless of the inherited CWD. uvicorn imports the
# app string "main:app" from sys.path[0]; without this a stale CWD (or a future
# WorkingDirectory change) yields the misleading "Could not import module main".
export PYTHONPATH="$PROJECT_DIR:${PYTHONPATH:-}"

# ── Required production environment ───────────────────────────────
# Keep these also in systemd Environment= lines if using service
export SESSION_TTL="${SESSION_TTL:-28800}"
# Production runs behind nginx HTTPS — secure cookies by default.
# Override to "false" only for a plain-HTTP local test session.
export COOKIE_SECURE="${COOKIE_SECURE:-true}"
export VIDEO_SOURCE="${VIDEO_SOURCE:-0}"

# ── Live-display real-time tuning (env-overridable; main.py holds the fallbacks) ──
# MJPEG outside-display smoothness. 0.0556s ≈ 18 fps (was 0.0667s ≈ 15 fps, 0.10s ≈
# 10 fps). 18 fps is a smoothness step up paired with the slightly lighter 896x504
# capture below; only go to 0.05 (20 fps) if a CPU measurement on the kiosk PC shows
# headroom. Purely a display-stream rate — it does NOT change recognition, which is
# throttled separately (DETECTION_INTERVAL_SECONDS).
export STREAM_INTERVAL_SECONDS="${STREAM_INTERVAL_SECONDS:-0.0556}"
# Capture resolution. 896x504 (16:9) is a touch lighter to encode than 960x540 to
# support the 18 fps target, while staying visually crisp on the 7" panel and well
# above 640x480. get_camera() reads the mode back and falls back to 640x480 if the
# webcam rejects it, so this is always safe. Detector input stays a fixed 320x320
# and the distance gate is resolution-normalised, so recognition accuracy is
# unchanged. Revert with CAMERA_WIDTH=960 CAMERA_HEIGHT=540 (or 1280x720).
export CAMERA_WIDTH="${CAMERA_WIDTH:-896}"
export CAMERA_HEIGHT="${CAMERA_HEIGHT:-504}"

if [ -z "$ADMIN_PASSWORD" ]; then
    echo "[ERROR] ADMIN_PASSWORD is not set" >> "$LOG"
    exit 1
fi

if [ -z "$SESSION_SECRET" ]; then
    echo "[ERROR] SESSION_SECRET is not set" >> "$LOG"
    exit 1
fi

# ── Disable USB autosuspend — prevents USB camera freeze ──────────
for f in /sys/bus/usb/devices/*/power/autosuspend; do
    echo -1 > "$f" 2>/dev/null || true
done
echo "[INFO] USB autosuspend disable attempted" >> "$LOG"

# ── Find Python / uvicorn from venv ───────────────────────────────
if [ -x "$VENV_DIR/bin/python" ]; then
    PYTHON_CMD="$VENV_DIR/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="$(command -v python3)"
else
    echo "[ERROR] python not found" >> "$LOG"
    exit 1
fi

echo "[INFO] Using python: $PYTHON_CMD" >> "$LOG"

# ── Pre-flight import check ───────────────────────────────────────
# uvicorn reports any exception raised while importing the app target as a flat
# "ERROR: Could not import module 'main'" and swallows the underlying traceback
# (e.g. a missing system lib for cv2, or an InsightFace/onnxruntime import error
# on first boot). Import the module ourselves first so the REAL traceback lands
# in the backend log and the service fails fast with an actionable error.
echo "[INFO] Pre-flight: importing 'main'…" >> "$LOG"
if ! "$PYTHON_CMD" -c "import main" >> "$LOG" 2>&1; then
    echo "[ERROR] 'import main' failed — see the traceback above. Refusing to start uvicorn." >> "$LOG"
    exit 1
fi
echo "[INFO] Pre-flight import OK" >> "$LOG"

# ── Start backend ────────────────────────────────────────────────
# Bind to localhost only — nginx terminates TLS and reverse-proxies to us.
# This keeps the plain-HTTP app (and the unauthenticated live-view endpoints)
# off the LAN; everything external must go through nginx on 443.
# Single worker is mandatory: camera/relay/engine state is in-process.
# --app-dir pins the import root to the project dir, belt-and-braces with the
# PYTHONPATH export above and the systemd WorkingDirectory.
exec "$PYTHON_CMD" -m uvicorn main:app --app-dir "$PROJECT_DIR" \
    --host 127.0.0.1 --port 5000 --workers 1