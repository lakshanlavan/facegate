#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# run_live_display.sh — launcher for the PyQt6 live-display wrapper.
#
# Opens the live attendance page full-screen on the connected door display
# (preferring a 7-inch panel) via kiosk/live_display.py, WITHOUT Google Chrome.
#
# This launcher deliberately does NOT:
#   • create an infinite restart loop (it runs live_display.py ONCE — see the
#     "Restart policy" note below),
#   • create desktop autostart,
#   • enable or install any systemd unit.
# Autostart on boot is handled by the codeglofix-live-display.service systemd
# --user unit (installed/enabled by the installer); this script is what that unit
# runs, and is also what the "CodeGloFix Live View" desktop icon launches.
#
# Usage:
#   ./run_live_display.sh          # auto-select target screen
#   ./run_live_display.sh 1        # force screen index 1
#
# Environment overrides (optional):
#   LIVE_DISPLAY_VENV     path to a venv to use (else auto-detected). The systemd
#                         --user service passes this as the installer-rendered
#                         venv (__VENV_DIR__), so the exact app venv is used.
#   LIVE_DISPLAY_URL      page URL        (default http://127.0.0.1:5000/)
#   LIVE_DISPLAY_HEALTH_URL health URL    (default http://127.0.0.1:5000/api/health)
#   LIVE_DISPLAY_LOG      explicit log file path
#   HEALTH_WAIT_SECONDS   max seconds to wait for backend health (default 180)
#
# Default URLs talk DIRECTLY to the backend on 127.0.0.1:5000 (plain HTTP, no
# nginx, no self-signed cert) so the display comes up even without nginx. Set
# the two URL vars to https://localhost/ to route through nginx instead.
# ──────────────────────────────────────────────────────────────────────────────
set -u

# ── Locate the project safely (this script lives in <project>/kiosk/) ─────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SCREEN_INDEX="${1:-}"

# ── Health / URL config (kept in sync with live_display.py defaults) ──────────
LIVE_DISPLAY_URL="${LIVE_DISPLAY_URL:-http://127.0.0.1:5000/}"
LIVE_DISPLAY_HEALTH_URL="${LIVE_DISPLAY_HEALTH_URL:-http://127.0.0.1:5000/api/health}"
HEALTH_WAIT_SECONDS="${HEALTH_WAIT_SECONDS:-180}"
export LIVE_DISPLAY_URL LIVE_DISPLAY_HEALTH_URL

# ── Log location: /var/log/codeglofix if writable, else project/kiosk ─────────
if [ -n "${LIVE_DISPLAY_LOG:-}" ]; then
    LOG="$LIVE_DISPLAY_LOG"
elif [ -w /var/log/codeglofix ] 2>/dev/null; then
    LOG="/var/log/codeglofix/live_display.log"
else
    LOG="$SCRIPT_DIR/live_display.log"
fi
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG" >&2; }

log "=== run_live_display starting (project=$PROJECT_DIR) ==="

# ── Qt platform: prefer Wayland for THIS process only when appropriate ────────
# If the session is Wayland and QT_QPA_PLATFORM is empty or the (often broken)
# "xcb", use "wayland" — exported only into this script's environment, never
# written globally. Any other explicit value is respected.
on_wayland() {
    [ -n "${WAYLAND_DISPLAY:-}" ] && return 0
    [ "$(printf '%s' "${XDG_SESSION_TYPE:-}" | tr 'A-Z' 'a-z')" = "wayland" ] && return 0
    [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -e "$XDG_RUNTIME_DIR/wayland-0" ] && return 0
    return 1
}
if on_wayland && { [ -z "${QT_QPA_PLATFORM:-}" ] || [ "${QT_QPA_PLATFORM:-}" = "xcb" ]; }; then
    export QT_QPA_PLATFORM=wayland
    log "Wayland session detected — using QT_QPA_PLATFORM=wayland (this process only)"
fi

# ── Resolve the Python interpreter / venv ─────────────────────────────────────
# Priority: explicit LIVE_DISPLAY_VENV, then the project's own conventions
# (CODEGLOFIX_VENV / DIGITGYM_VENV), then known install locations, then a
# sibling ../venv, then a bare python3 (WebEngine likely missing → warn).
PYTHON=""
for cand in \
    "${LIVE_DISPLAY_VENV:-}" \
    "${VENV_DIR:-}" \
    "${CODEGLOFIX_VENV:-}" \
    "${DIGITGYM_VENV:-}" \
    "/opt/codeglofix/venv" \
    "/opt/digitweb-attendance/venv" \
    "$PROJECT_DIR/../venv" \
    "$PROJECT_DIR/venv" ; do
    [ -n "$cand" ] || continue
    if [ -x "$cand/bin/python" ]; then
        PYTHON="$cand/bin/python"
        log "Using venv python: $PYTHON"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON="$(command -v python3)"
        log "WARNING: no project venv found — falling back to system $PYTHON (PyQt6-WebEngine may be missing; see kiosk/README_live_display.md)"
    else
        log "ERROR: no python interpreter found. Aborting."
        exit 1
    fi
fi

# ── Verify the display deps are importable before we try to open a window ─────
if ! "$PYTHON" - <<'PY' >>"$LOG" 2>&1
import importlib, sys
for m in ("PyQt6.QtWidgets", "PyQt6.QtWebEngineWidgets"):
    importlib.import_module(m)
print("display deps OK")
PY
then
    log "ERROR: PyQt6 / PyQt6-WebEngine not importable with $PYTHON."
    log "       Install them into that venv, e.g.:"
    log "         $(dirname "$PYTHON")/pip install -r \"$PROJECT_DIR/requirements-display.txt\""
    log "       (or re-run deploy/install_customer_pc.sh to reinstall the display deps.)"
    exit 1
fi

# ── Wait for backend health before starting the UI ────────────────────────────
log "Waiting up to ${HEALTH_WAIT_SECONDS}s for $LIVE_DISPLAY_HEALTH_URL ..."
deadline=$(( $(date +%s) + HEALTH_WAIT_SECONDS ))
healthy=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    code="$(curl -sk -o /dev/null -m 4 -w '%{http_code}' "$LIVE_DISPLAY_HEALTH_URL" 2>/dev/null)"
    case "$code" in
        200) healthy=1; break ;;
        *) sleep 3 ;;
    esac
done
if [ "$healthy" -eq 1 ]; then
    log "Backend healthy (HTTP 200) — launching live_display.py"
else
    # Not fatal: live_display.py has its own health-wait + on-screen status and
    # ESC-to-exit, so we still launch and let it keep waiting/logging.
    log "WARNING: backend not healthy within ${HEALTH_WAIT_SECONDS}s — launching anyway (live_display.py will keep waiting; press ESC to exit)."
fi

# ── Restart policy ────────────────────────────────────────────────────────────
# Intentionally NO restart loop here yet. We run live_display.py exactly once.
# live_display.py itself exits non-zero (code 2) after repeated reload failures
# so that a FUTURE supervisor (systemd --user) can restart it — but wiring that
# supervisor up is a separate, not-yet-approved step.
log "exec: $PYTHON $SCRIPT_DIR/live_display.py $SCREEN_INDEX"
"$PYTHON" "$SCRIPT_DIR/live_display.py" $SCREEN_INDEX 2>&1 | tee -a "$LOG"
rc="${PIPESTATUS[0]}"
log "live_display.py exited (rc=$rc)"
exit "$rc"
