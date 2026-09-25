#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# open_admin.sh — Inside admin launcher (CodeGloFix Admin desktop icon).
#
# Opens a NORMAL browser window at the admin login page. This is triggered
# MANUALLY by staff clicking the "CodeGloFix Admin" desktop icon — it never runs
# on boot, so the admin panel is never shown on the outside display.
#
# The admin panel still requires login (admin_session cookie) regardless.
# ──────────────────────────────────────────────────────────────────────────────
set -u

ADMIN_URL="${ADMIN_URL:-https://localhost/admin}"
PROFILE_DIR="${ADMIN_PROFILE_DIR:-$HOME/.config/codeglofix-admin-chrome}"
mkdir -p "$PROFILE_DIR"

# Clear a STALE single-instance lock left by a crashed / killed / power-cut admin
# Chrome. SingletonLock is a symlink (→ host-PID); if the owning process is gone
# Chrome should recreate it, but a broken/leftover lock makes it abort with
# "Failed to create $PROFILE_DIR/SingletonLock" and no window opens. We remove the
# lock ONLY when NO live browser is using this profile — never disturbing a real
# running admin session (which would just open another window normally).
if ! pgrep -f -- "--user-data-dir=$PROFILE_DIR" >/dev/null 2>&1; then
    rm -f "$PROFILE_DIR/SingletonLock" \
          "$PROFILE_DIR/SingletonSocket" \
          "$PROFILE_DIR/SingletonCookie" 2>/dev/null || true
fi

# BROWSER POLICY: prefer the Google Chrome .deb over a (snap) Chromium — a
# confined snap browser cannot reliably open our custom --user-data-dir, which is
# why the Admin icon only worked after Google Chrome was installed. Chrome first;
# a snap Chromium is used only as a last resort when nothing else is present.
CHROME="" SNAP_FALLBACK=""
for c in google-chrome google-chrome-stable chromium-browser chromium; do
    if command -v "$c" >/dev/null 2>&1; then
        bin="$(command -v "$c")"; real="$(readlink -f "$bin" 2>/dev/null || echo "$bin")"
        case "$real" in
            */snap/*|/snap/*) [ -z "$SNAP_FALLBACK" ] && SNAP_FALLBACK="$c" ;;
            *) CHROME="$c"; break ;;
        esac
    fi
done
[ -z "$CHROME" ] && CHROME="$SNAP_FALLBACK"

if [ -n "$CHROME" ]; then
    exec "$CHROME" \
        --new-window \
        --ignore-certificate-errors \
        --test-type \
        --user-data-dir="$PROFILE_DIR" \
        "$ADMIN_URL"
fi

# Fallback to Firefox, then the system default browser.
if command -v firefox >/dev/null 2>&1; then
    exec firefox --new-window "$ADMIN_URL"
fi
exec xdg-open "$ADMIN_URL"
