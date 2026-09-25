#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# verify_pyqt_display_install.sh — post-install health check for the PyQt6
# live-display wrapper. READ-ONLY: it changes nothing, only reports.
#
# Confirms the things that make the PyQt door display (the ONLY live view) come
# up after an install:
#   • requirements-display.txt present
#   • PyQt6 + PyQt6-WebEngine import in the app venv
#   • backend health URL reachable (default http://127.0.0.1:5000/api/health)
#   • the systemd --user unit file exists + is enabled/active
#   • the display(s) Qt can see (screen selection preview)
#   • where the live-display log is
#
# RUN AS THE NORMAL DESKTOP USER (systemd --user is per-user).
#   ./deploy/verify_pyqt_display_install.sh [--venv <dir>] [--app-dir <dir>]
#
# Exit code: 0 if all mandatory checks pass, non-zero otherwise.
# ──────────────────────────────────────────────────────────────────────────────
set -u

VENV_OVERRIDE=""
APP_OVERRIDE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --venv)     VENV_OVERRIDE="${2:-}"; shift 2 ;;
        --venv=*)   VENV_OVERRIDE="${1#*=}"; shift ;;
        --app-dir)  APP_OVERRIDE="${2:-}"; shift 2 ;;
        --app-dir=*) APP_OVERRIDE="${1#*=}"; shift ;;
        -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

USER_HOME="$HOME"
HEALTH_URL="${LIVE_DISPLAY_HEALTH_URL:-http://127.0.0.1:5000/api/health}"
PAGE_URL="${LIVE_DISPLAY_URL:-http://127.0.0.1:5000/}"
USER_UNIT="$USER_HOME/.config/systemd/user/codeglofix-live-display.service"
SERVICE="codeglofix-live-display.service"

fails=0
ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; fails=$((fails+1)); }
note() { echo "  • $*"; }

echo "╔══════════════════════════════════════════════════╗"
echo "║   CodeGloFix — verify PyQt live-display install   ║"
echo "╚══════════════════════════════════════════════════╝"

# ── Resolve the app dir ───────────────────────────────────────────────────────
APP_DIR=""
for cand in "$APP_OVERRIDE" "/opt/codeglofix/app" \
            "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; do
    [ -n "$cand" ] && [ -f "$cand/kiosk/run_live_display.sh" ] && { APP_DIR="$cand"; break; }
done
[ -n "$APP_DIR" ] && note "app dir: $APP_DIR" || note "app dir: (not found — using script parent)"

# ── Resolve the venv (same order run_live_display.sh uses) ────────────────────
VENV=""
for cand in "$VENV_OVERRIDE" "${LIVE_DISPLAY_VENV:-}" "${CODEGLOFIX_VENV:-}" \
            "/opt/codeglofix/venv" "/opt/digitweb-attendance/venv" \
            "${APP_DIR:+$APP_DIR/../venv}" "${APP_DIR:+$APP_DIR/venv}"; do
    [ -n "$cand" ] && [ -x "$cand/bin/python" ] && { VENV="$cand"; break; }
done
[ -n "$VENV" ] && note "venv: $VENV" || bad "no venv with a python found (pass --venv <dir>)"
echo

# ── 1. requirements-display.txt present ───────────────────────────────────────
echo "── 1. requirements-display.txt ──"
if [ -n "$APP_DIR" ] && [ -f "$APP_DIR/requirements-display.txt" ]; then
    ok "present: $APP_DIR/requirements-display.txt"
else
    bad "requirements-display.txt not found under app dir"
fi

# ── 2. PyQt6 + PyQt6-WebEngine import in the venv ─────────────────────────────
echo "── 2. PyQt6 / PyQt6-WebEngine import in venv ──"
if [ -n "$VENV" ]; then
    if "$VENV/bin/python" - >/dev/null 2>&1 <<'PY'
import PyQt6.QtWidgets, PyQt6.QtWebEngineWidgets
PY
    then
        ok "PyQt6 + PyQt6-WebEngine import OK ($("$VENV/bin/python" -c 'from PyQt6.QtCore import QT_VERSION_STR;print("Qt "+QT_VERSION_STR)' 2>/dev/null))"
    else
        bad "PyQt6/PyQt6-WebEngine NOT importable in $VENV"
        echo "     Install:  $VENV/bin/pip install -r $APP_DIR/requirements-display.txt"
        echo "     If import still fails, add system Qt libs:"
        echo "        sudo apt-get install -y libxcb-cursor0 libnss3 libxkbcommon0 libgl1"
    fi
