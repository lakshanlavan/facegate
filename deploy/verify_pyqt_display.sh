#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# verify_pyqt_display.sh — quick status check for the PyQt live display, which is
# the ONE and ONLY outside-door live view (there is no Chromium kiosk anymore).
#
# Confirms the display is installed and set to run:
#   • the systemd --user unit codeglofix-live-display.service exists
#   • it is enabled  (auto-starts on graphical login)
#   • it is active   (running now, if a graphical session is present)
#   • the live-view page is reachable (backend serving)
#
# READ-ONLY: changes nothing, only reports. RUN AS THE NORMAL DESKTOP USER
# (systemd --user is per-user).
#
#   ./deploy/verify_pyqt_display.sh
#
# Exit code:
#   0  PASS  — unit installed AND enabled (active is advisory: needs a login)
#   1  FAIL  — unit missing or not enabled (the door display will not auto-start)
# ──────────────────────────────────────────────────────────────────────────────
set -u

USER_HOME="$HOME"
SERVICE="codeglofix-live-display.service"
USER_UNIT="$USER_HOME/.config/systemd/user/$SERVICE"
PAGE_URL="${LIVE_DISPLAY_URL:-http://127.0.0.1:5000/}"

echo "╔══════════════════════════════════════════════════╗"
echo "║   CodeGloFix — verify PyQt live display           ║"
echo "╚══════════════════════════════════════════════════╝"

# ── Gather state ──────────────────────────────────────────────────────────────
UNIT_PRESENT=false
[ -f "$USER_UNIT" ] && UNIT_PRESENT=true

# is-enabled is the authoritative "will it autostart" signal for the --user unit.
# systemctl prints the state to stdout even when it exits non-zero, so take the
# first line and only default when it printed nothing (avoids a doubled value).
ENABLED_STATE="$(systemctl --user is-enabled "$SERVICE" 2>/dev/null | head -n1)"
[ -n "$ENABLED_STATE" ] || ENABLED_STATE="not-enabled"
ACTIVE_STATE="$(systemctl --user is-active "$SERVICE" 2>/dev/null | head -n1)"
[ -n "$ACTIVE_STATE" ] || ACTIVE_STATE="inactive"
PAGE_CODE="$(curl -sk -o /dev/null -m 5 -w '%{http_code}' "$PAGE_URL" 2>/dev/null)"

echo
echo "── Detected state ──"
echo "  --user unit file exists ..................... $UNIT_PRESENT   ($USER_UNIT)"
echo "  service is-enabled .......................... $ENABLED_STATE"
echo "  service is-active ........................... $ACTIVE_STATE"
echo "  live-view page ($PAGE_URL) ...... ${PAGE_CODE:-000}"

# ── Verdict ───────────────────────────────────────────────────────────────────
echo
echo "── Verdict ──"
rc=0
if [ "$UNIT_PRESENT" != true ]; then
    echo "  ✗ FAIL: PyQt live-display --user unit not installed ($USER_UNIT)."
    echo "          Re-run the installer: ./deploy/install_customer_pc.sh"
    rc=1
elif [ "$ENABLED_STATE" != "enabled" ]; then
    echo "  ✗ FAIL: the live-display service is not enabled (is-enabled=$ENABLED_STATE)."
    echo "          The door display will NOT auto-start on boot. Enable it:"
    echo "            systemctl --user enable --now $SERVICE"
    rc=1
else
    echo "  ✓ PASS: PyQt live display is installed and enabled (auto-starts on login)."
    if [ "$ACTIVE_STATE" = "active" ]; then
        echo "         Service is active now."
    else
        echo "         Service is '$ACTIVE_STATE' (it starts on graphical login; start now with:"
        echo "           systemctl --user start $SERVICE )"
    fi
    [ "$PAGE_CODE" = "200" ] \
        && echo "         Live-view page reachable ($PAGE_URL → 200)." \
        || echo "         NOTE: live-view page $PAGE_URL → ${PAGE_CODE:-000} (loads once the backend is fully up)."
fi

echo
if [ "$rc" -eq 0 ]; then
    echo "  ════════ PyQt LIVE DISPLAY: PASS ════════"
else
    echo "  ════════ PyQt LIVE DISPLAY: FAIL ════════"
fi
exit "$rc"