fi

# ── 3. Backend health reachable ───────────────────────────────────────────────
echo "── 3. Backend health ──"
code="$(curl -sk -o /dev/null -m 5 -w '%{http_code}' "$HEALTH_URL" 2>/dev/null)"
if [ "$code" = "200" ]; then
    ok "health reachable: $HEALTH_URL → 200"
else
    bad "health NOT reachable: $HEALTH_URL → ${code:-000}"
    echo "     Check the backend:  systemctl status codeglofix-access@$(id -un) --no-pager"
fi
pcode="$(curl -sk -o /dev/null -m 5 -w '%{http_code}' "$PAGE_URL" 2>/dev/null)"
[ "$pcode" = "200" ] && ok "live page reachable: $PAGE_URL → 200" \
    || note "live page $PAGE_URL → ${pcode:-000} (page loads once backend is fully up)"

# ── 4. systemd --user unit file + state ───────────────────────────────────────
echo "── 4. systemd --user service ──"
if [ -f "$USER_UNIT" ]; then
    ok "unit file present: $USER_UNIT"
    grep -q '__PROJECT_DIR__\|__VENV_DIR__' "$USER_UNIT" 2>/dev/null \
        && bad "unit still has unrendered __PROJECT_DIR__/__VENV_DIR__ placeholders" \
        || ok "unit placeholders rendered"
    en="$(systemctl --user is-enabled "$SERVICE" 2>/dev/null || echo unknown)"
    ac="$(systemctl --user is-active  "$SERVICE" 2>/dev/null || echo unknown)"
    [ "$en" = "enabled" ] && ok "service enabled ($en)" || bad "service not enabled (is-enabled=$en)"
    [ "$ac" = "active" ]  && ok "service active ($ac)"  || note "service is '$ac' (starts on graphical login; start now: systemctl --user start $SERVICE)"
else
    bad "no --user unit at $USER_UNIT (re-run ./deploy/install_customer_pc.sh)"
fi

# ── 5. Displays Qt can see (screen-selection preview) ─────────────────────────
echo "── 5. Displays ──"
if [ -n "$VENV" ]; then
    QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-}" "$VENV/bin/python" - <<'PY' 2>/dev/null || note "Qt could not enumerate screens here (no graphical session in this shell?) — try xrandr"
import os
if (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE","").lower()=="wayland") and os.environ.get("QT_QPA_PLATFORM","") in ("","xcb"):
    os.environ["QT_QPA_PLATFORM"]="wayland"
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication
app=QApplication([])
ss=QGuiApplication.screens()
pr=QGuiApplication.primaryScreen()
print("  detected %d screen(s):"%len(ss))
for i,s in enumerate(ss):
    g=s.geometry()
    print("    [%d] %r %dx%d primary=%s"%(i,s.name(),g.width(),g.height(),s is pr))
PY
fi
command -v xrandr >/dev/null 2>&1 && { echo "  xrandr connected outputs:"; xrandr --query 2>/dev/null | awk '/ connected/{print "    "$0}'; }

# ── 6. Log file path ──────────────────────────────────────────────────────────
echo "── 6. Live-display log ──"
if [ -w /var/log/codeglofix ] 2>/dev/null || [ -f /var/log/codeglofix/live_display.log ]; then
    note "log: /var/log/codeglofix/live_display.log"
else
    note "log: $APP_DIR/kiosk/live_display.log (fallback)"
fi
echo "     tail -f /var/log/codeglofix/live_display.log 2>/dev/null || tail -f \"$APP_DIR/kiosk/live_display.log\""

echo
if [ "$fails" -eq 0 ]; then
    echo "  ════════ PyQt DISPLAY VERIFY: PASS ════════"
    exit 0
else
    echo "  ════════ PyQt DISPLAY VERIFY: FAIL ($fails check(s)) ════════"
    exit 1
fi
