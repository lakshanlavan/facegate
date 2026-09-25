#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# install_customer_pc.sh — Clean PRODUCTION install of CodeGloFix on a new Ubuntu PC
#
# Production layout (FHS):
#   /opt/codeglofix/app          application code (root-owned, read-only to app)
#   /opt/codeglofix/venv         Python virtualenv
#   /var/lib/codeglofix          gym.db, enrolments/shared/faces/*.npz, gate_config
#   /var/backups/codeglofix      backups
#   /var/log/codeglofix          logs
#   /etc/codeglofix-access.env   secrets/env (chmod 600, root)
#   ~/Desktop                    ONLY the CodeGloFix Admin / Live View launcher icons
#
# The customer never sees the source folder on the Desktop.
#
# RUN AS THE NORMAL DESKTOP USER (not root). It uses sudo where required.
# Usage:
#   cd <unpacked-package>/deploy
#   ./install_customer_pc.sh [options]
#
# Options (see --help):
#   --test-mode-no-hardware   install + verify with no camera/relay/7" display
#   --no-autostart            do not auto-start the PyQt live display on boot
#   --verify-only             run only the post-install verification, change nothing
#   --uninstall-old-digitgym  remove a previous DigitGym/gym-access install first
#
# The live gate display is the PyQt6 app (kiosk/live_display.py); it is installed
# unconditionally and is the ONLY live view. There is no Chromium kiosk anymore.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Guards ────────────────────────────────────────────────────────────────────
if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: run as the normal desktop user, not root (the script calls sudo itself)."
    exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
    echo "ERROR: this installer targets Ubuntu/Debian (apt-get not found)."
    exit 1
fi

# ── Paths / identity ──────────────────────────────────────────────────────────
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"          # the project root in the package
RUN_USER="$(id -un)"
USER_HOME="$HOME"

APP_DIR=/opt/codeglofix/app
VENV_DIR=/opt/codeglofix/venv
DATA_DIR=/var/lib/codeglofix
LOG_DIR=/var/log/codeglofix
BACKUP_DIR=/var/backups/codeglofix
ENV_FILE=/etc/codeglofix-access.env
ICON="$APP_DIR/static/codeglofix.png"
SERVICE="codeglofix-access@${RUN_USER}.service"
BACKUP_TIMER="codeglofix-backup@${RUN_USER}.timer"
# The outside door display is the PyQt live view, auto-started on graphical login
# by a systemd --user service (not a Chromium autostart .desktop anymore).
LIVE_DISPLAY_UNIT="codeglofix-live-display.service"
USER_UNIT_DIR="$USER_HOME/.config/systemd/user"
USER_UNIT="$USER_UNIT_DIR/$LIVE_DISPLAY_UNIT"

# ── CLI options ───────────────────────────────────────────────────────────────
# Production policy: the app runtime should be Python 3.10–3.12 (3.12 preferred).
# Python 3.13/3.14 is EXPERIMENTAL only and never chosen unless
# --allow-experimental-python is given.
PYTHON_OVERRIDE=""
ALLOW_EXPERIMENTAL=false
PRODUCTION_ONLY=false
WHEELHOUSE=""
TEST_MODE_NO_HW=false
NO_AUTOSTART=false
VERIFY_ONLY=false
UNINSTALL_OLD=false
HARDWARE_CHECK=false
YES_RELAY_TEST=false
while [ $# -gt 0 ]; do
    case "$1" in
        --python)   PYTHON_OVERRIDE="${2:-}"; shift 2 ;;
        --python=*) PYTHON_OVERRIDE="${1#*=}"; shift ;;
        --production-python)         PRODUCTION_ONLY=true; shift ;;
        --allow-experimental-python) ALLOW_EXPERIMENTAL=true; shift ;;
        --wheelhouse)   WHEELHOUSE="${2:-}"; shift 2 ;;
        --wheelhouse=*) WHEELHOUSE="${1#*=}"; shift ;;
        --test-mode-no-hardware)  TEST_MODE_NO_HW=true; shift ;;
        --no-autostart)           NO_AUTOSTART=true; shift ;;
        # Deprecated: the PyQt live display is now the only live view and is always
        # installed, so these flags are no-ops kept only so old automation/wrappers
        # do not abort on an "Unknown option". --no-kiosk-autostart maps to the new
        # --no-autostart.
        --with-pyqt-display|--prefer-pyqt-display)
            echo "NOTE: $1 is deprecated and ignored — the PyQt live display is always installed now."
            shift ;;
        --no-kiosk-autostart)
            echo "NOTE: --no-kiosk-autostart is deprecated; treating it as --no-autostart."
            NO_AUTOSTART=true; shift ;;
        --verify-only)            VERIFY_ONLY=true; shift ;;
        --uninstall-old-digitgym) UNINSTALL_OLD=true; shift ;;
        --hardware-check)         HARDWARE_CHECK=true; shift ;;
        --yes-relay-test)         YES_RELAY_TEST=true; shift ;;
        -h|--help)
            cat <<'USAGE'
Usage: install_customer_pc.sh [options]

Runtime selection:
  --python <bin>                force a specific interpreter (e.g. python3.12)
  --production-python           use only Python 3.10–3.12 (default behaviour)
  --allow-experimental-python   also try Python 3.13/3.14 (experimental)
  --wheelhouse <dir>            install offline from a local wheel directory

Install behaviour:
  --test-mode-no-hardware       install + verify on a PC with NO camera/relay and
                                NO 7" display. Sets VIDEO_SOURCE to a bundled test
                                video if present, otherwise a no-camera placeholder,
                                so the service still starts and verifies.
  --no-autostart                do NOT auto-start the PyQt live display on boot
                                (install its systemd --user service disabled; use
                                the "CodeGloFix Live View" desktop icon to open it
                                manually, or enable the service later)
  --verify-only                 run ONLY the post-install verification (service,
                                live view=200, admin=401, 127.0.0.1:5000 bind,
                                desktop launchers, live-display service) and change
                                nothing
  --uninstall-old-digitgym      run cleanup_old_digitgym_install.sh first to remove
                                a previous DigitGym/gym-access install, then install

The live gate display is always the PyQt6 app (kiosk/live_display.py) — installed
unconditionally into the app venv and auto-started on graphical login by the
codeglofix-live-display.service systemd --user unit. There is no Chromium kiosk.

Hardware validation:
  --hardware-check              after install, validate the connected camera and
                                ESP32 relay (tools/hardware_check.py). Camera: open +
                                capture a frame. Relay: PING/PONG serial handshake.
                                If hardware fails, the installer exits non-zero with
                                "HARDWARE VALIDATION FAILED". Skipped automatically
                                under --test-mode-no-hardware.
  --yes-relay-test              with --hardware-check, also fire a one-second relay
                                TEST pulse WITHOUT the interactive prompt (the door
                                may release). Omit it to be prompted before pulsing.
USAGE
            exit 0 ;;
        *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

# --production-python wins if both are passed (production reliability first).
if [ "$PRODUCTION_ONLY" = true ] && [ "$ALLOW_EXPERIMENTAL" = true ]; then
    echo "NOTE: --production-python overrides --allow-experimental-python (no 3.14)."
    ALLOW_EXPERIMENTAL=false
fi

step() { echo; echo "═══ $* ═══"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }

# ── Detect Ubuntu version ─────────────────────────────────────────────────────
UBUNTU_PRETTY="unknown"; UBUNTU_VER="unknown"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    UBUNTU_PRETTY="${PRETTY_NAME:-${NAME:-Linux} ${VERSION_ID:-}}"
    UBUNTU_VER="${VERSION_ID:-unknown}"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Post-install VERIFICATION  (also runnable standalone via --verify-only)
# Returns non-zero if any mandatory check fails.
# ──────────────────────────────────────────────────────────────────────────────
run_verification() {
    local fails=0
    step "VERIFY  Post-install health checks"

    # 1. Service active within 60s. InsightFace/onnxruntime model load on the very
    #    first boot can take well over 15s on a slow customer PC, so give it 60s
    #    before declaring failure (the import itself is already proven in step 11).
    echo "  • Waiting up to 60s for $SERVICE to become active…"
    local i active="inactive"
    for i in $(seq 1 60); do
        active="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
        [ "$active" = "active" ] && break
        sleep 1
    done
    if [ "$active" = "active" ]; then
        ok "service is active ($SERVICE)"
    else
        warn "FAIL: $SERVICE is '$active' after 60s"
        echo "        journalctl -u $SERVICE -n 40 --no-pager"
        fails=$((fails+1))
    fi

    # 1b. Wait for the app to actually SERVE through nginx before the point-in-time
    #     checks below. systemd reports the unit "active" the moment uvicorn's
    #     process starts, but on the very first boot InsightFace/onnxruntime load
    #     their models for 30–90s AFTER that — during which 127.0.0.1:5000 is not
    #     yet bound and nginx returns 502/503/504. Without this wait the live-view/
    #     admin checks fire too early and FALSE-FAIL (exactly what the customer saw,
    #     then a later --verify-only PASSed). We poll GET / (NOT HEAD: GET / is 200
    #     but HEAD / is 405) and GET /admin until both are ready, or up to KMAX secs.
    #     GET / is the live-view page the PyQt display (and admins) load.
    local lvcode="000" acode="000" w KMAX=150
    echo "  • Waiting up to ${KMAX}s for the app to serve through nginx (cold InsightFace model load)…"
    for w in $(seq 1 "$KMAX"); do
        lvcode="$(curl -k -s -o /dev/null -m 5 -w '%{http_code}' https://localhost/ 2>/dev/null || echo 000)"
        acode="$(curl -k -s -o /dev/null -m 5 -w '%{http_code}' https://localhost/admin 2>/dev/null || echo 000)"
        [ "$lvcode" = "200" ] && [ "$acode" = "401" ] && break
        sleep 1
    done

    # 2. App bound ONLY on 127.0.0.1:5000 (never 0.0.0.0).
    local ss_out; ss_out="$(ss -ltn 2>/dev/null || true)"
    if echo "$ss_out" | grep -q '127.0.0.1:5000'; then
        if echo "$ss_out" | grep -E '(\*|0\.0\.0\.0):5000' >/dev/null; then
            warn "FAIL: app is also bound on a public address (:5000 not localhost-only)"
            fails=$((fails+1))
        else
            ok "app bound on 127.0.0.1:5000 only (not exposed on the LAN)"
        fi
    else
        warn "FAIL: nothing listening on 127.0.0.1:5000"
        fails=$((fails+1))
    fi

    # 2b. nginx active.
    if [ "$(systemctl is-active nginx 2>/dev/null || true)" = "active" ]; then
        ok "nginx is active"
    else
        warn "FAIL: nginx is not active"
        fails=$((fails+1))
    fi

    # 2c. Health watchdog timer active AND armed (a timer with next_elapse=infinity
    #     is "active" but never fires — an inert watchdog).
    if [ "$(systemctl is-active codeglofix-health.timer 2>/dev/null || true)" = "active" ]; then
        he_next="$(systemctl show codeglofix-health.timer -p NextElapseUSecMonotonic --value 2>/dev/null)"
        if [ -n "$he_next" ] && [ "$he_next" != "infinity" ]; then
            ok "health watchdog timer active with a scheduled next run"
        else
            warn "FAIL: codeglofix-health.timer has no next elapse (inert timer)"
            fails=$((fails+1))
        fi
    else
        warn "FAIL: codeglofix-health.timer is not active"
        fails=$((fails+1))
    fi

    # 3. nginx live-view page → 200 (GET; from the readiness poll above).
    if [ "$lvcode" = "200" ]; then ok "live view https://localhost/ → 200"
    else warn "FAIL: live view https://localhost/ → $lvcode (expected 200 after ${KMAX}s)"; fails=$((fails+1)); fi

    # 4. nginx admin → 401 (login required, no cookie).
    if [ "$acode" = "401" ]; then ok "admin https://localhost/admin → 401 (login required)"
    else warn "FAIL: admin https://localhost/admin → $acode (expected 401 after ${KMAX}s)"; fails=$((fails+1)); fi

    # 5. Desktop launchers exist and point at executable scripts.
    local DESKTOP_DIR; DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$USER_HOME/Desktop")"
    local d exec_target
    for d in CodeGloFix-Admin CodeGloFix-LiveView; do
        local df="$DESKTOP_DIR/$d.desktop"
        if [ -f "$df" ]; then
            exec_target="$(sed -n 's/^Exec=//p' "$df" | awk '{print $1}')"
            if [ -x "$exec_target" ]; then
                ok "launcher $d.desktop → $exec_target (executable)"
            else
                warn "FAIL: launcher $d.desktop points at non-executable '$exec_target'"
                fails=$((fails+1))
            fi
        else
            warn "FAIL: desktop launcher missing: $df"
            fails=$((fails+1))
        fi
    done

    # 6. open_admin.sh works without a 7" display (script present + a CERT-BYPASS
    #    browser exists). Only Chromium/Chrome get --ignore-certificate-errors in
    #    the launcher; firefox / xdg-open would hit the self-signed cert wall, so
    #    they are NOT counted as sufficient here.
    if [ -x "$APP_DIR/kiosk/open_admin.sh" ]; then
        if command -v google-chrome >/dev/null 2>&1 || command -v google-chrome-stable >/dev/null 2>&1; then
            ok "open_admin.sh present + Google Chrome available (admin opens, cert bypassed)"
        elif { command -v chromium-browser >/dev/null 2>&1 && ! is_snap_browser chromium-browser; } \
             || { command -v chromium >/dev/null 2>&1 && ! is_snap_browser chromium; }; then
            ok "open_admin.sh present + non-snap Chromium available (admin opens, cert bypassed)"
        elif command -v chromium-browser >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1; then
            warn "FAIL: only a SNAP Chromium is installed — it misbehaves under confinement and the Admin icon may not open."
            echo  "        Install Google Chrome (.deb) so the launcher works reliably:"
            echo  "          curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && sudo apt install -y /tmp/chrome.deb"
            fails=$((fails+1))
        elif command -v firefox >/dev/null 2>&1 || command -v xdg-open >/dev/null 2>&1; then
            warn "FAIL: only a non-Chromium browser found — it will hit the self-signed cert."
            echo  "        Install Google Chrome so the launcher can bypass it:"
            echo  "          curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && sudo apt install -y /tmp/chrome.deb"
            fails=$((fails+1))
        else
            warn "FAIL: no browser found for open_admin.sh."
            echo  "        curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && sudo apt install -y /tmp/chrome.deb"
            fails=$((fails+1))
        fi
    else
        warn "FAIL: $APP_DIR/kiosk/open_admin.sh missing or not executable"
        fails=$((fails+1))
    fi

    # 7. PyQt live-display autostart: the systemd --user unit must be installed, and
    #    (unless --no-autostart) enabled so the door display comes up on login.
    if [ -f "$USER_UNIT" ]; then
        if grep -q '__PROJECT_DIR__\|__VENV_DIR__' "$USER_UNIT" 2>/dev/null; then
            warn "FAIL: $LIVE_DISPLAY_UNIT still has unrendered __PROJECT_DIR__/__VENV_DIR__ placeholders"
            fails=$((fails+1))
        else
            ok "PyQt live-display --user unit installed ($USER_UNIT)"
        fi
        lv_enabled="$(systemctl --user is-enabled "$LIVE_DISPLAY_UNIT" 2>/dev/null | head -n1)"
        if [ "$NO_AUTOSTART" = true ]; then
            ok "live-display autostart intentionally disabled (--no-autostart); enabled state: ${lv_enabled:-unknown}"
        elif [ "$lv_enabled" = "enabled" ]; then
            ok "PyQt live-display autostart enabled (opens on graphical login)"
        else
            warn "live-display --user service not 'enabled' here (state: ${lv_enabled:-unknown})."
            echo  "        If no user systemd bus was available at install, enable after login:"
            echo  "          systemctl --user enable --now $LIVE_DISPLAY_UNIT"
        fi
    else
        warn "FAIL: PyQt live-display --user unit missing: $USER_UNIT"
        fails=$((fails+1))
    fi

    # 7b. Power-recovery: the --user service only runs AFTER a graphical login.
    #     For the 7" display to come up unattended after a power cut, GNOME
    #     auto-login must be ON. Advisory only — we never edit gdm config for you.
    if [ "$NO_AUTOSTART" != true ] && [ -f "$USER_UNIT" ]; then
        if grep -qsiE '^[[:space:]]*AutomaticLoginEnable[[:space:]]*=[[:space:]]*(true|1)' /etc/gdm3/custom.conf 2>/dev/null; then
            ok "GNOME auto-login enabled (live display opens unattended after power-on)"
        else
            warn "ACTION: live-display autostart is on but GNOME auto-login appears OFF."
            echo  "        After a power cut the 7\" display will wait at the login screen."
            echo  "        Enable it: Settings → Users → Automatic Login = On (run user)."
        fi
    fi

    # 7c. A bad application/x-desktop MIME handler makes double-clicking an icon
    #     open it in an editor/browser instead of running it. A full install already
    #     auto-removes this from the USER's mimeapps.list (with a .bak); anything
    #     still reported here (e.g. system-wide /etc/xdg, or in --verify-only mode)
    #     is advisory — we never edit /etc/xdg for you.
    local ml mime_bad=""
    for ml in "$USER_HOME/.config/mimeapps.list" \
              "$USER_HOME/.local/share/applications/mimeapps.list" \
              /etc/xdg/mimeapps.list; do
        [ -f "$ml" ] && grep -qE '^[[:space:]]*application/x-desktop[[:space:]]*=' "$ml" 2>/dev/null && mime_bad="$ml"
    done
    if [ -n "$mime_bad" ]; then
        warn "ACTION: application/x-desktop is overridden in $mime_bad"
        echo  "        Remove that one line so the icons launch instead of opening in an app:"
        echo  "          sed -i '/^application\\/x-desktop=/d' \"$mime_bad\""
    else
        ok "no application/x-desktop MIME override (icons launch normally)"
    fi

    # 8. Database auto-created + healthy after the service started.
    #    Checks: file exists, integrity_check=ok, the five required tables exist,
    #    default plans + settings were seeded, and the data dir is writable by the
    #    service user (so gym.db / gym.db-wal / gym.db-shm can be written).
    local DBP dbout dbrc
    DBP="$(sudo sed -n 's/^CODEGLOFIX_DB_PATH=//p' "$ENV_FILE" 2>/dev/null | tail -1)"
    [ -n "$DBP" ] || DBP="$DATA_DIR/gym.db"
    if [ ! -f "$DBP" ]; then
        warn "FAIL: Database initialization failed. — no database at $DBP"
        echo  "        The service did not create the DB on first start. Inspect:"
        echo  "        journalctl -u $SERVICE -n 60 --no-pager"
        fails=$((fails+1))
    else
        dbout="$("$VENV_DIR/bin/python" - "$DBP" <<'PYEOF' 2>&1
import os, sqlite3, sys
db = sys.argv[1]
need = ["members", "payments", "membership_plans", "access_logs", "system_settings"]
try:
    con = sqlite3.connect(db)
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    if ic != "ok":
        print("integrity_check=%s" % ic); sys.exit(1)
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    miss = [t for t in need if t not in have]
    if miss:
        print("missing tables: %s" % ",".join(miss)); sys.exit(1)
    plans = con.execute("SELECT COUNT(*) FROM membership_plans").fetchone()[0]
    setts = con.execute("SELECT COUNT(*) FROM system_settings").fetchone()[0]
    if plans < 1:
        print("no default membership_plans seeded"); sys.exit(1)
    if setts < 1:
        print("no default system_settings seeded"); sys.exit(1)
    d = os.path.dirname(os.path.abspath(db))
    if not os.access(d, os.W_OK):
        print("data dir not writable by service user: %s" % d); sys.exit(1)
    print("integrity=ok tables=5 plans=%d settings=%d dir_writable=yes" % (plans, setts))
except Exception as e:
    print("db error: %s" % e); sys.exit(1)
PYEOF
)"; dbrc=$?
        if [ "$dbrc" -eq 0 ]; then
            ok "database healthy at $DBP ($dbout)"
        else
            warn "FAIL: Database initialization failed. — $dbout ($DBP)"
            fails=$((fails+1))
        fi
    fi

    # 9. No old DigitGym/gym-access units left enabled (no orphans shadowing us).
    local old_enabled
    old_enabled="$(find /etc/systemd/system -type l \
        \( -name 'gym-access@*' -o -name 'digitgym-backup@*' \) 2>/dev/null)"
    old_enabled="$old_enabled$(systemctl list-unit-files 2>/dev/null \
        | grep -E '^(gym-access@|digitgym-backup@)' | grep -i enabled || true)"
    if [ -z "$old_enabled" ]; then
        ok "no old DigitGym/gym-access units enabled"
    else
        warn "FAIL: old DigitGym/gym-access units still enabled:"
        echo "$old_enabled" | sed 's/^/        /'
        echo  "        Remove them: ./deploy/cleanup_old_digitgym_install.sh"
        fails=$((fails+1))
    fi

    echo
    if [ "$fails" -eq 0 ]; then
        echo "  ════════ VERIFICATION: PASS ════════"
        return 0
    else
        echo "  ════════ VERIFICATION: FAIL ($fails check(s)) ════════"
        return 1
    fi
}

# --verify-only: don't touch anything, just report.
if [ "$VERIFY_ONLY" = true ]; then
    echo "╔══════════════════════════════════════════════════╗"
    echo "║   CodeGloFix — VERIFY ONLY (no changes made)      ║"
    echo "╚══════════════════════════════════════════════════╝"
    run_verification
    exit $?
fi

# --uninstall-old-digitgym: remove a previous DigitGym/gym-access install first.
if [ "$UNINSTALL_OLD" = true ]; then
    if [ -x "$DEPLOY_DIR/cleanup_old_digitgym_install.sh" ]; then
        echo "Running cleanup_old_digitgym_install.sh before installing…"
        "$DEPLOY_DIR/cleanup_old_digitgym_install.sh" || {
            echo "Cleanup did not complete — aborting install."; exit 1; }
    else
        echo "ERROR: cleanup_old_digitgym_install.sh not found next to this installer."
        exit 1
    fi
fi

echo "╔══════════════════════════════════════════════════╗"
echo "║   CodeGloFix — Customer PC Production Installer    ║"
echo "╚══════════════════════════════════════════════════╝"
echo "  user        : $RUN_USER"
echo "  ubuntu      : $UBUNTU_PRETTY"
echo "  source pkg  : $SRC_DIR"
echo "  install to  : $APP_DIR"
[ -n "$PYTHON_OVERRIDE" ] && echo "  python      : forced → $PYTHON_OVERRIDE"
[ "$TEST_MODE_NO_HW" = true ] && echo "  mode        : TEST (no camera/relay/7\" display required)"
[ "$NO_AUTOSTART" = true ] && echo "  live view   : autostart DISABLED (--no-autostart)"
read -rp "Proceed with installation? [y/N] " ans
[ "${ans,,}" = "y" ] || { echo "Aborted."; exit 0; }

# ── 1. Dependencies ───────────────────────────────────────────────────────────
step "1/13  Verifying Ubuntu dependencies"
PKGS=(python3 python3-venv python3-pip nginx openssl curl sqlite3 rsync unclutter iproute2)
MISSING=()
for p in "${PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "  Installing: ${MISSING[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${MISSING[@]}"
fi
ok "base dependencies present"

# ── Browser for the Admin launcher ────────────────────────────────────────────
# The admin launcher opens Chromium/Chrome with --ignore-certificate-errors so the
# self-signed localhost cert never blocks the page. BROWSER POLICY (Ubuntu
# 22/24/26): PREFER Google Chrome (.deb) — it is identical and reliable across
# all three releases. On 22.04+ the apt 'chromium-browser' package is only a
# SNAP shim (it pulls the snap, which can be absent on minimal systems and
# behaves differently under confinement); that is why a PC with no real browser
# could not open the admin page until Google Chrome was installed by hand. We
# therefore install Google Chrome when no browser exists, accept an already
# present Chrome/Chromium as valid, and only fall back to apt chromium-browser
# if the Chrome .deb cannot be fetched (offline).
# True only if $1 (a command name) resolves to a snap-confined binary.
is_snap_browser() {
    local bin real
    bin="$(command -v "$1" 2>/dev/null)" || return 1
    real="$(readlink -f "$bin" 2>/dev/null || echo "$bin")"
    case "$real" in */snap/*|/snap/*) return 0 ;; *) return 1 ;; esac
}
ensure_browser() {
    # 1. Google Chrome (.deb) already present → done, this is the preferred browser.
    if command -v google-chrome >/dev/null 2>&1 || command -v google-chrome-stable >/dev/null 2>&1; then
        ok "browser present: $(command -v google-chrome google-chrome-stable 2>/dev/null | head -1) (Google Chrome)"
        return 0
    fi
    # 2. A real (non-snap) Chromium .deb is also acceptable.
    if command -v chromium-browser >/dev/null 2>&1 && ! is_snap_browser chromium-browser; then
        ok "browser present: $(command -v chromium-browser) (non-snap Chromium)"; return 0
    fi
    if command -v chromium >/dev/null 2>&1 && ! is_snap_browser chromium; then
        ok "browser present: $(command -v chromium) (non-snap Chromium)"; return 0
    fi
    # 3. Only a SNAP Chromium exists (or no browser at all). A confined snap browser
    #    cannot reliably open the launcher's --user-data-dir — that was the exact
    #    cause of the customer's Admin icon failing — so install Google Chrome even
    #    though a snap chromium is technically "present".
    if command -v chromium-browser >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1; then
        warn "only a SNAP Chromium is installed — it misbehaves under confinement; installing Google Chrome (.deb)…"
    fi
    local arch; arch="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
    if [ "$arch" = "amd64" ]; then
        echo "  • No browser found — installing Google Chrome (.deb; reliable on Ubuntu 22/24/26)…"
        local deb; deb="$(mktemp --suffix=.deb)"
        if curl -fsSL -o "$deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
           && sudo apt-get install -y "$deb"; then
            rm -f "$deb"; ok "Google Chrome installed"; return 0
        fi
        rm -f "$deb"
        warn "Google Chrome download failed (offline?) — trying apt chromium-browser…"
    else
        warn "non-amd64 ($arch): Google Chrome .deb unavailable — trying apt chromium-browser…"
    fi
    if sudo apt-get install -y chromium-browser \
       && { command -v chromium-browser >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1; }; then
        ok "Chromium installed (fallback)"; return 0
    fi
    warn "FAIL: could not install a browser — the Admin icon needs one."
    echo  "        Install Google Chrome manually, then re-run with --verify-only:"
    echo  "          curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
    echo  "          sudo apt install -y /tmp/chrome.deb"
    [ "$TEST_MODE_NO_HW" = true ] && return 0
    exit 1
}
ensure_browser
# The service user needs the 'video' group (camera /dev/video*) and 'dialout'
# group (ESP32 relay /dev/ttyUSB*,/dev/ttyACM*) to access the hardware. usermod
# is idempotent; the systemd service picks the groups up on (re)start.
for grp in video dialout; do
    if getent group "$grp" >/dev/null 2>&1 && ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "$grp"; then
        sudo usermod -aG "$grp" "$RUN_USER" && ok "added $RUN_USER to '$grp' group"
    fi
done

# ── 2. App code → /opt/codeglofix/app (root-owned) ────────────────────────────
step "2/13  Installing application code to $APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo rsync -a --delete \
    --exclude '.git' --exclude '.claude' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'venv' --exclude '../venv' \
    --exclude '*.mp4' \
    --exclude '*.log' \
    --exclude 'logs' \
    --exclude 'gym.db' --exclude 'gym.db-wal' --exclude 'gym.db-shm' \
    --exclude '.env' \
    --exclude 'enrolments' \
    --exclude 'gate_config.json' \
    "$SRC_DIR/." "$APP_DIR/"
sudo chown -R root:root "$APP_DIR"
sudo find "$APP_DIR" -type d -exec chmod 755 {} +
sudo find "$APP_DIR" -type f -exec chmod 644 {} +
sudo chmod 755 "$APP_DIR/start.sh" "$APP_DIR/backup_db.sh" \
               "$APP_DIR/kiosk/run_live_display.sh" "$APP_DIR/kiosk/open_admin.sh"
ok "code installed (root-owned, read-only to the app)"

# ── 3. Python virtualenv (multi-version aware) ────────────────────────────────
step "3/13  Selecting Python and building virtualenv at $VENV_DIR"

SMOKE_IMPORTS="fastapi, uvicorn, cv2, numpy, insightface, onnxruntime, serial"

declare -A CAND_BYVER=()
add_cand() {
    local bin path ver major minor
    bin="$1"
    command -v "$bin" >/dev/null 2>&1 || return 0
    path="$(command -v "$bin")"
    ver="$("$path" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || return 0
    [ -n "$ver" ] || return 0
    major="${ver%%.*}"; minor="${ver##*.}"
    [ "$major" = "3" ] && [ "$minor" -ge 10 ] || return 0
    [ -n "${CAND_BYVER[$ver]:-}" ] || CAND_BYVER["$ver"]="$path"
}

is_experimental() {  # true for Python 3.13+
    local minor="${1##*.}"
    [ "${1%%.*}" = "3" ] && [ "$minor" -ge 13 ]
}

have_production_python() {
    [ -n "${CAND_BYVER[3.10]:-}" ] || [ -n "${CAND_BYVER[3.11]:-}" ] || [ -n "${CAND_BYVER[3.12]:-}" ]
}

ORDER=()
if [ -n "$PYTHON_OVERRIDE" ]; then
    add_cand "$PYTHON_OVERRIDE"
    if [ "${#CAND_BYVER[@]}" -eq 0 ]; then
        echo "ERROR: --python '$PYTHON_OVERRIDE' is not usable (missing, or older than 3.10)."
        exit 1
    fi
    for v in "${!CAND_BYVER[@]}"; do ORDER+=("$v"); done
    if is_experimental "${ORDER[0]}"; then
        echo "  ⚠ WARNING: Python ${ORDER[0]} is experimental for InsightFace."
        echo "    Production recommended runtime is Python 3.12."
    fi
else
    for n in python3.12 python3.11 python3.10 python3; do add_cand "$n"; done
    if ! have_production_python; then
        if apt-cache show python3.12 >/dev/null 2>&1; then
            echo "  No Python 3.10–3.12 found — installing python3.12 from official repos…"
            sudo apt-get update -qq || true
            sudo apt-get install -y python3.12 python3.12-venv python3.12-dev >/dev/null 2>&1 || true
            add_cand python3.12
        fi
    fi
    for v in 3.12 3.11 3.10; do [ -n "${CAND_BYVER[$v]:-}" ] && ORDER+=("$v"); done
    if [ "$ALLOW_EXPERIMENTAL" = true ]; then
        for n in python3.13 python3.14; do add_cand "$n"; done
        while IFS= read -r v; do
            [ -n "$v" ] && is_experimental "$v" && ORDER+=("$v")
        done < <(for v in "${!CAND_BYVER[@]}"; do is_experimental "$v" && echo "$v"; done | sort -V)
    fi
fi

echo "  Ubuntu        : $UBUNTU_PRETTY"
echo "  Policy        : production Python 3.10–3.12$([ "$ALLOW_EXPERIMENTAL" = true ] && echo '  (+experimental 3.13/3.14 enabled)')"
echo "  Python try    : ${ORDER[*]:-(none usable)}"

ensure_venv_support() {
    local v="$1" bin="$2" pkg="python$1-venv"
    "$bin" -c 'import ensurepip, venv' 2>/dev/null && return 0
    if apt-cache show "$pkg" >/dev/null 2>&1; then
        echo "     installing $pkg (official repo)…"
        sudo apt-get install -y "$pkg" >/dev/null 2>&1 || true
    elif apt-cache show python3-venv >/dev/null 2>&1; then
        sudo apt-get install -y python3-venv >/dev/null 2>&1 || true
    fi
}

ensure_build_support() {
    local v="$1" minor="${1##*.}"
    { [ "${v%%.*}" = "3" ] && [ "$minor" -ge 13 ]; } || return 0
    for pkg in build-essential "python$v-dev"; do
        dpkg -s "$pkg" >/dev/null 2>&1 && continue
        if apt-cache show "$pkg" >/dev/null 2>&1; then
            echo "     installing $pkg (official repo, for source builds)…"
            sudo apt-get install -y "$pkg" >/dev/null 2>&1 || true
        fi
    done
}

req_for_python() {
    local v="$1" tag minor
    tag="py${v//./}"
    if [ -f "$APP_DIR/requirements-$tag.txt" ]; then echo "requirements-$tag.txt"; return; fi
    minor="${v##*.}"
    if [ "${v%%.*}" = "3" ] && [ "$minor" -ge 13 ] && [ -f "$APP_DIR/requirements-py314.txt" ]; then
        echo "requirements-py314.txt"; return
    fi
    echo "requirements.txt"
}

SELECTED_PYTHON=""; PYTHON_VER=""; REQ_FILE_USED=""; RUNTIME_EXPERIMENTAL=false
for v in "${ORDER[@]}"; do
    pybin="${CAND_BYVER[$v]}"
    echo "  ── Trying Python $v  ($pybin) ─────────────────────────────"
    if is_experimental "$v"; then
        echo "     ⚠ Python $v is EXPERIMENTAL for InsightFace (0.7.3 may fail to build)."
        echo "       Production recommended runtime is Python 3.12."
    fi
    ensure_venv_support "$v" "$pybin"
    ensure_build_support "$v"
    REQ_FILE="$(req_for_python "$v")"

    sudo mkdir -p "$(dirname "$VENV_DIR")"
    sudo rm -rf "$VENV_DIR"
    sudo install -d -o "$RUN_USER" -g "$RUN_USER" "$VENV_DIR"

    if ! "$pybin" -m venv "$VENV_DIR" 2>/dev/null; then
        echo "     ✗ venv creation failed — next"; continue
    fi
    OFFLINE=false
    PIP_SRC=(-r "$APP_DIR/$REQ_FILE")
    if [ -n "$WHEELHOUSE" ]; then
        if [ -d "$WHEELHOUSE" ]; then
            echo "     using offline wheelhouse: $WHEELHOUSE"
            OFFLINE=true
            PIP_SRC=(--no-index --find-links "$WHEELHOUSE" -r "$APP_DIR/$REQ_FILE")
        else
            echo "     ⚠ wheelhouse '$WHEELHOUSE' not found — falling back to online install"
        fi
    fi
    if [ "$OFFLINE" = true ]; then
        "$VENV_DIR/bin/pip" install --no-index --find-links "$WHEELHOUSE" --upgrade pip wheel >/dev/null 2>&1 || true
    else
        "$VENV_DIR/bin/pip" install --upgrade pip wheel >/dev/null 2>&1 \
            || { echo "     ✗ pip/wheel upgrade failed — next"; continue; }
    fi
    echo "     installing $REQ_FILE (several minutes)…"
    if ! "$VENV_DIR/bin/pip" install "${PIP_SRC[@]}"; then
        echo "     ✗ requirements ($REQ_FILE) failed to install on Python $v — next"; continue
    fi
    echo "     import smoke test: $SMOKE_IMPORTS"
    if ! "$VENV_DIR/bin/python" - <<'PYEOF'
import importlib, sys
mods = ["fastapi","uvicorn","cv2","numpy","insightface","onnxruntime","serial"]
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append(f"{m}: {e}")
if bad:
    print("FAILED imports:\n  " + "\n  ".join(bad)); sys.exit(1)
print("all imports OK")
PYEOF
    then
        echo "     ✗ smoke test failed on Python $v — next"; continue
    fi
    SELECTED_PYTHON="$pybin"; PYTHON_VER="$v"; REQ_FILE_USED="$REQ_FILE"
    is_experimental "$v" && RUNTIME_EXPERIMENTAL=true
    break
done

if [ -z "$SELECTED_PYTHON" ]; then
    sudo rm -rf "$VENV_DIR"
    echo
    echo "ERROR: Could not build a working CodeGloFix runtime on this machine."
    echo "  Ubuntu : $UBUNTU_PRETTY"
    echo "  Tried  : ${ORDER[*]:-<no usable Python found>}"
    echo "  Recommended fix — install Python 3.12 from the official Ubuntu repos:"
    echo "      sudo apt install python3.12 python3.12-venv python3.12-dev"
    echo "      $0 --python python3.12"
    exit 1
fi
ok "virtualenv ready on Python $PYTHON_VER ($SELECTED_PYTHON) using $REQ_FILE_USED"
[ "$RUNTIME_EXPERIMENTAL" = true ] && warn "Running on EXPERIMENTAL Python $PYTHON_VER — for production, migrate to 3.12."

# ── 3b. PyQt live-display wrapper deps (REQUIRED — this is the only live view) ─
# Display deps, kept OUT of the backend requirements.txt on purpose (PyQt6 +
# WebEngine are large). Installed into the SAME app venv — never globally. The
# systemd --user service that auto-starts the display is installed + enabled later
# (step 13). The live view is the PyQt app; there is no Chromium kiosk fallback.
step "3b/13  PyQt live-display wrapper (required)"
if [ -f "$APP_DIR/requirements-display.txt" ]; then
    DISP_OK=false
    # Offline first when a wheelhouse is given. build_wheelhouse.sh bundles the
    # PyQt6/PyQt6-WebEngine wheels by default; if this wheelhouse predates that
    # (no PyQt wheels), fall back to an ONLINE install on a connected PC.
    if [ -n "$WHEELHOUSE" ] && [ -d "$WHEELHOUSE" ]; then
        echo "  • trying offline PyQt install from wheelhouse $WHEELHOUSE"
        if "$VENV_DIR/bin/pip" install --no-index --find-links "$WHEELHOUSE" \
               -r "$APP_DIR/requirements-display.txt"; then
            DISP_OK=true
            ok "PyQt6 live-display deps installed from wheelhouse into $VENV_DIR"
        else
            warn "wheelhouse has no PyQt6/PyQt6-WebEngine wheels — retrying ONLINE…"
            echo "     (rebuild the wheelhouse with display deps to keep installs fully offline:"
            echo "        deploy/build_wheelhouse.sh python3.12 requirements-display.txt )"
        fi
    fi
    if [ "$DISP_OK" != true ]; then
        if "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements-display.txt"; then
            DISP_OK=true
            ok "PyQt6 live-display deps installed (online) into $VENV_DIR"
        else
            warn "PyQt6 live-display deps FAILED to install — the live gate display cannot run."
            echo "     Fix the venv (or wheelhouse) and re-run; the backend/admin still install."
        fi
    fi
    # Prove the two modules actually import before we rely on the service.
    if [ "$DISP_OK" = true ]; then
        if "$VENV_DIR/bin/python" -c "import PyQt6.QtWidgets, PyQt6.QtWebEngineWidgets" 2>/dev/null; then
            ok "PyQt6 + PyQt6-WebEngine import OK in $VENV_DIR"
        else
            warn "PyQt6/PyQt6-WebEngine installed but NOT importable — check system Qt libs."
            echo "     (e.g. sudo apt-get install -y libxcb-cursor0 libnss3 libxkbcommon0)"
        fi
    fi
else
    warn "requirements-display.txt not found in $APP_DIR — the PyQt live display cannot be installed."
fi
if [ -f "$APP_DIR/kiosk/run_live_display.sh" ]; then
    sudo chmod 755 "$APP_DIR/kiosk/run_live_display.sh"
    ok "kiosk/run_live_display.sh is executable"
fi
echo "  • live_display.py, run_live_display.sh + service template under $APP_DIR/kiosk"
echo "  • autostart --user service installed + enabled in step 13"

# ── 4. Persistent data → /var/lib/codeglofix ──────────────────────────────────
step "4/13  Preparing data directory $DATA_DIR"
sudo mkdir -p "$DATA_DIR/enrolments/shared/faces"
sudo chown -R "$RUN_USER:$RUN_USER" "$DATA_DIR"
sudo chmod 750 "$DATA_DIR"
if [ -f "$DATA_DIR/gym.db" ]; then
    ok "existing database kept ($DATA_DIR/gym.db)"
else
    ok "empty database will be auto-created on first start"
fi
if [ ! -f "$DATA_DIR/gate_config.json" ] && [ -f "$SRC_DIR/gate_config.json" ]; then
    sudo -u "$RUN_USER" cp "$SRC_DIR/gate_config.json" "$DATA_DIR/gate_config.json"
fi

# ── 4b. No-hardware test mode: pick a VIDEO_SOURCE that needs no camera ────────
# Decide here; written into the env file in step 6 (or overridden if env exists).
VIDEO_SOURCE_OVERRIDE=""
if [ "$TEST_MODE_NO_HW" = true ]; then
    if [ -f "$SRC_DIR/test1.mp4" ]; then
        sudo -u "$RUN_USER" cp "$SRC_DIR/test1.mp4" "$DATA_DIR/test1.mp4"
        VIDEO_SOURCE_OVERRIDE="$DATA_DIR/test1.mp4"
    elif [ -f "$SRC_DIR/test2.mp4" ]; then
        sudo -u "$RUN_USER" cp "$SRC_DIR/test2.mp4" "$DATA_DIR/test2.mp4"
        VIDEO_SOURCE_OVERRIDE="$DATA_DIR/test2.mp4"
    else
        # No bundled test clip → no-camera placeholder. The app starts and serves
        # the live view/admin; only the live video shows "no camera".
        VIDEO_SOURCE_OVERRIDE="none"
    fi
    ok "TEST MODE: VIDEO_SOURCE=$VIDEO_SOURCE_OVERRIDE (no camera/relay required)"
fi

# ── 5. Logs + backups ─────────────────────────────────────────────────────────
step "5/13  Creating log and backup directories"
sudo mkdir -p "$LOG_DIR" "$BACKUP_DIR"
# Recursive on the log dir: a previous root-run (or systemd append) can leave
# backend.log root-owned inside an otherwise-correct dir, which makes start.sh's
# first write fail with "Permission denied" and the service exits immediately.
sudo chown -R "$RUN_USER:$RUN_USER" "$LOG_DIR"
sudo chown "$RUN_USER:$RUN_USER" "$BACKUP_DIR"
sudo chmod 750 "$LOG_DIR" "$BACKUP_DIR"
# Ensure backend.log itself exists and is owned/writable by the service user.
LOG_FILE="$LOG_DIR/backend.log"
if sudo test -f "$LOG_FILE"; then
    sudo chown "$RUN_USER:$RUN_USER" "$LOG_FILE"
    sudo chmod 640 "$LOG_FILE"
else
    sudo install -o "$RUN_USER" -g "$RUN_USER" -m 640 /dev/null "$LOG_FILE"
fi
ok "$LOG_DIR and $BACKUP_DIR ready ($LOG_FILE owned by $RUN_USER)"

# Prove the service user can actually write the log dir AND append to backend.log
# BEFORE we ever start the service — this is the exact operation start.sh does at
# launch, so catching it here turns a silent crash-loop into a clear install error.
if ! sudo -u "$RUN_USER" test -w "$LOG_DIR" \
   || ! sudo -u "$RUN_USER" sh -c "echo '[install] log writability check' >> '$LOG_FILE'"; then
    warn "FAIL: Log file is not writable by service user."
    echo  "        $LOG_FILE must be writable by $RUN_USER (the service user)."
    echo  "        Fix: sudo chown -R $RUN_USER:$RUN_USER $LOG_DIR && sudo chmod 640 $LOG_FILE"
    exit 1
fi
ok "log directory + backend.log are writable by $RUN_USER"

# ── 6. /etc/codeglofix-access.env (secrets) ───────────────────────────────────
step "6/13  Configuring $ENV_FILE"
if sudo test -f "$ENV_FILE"; then
    ok "env file already exists — leaving secrets untouched"
else
    echo "  Generating SESSION_SECRET and setting the admin password."
    SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    while :; do
        read -rsp "  Enter NEW admin panel password: " PW1; echo
        read -rsp "  Confirm admin password:         " PW2; echo
        [ -n "$PW1" ] && [ "$PW1" = "$PW2" ] && break
        echo "  Passwords empty or do not match — try again."
    done
    TMP_ENV="$(mktemp)"
    sed -e "s#__SESSION_SECRET__#$SESSION_SECRET#g" \
        -e "s#__ADMIN_PASSWORD__#$PW1#g" \
        "$DEPLOY_DIR/codeglofix-access.env.template" > "$TMP_ENV"
    sudo cp "$TMP_ENV" "$ENV_FILE"
    rm -f "$TMP_ENV"
    unset PW1 PW2 SESSION_SECRET
    ok "env file created"
fi
# Apply the no-hardware VIDEO_SOURCE override (whether the env was new or kept).
if [ -n "$VIDEO_SOURCE_OVERRIDE" ]; then
    if sudo grep -q '^VIDEO_SOURCE=' "$ENV_FILE"; then
        sudo sed -i "s#^VIDEO_SOURCE=.*#VIDEO_SOURCE=$VIDEO_SOURCE_OVERRIDE#" "$ENV_FILE"
    else
        echo "VIDEO_SOURCE=$VIDEO_SOURCE_OVERRIDE" | sudo tee -a "$ENV_FILE" >/dev/null
    fi
    ok "VIDEO_SOURCE set to $VIDEO_SOURCE_OVERRIDE in $ENV_FILE"
fi
sudo chown root:root "$ENV_FILE"
sudo chmod 600 "$ENV_FILE"
ok "$ENV_FILE secured (root:root 600)"

# ── 7. systemd units ──────────────────────────────────────────────────────────
step "7/13  Installing systemd service + backup timer"
sudo cp "$DEPLOY_DIR/codeglofix-access@.service"  /etc/systemd/system/codeglofix-access@.service
sudo cp "$DEPLOY_DIR/codeglofix-backup@.service"  /etc/systemd/system/codeglofix-backup@.service
sudo cp "$DEPLOY_DIR/codeglofix-backup@.timer"    /etc/systemd/system/codeglofix-backup@.timer
# Health watchdog (borrowed from DigitFace): non-templated units that probe
# /api/health and restart the access instance if it becomes unhealthy/hung.
sudo cp "$DEPLOY_DIR/codeglofix-health.service"   /etc/systemd/system/codeglofix-health.service
sudo cp "$DEPLOY_DIR/codeglofix-health.timer"     /etc/systemd/system/codeglofix-health.timer
# The health probe is shipped in the app tree and called directly from the unit.
sudo install -m 0755 "$DEPLOY_DIR/codeglofix-health-check.sh" "$APP_DIR/deploy/codeglofix-health-check.sh"
# USB autosuspend prevention (reduces camera-freeze risk on long-running displays).
if [ ! -f /etc/udev/rules.d/50-usb-no-autosuspend.rules ]; then
    sudo cp "$DEPLOY_DIR/50-usb-no-autosuspend.rules" /etc/udev/rules.d/50-usb-no-autosuspend.rules
    sudo udevadm control --reload-rules 2>/dev/null || true
fi
sudo systemctl daemon-reload
ok "units installed (access + backup + health watchdog)"

# ── 8. logrotate ──────────────────────────────────────────────────────────────
step "8/13  Installing log rotation"
TMP_LR="$(mktemp)"
sed -e "s#__USER__#$RUN_USER#g" "$DEPLOY_DIR/codeglofix.logrotate" > "$TMP_LR"
sudo cp "$TMP_LR" /etc/logrotate.d/codeglofix
sudo chown root:root /etc/logrotate.d/codeglofix
sudo chmod 644 /etc/logrotate.d/codeglofix
rm -f "$TMP_LR"
ok "logrotate configured"

# ── 9. nginx + TLS ────────────────────────────────────────────────────────────
step "9/13  Configuring nginx (HTTPS reverse proxy)"
sudo cp "$APP_DIR/gym-access.nginx" /etc/nginx/sites-available/face-attendance
sudo ln -sf /etc/nginx/sites-available/face-attendance /etc/nginx/sites-enabled/face-attendance
sudo rm -f /etc/nginx/sites-enabled/default
if [ ! -f /etc/nginx/ssl/face-attendance.crt ]; then
    sudo mkdir -p /etc/nginx/ssl
    sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout /etc/nginx/ssl/face-attendance.key \
        -out    /etc/nginx/ssl/face-attendance.crt \
        -subj "/CN=localhost/O=CodeGloFix/C=LK" 2>/dev/null
fi
sudo nginx -t
ok "nginx configured"

# ── 10. Desktop launchers ONLY (no source folder on Desktop) ──────────────────
step "10/13  Installing desktop launchers (Admin + Live View)"
APPS_DIR="$USER_HOME/.local/share/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$USER_HOME/Desktop")"
mkdir -p "$APPS_DIR" "$DESKTOP_DIR"
render() {  # render <src.desktop> <dest>
    sed -e "s#__PROJECT_DIR__#$APP_DIR#g" -e "s#__ICON__#$ICON#g" "$1" > "$2"
    chmod +x "$2"
    gio set "$2" "metadata::trusted" true 2>/dev/null || true
}
# Exactly two icons: Admin (Chrome-based, unchanged) + Live View (PyQt display).
# Clean up any stale Chrome-kiosk artifacts from a previous install so nothing
# lingers — including the on-boot AUTOSTART entry (and its .disabled variant), so
# a direct over-install on an old kiosk machine never leaves an active Chrome
# kiosk autostart fighting the PyQt live-display service. The Admin icon is never
# touched here.
rm -f "$APPS_DIR/CodeGloFix-Kiosk.desktop" "$DESKTOP_DIR/CodeGloFix-Kiosk.desktop" \
      "$USER_HOME/.config/autostart/CodeGloFix-Kiosk.desktop" \
      "$USER_HOME/.config/autostart/CodeGloFix-Kiosk.desktop.disabled"
render "$APP_DIR/kiosk/CodeGloFix-Admin.desktop" "$APPS_DIR/CodeGloFix-Admin.desktop"
render "$APP_DIR/kiosk/CodeGloFix-Admin.desktop" "$DESKTOP_DIR/CodeGloFix-Admin.desktop"
render "$APP_DIR/kiosk/CodeGloFix-LiveView.desktop" "$APPS_DIR/CodeGloFix-LiveView.desktop"
render "$APP_DIR/kiosk/CodeGloFix-LiveView.desktop" "$DESKTOP_DIR/CodeGloFix-LiveView.desktop"
ok "Admin + Live View icons on Desktop and app menu"

# A user-level 'application/x-desktop=' MIME override makes double-clicking an icon
# open it in an editor/browser instead of running it (seen on the customer PC). We
# safely remove ONLY that line from the USER's own mimeapps.list files, keeping a
# timestamped .bak first. We never touch /etc/xdg (system-wide, not ours to edit) —
# verification still reports that one as an ACTION if present.
mime_ts="$(date +%Y%m%d-%H%M%S 2>/dev/null || echo backup)"
for ml in "$USER_HOME/.config/mimeapps.list" \
          "$USER_HOME/.local/share/applications/mimeapps.list"; do
    if [ -f "$ml" ] && grep -qE '^[[:space:]]*application/x-desktop[[:space:]]*=' "$ml" 2>/dev/null; then
        cp -a "$ml" "$ml.bak-$mime_ts" 2>/dev/null || true
        sed -i '/^[[:space:]]*application\/x-desktop[[:space:]]*=/d' "$ml"
        ok "removed application/x-desktop override from $ml (backup: $ml.bak-$mime_ts)"
    fi
done

# ── 11. Pre-flight: prove the app imports BEFORE we hand it to systemd ─────────
# A fresh customer PC can pass the camera/relay hardware checks (those use v4l2 /
# serial, not the Python stack) yet still fail `import main` — e.g. a missing
# system lib for cv2 (libGL.so.1), or an InsightFace/onnxruntime import error.
# uvicorn would only log a flat "Could not import module 'main'" and the unit
# would crash-loop. Import it here, under the real run user + env file, so the
# installer aborts immediately with the genuine traceback instead of "finishing"
# a broken install that 502s.
step "11/13  Pre-flight import check"
echo "  • cd $APP_DIR && python -c 'import main'  (as $RUN_USER, with $ENV_FILE)"
# set +e: capture the import failure ourselves so we can print the real traceback
# instead of letting `set -e` abort the installer with no explanation.
set +e
IMPORT_OUT="$(cd "$APP_DIR" && sudo -u "$RUN_USER" env $(sudo grep -vE '^\s*#|^\s*$' "$ENV_FILE" | xargs) \
    PYTHONPATH="$APP_DIR" "$VENV_DIR/bin/python" -c "import main; print('IMPORT OK')" 2>&1)"
IMPORT_RC=$?
set -e
if [ "$IMPORT_RC" -ne 0 ] || ! echo "$IMPORT_OUT" | grep -q 'IMPORT OK'; then
    warn "FAIL: the application could not be imported — NOT starting the service."
    echo
    echo "──────────── real Python traceback ────────────"
    echo "$IMPORT_OUT" | sed 's/^/  /'
    echo "────────────────────────────────────────────────"
    echo
    echo "  This is the true root cause of the failed install. Common fixes:"
    echo "    • cv2 'libGL.so.1' / 'libgthread' → sudo apt-get install -y libgl1 libglib2.0-0"
    echo "    • onnxruntime / insightface import error → rebuild the venv from the wheelhouse"
    echo "  Fix the above, then re-run this installer."
    exit 1
fi
ok "import main → IMPORT OK"

# ── 12. Enable + start services ───────────────────────────────────────────────
step "12/13  Enabling and starting services"
sudo systemctl enable nginx
sudo systemctl restart nginx
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"
sudo systemctl enable "$BACKUP_TIMER"
sudo systemctl start  "$BACKUP_TIMER"
# Health watchdog: enable at boot + start now, then run one check to bootstrap
# the OnUnitActiveSec cadence so the timer always has a real next-elapse
# (OnBootSec covers a fresh boot; this covers a mid-session install/upgrade).
sudo systemctl enable --now codeglofix-health.timer 2>/dev/null || true
sudo systemctl restart codeglofix-health.timer 2>/dev/null || true
sudo systemctl start codeglofix-health.service 2>/dev/null || true
he_next="$(systemctl show codeglofix-health.timer -p NextElapseUSecMonotonic --value 2>/dev/null)"
if [ -n "$he_next" ] && [ "$he_next" != "infinity" ]; then
    ok "health watchdog timer scheduled (next elapse set)"
else
    warn "health watchdog timer has no next elapse yet — will arm on next boot (OnBootSec=60)"
fi
ok "nginx + $SERVICE + health watchdog enabled and started"

# ── 13. Outside 7" door display: PyQt live-display autostart ──────────────────
# The PyQt live view is the ONLY door display. It auto-starts on graphical login
# via a systemd --user service (runs inside the desktop user's session, so
# Wayland/DISPLAY/XAUTHORITY are correct — WebEngine needs a real session).
# --no-autostart installs the unit but leaves it DISABLED (manual start / icon).
step "13/13  Outside 7\" door display (PyQt live-display autostart)"
mkdir -p "$USER_UNIT_DIR"
if [ -f "$APP_DIR/kiosk/codeglofix-live-display.service" ]; then
    sed -e "s#__PROJECT_DIR__#$APP_DIR#g" \
        -e "s#__VENV_DIR__#$VENV_DIR#g" \
        "$APP_DIR/kiosk/codeglofix-live-display.service" \
        > "$USER_UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
    if [ "$NO_AUTOSTART" = true ]; then
        systemctl --user disable "$LIVE_DISPLAY_UNIT" 2>/dev/null || true
        ok "live-display --user unit installed but autostart DISABLED (--no-autostart)."
        echo "     Start manually with the \"CodeGloFix Live View\" icon, or enable later:"
        echo "       systemctl --user enable --now $LIVE_DISPLAY_UNIT"
    else
        if systemctl --user enable "$LIVE_DISPLAY_UNIT" 2>/dev/null; then
            ok "PyQt live-display --user service enabled (auto-starts on graphical login)"
        else
            warn "could not enable --user service (no user systemd bus in this context)."
            echo "     Enable later:  systemctl --user enable --now $LIVE_DISPLAY_UNIT"
        fi
        if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ] \
           && systemctl --user start "$LIVE_DISPLAY_UNIT" 2>/dev/null; then
            ok "PyQt live-display started now"
        else
            echo "  • No graphical session here — start after login with:"
            echo "      systemctl --user start $LIVE_DISPLAY_UNIT"
        fi
        echo "  NOTE: for unattended power-cut recovery, enable GNOME auto-login so the"
        echo "        graphical session (and this --user service) start without a login."
    fi
else
    warn "service template not found in $APP_DIR/kiosk — cannot install the --user service."
    echo "     The \"CodeGloFix Live View\" desktop icon still launches the display manually."
fi

# ── Record install info ───────────────────────────────────────────────────────
step "Recording install_info.txt"
INFO_FILE="$DATA_DIR/install_info.txt"
{
    echo "CodeGloFix Access Control — installation record"
    echo "==============================================="
    echo "Install date : $(date '+%Y-%m-%d %H:%M:%S %z')"
    echo "Ubuntu       : $UBUNTU_PRETTY (VERSION_ID=$UBUNTU_VER)"
    echo "Python       : $PYTHON_VER  ($SELECTED_PYTHON)"
    echo "Runtime tier : $([ "$RUNTIME_EXPERIMENTAL" = true ] && echo 'EXPERIMENTAL (3.13+/InsightFace risk — migrate to 3.12)' || echo 'production (3.10–3.12)')"
    echo "Requirements : $REQ_FILE_USED"
    echo "Test mode    : $TEST_MODE_NO_HW"
    echo "Venv         : $VENV_DIR"
    echo "App dir      : $APP_DIR"
    echo "Installed by : $RUN_USER"
    echo
    echo "pip freeze:"
    "$VENV_DIR/bin/pip" freeze 2>/dev/null | sed 's/^/  /'
} > "$INFO_FILE"
chmod 640 "$INFO_FILE" 2>/dev/null || true
ok "wrote $INFO_FILE"

# ── Verify the running install ────────────────────────────────────────────────
VERIFY_RC=0
run_verification || VERIFY_RC=$?

# ── Optional hardware validation (camera + ESP32 relay) ───────────────────────
HW_RC=0
HW_CAMERA="not run"; HW_RELAY="not run"; HW_PULSE="not run"; HW_OVERALL="not run"
DB_FOR_HW="$(sudo sed -n 's/^CODEGLOFIX_DB_PATH=//p' "$ENV_FILE" 2>/dev/null | tail -1)"
[ -n "$DB_FOR_HW" ] || DB_FOR_HW="$DATA_DIR/gym.db"
if [ "$HARDWARE_CHECK" = true ] && [ "$TEST_MODE_NO_HW" = true ]; then
    step "Hardware validation SKIPPED (--test-mode-no-hardware)"
    HW_CAMERA="skipped"; HW_RELAY="skipped"; HW_PULSE="skipped"; HW_OVERALL="skipped (test mode)"
elif [ "$HARDWARE_CHECK" = true ]; then
    step "Validating connected hardware (camera + ESP32 relay)"
    HW_VS="$(sudo sed -n 's/^VIDEO_SOURCE=//p' "$ENV_FILE" 2>/dev/null | tail -1)";   [ -n "$HW_VS" ]   || HW_VS=0
    HW_BAUD="$(sudo sed -n 's/^RELAY_BAUDRATE=//p' "$ENV_FILE" 2>/dev/null | tail -1)"; [ -n "$HW_BAUD" ] || HW_BAUD=115200
    HW_PORT="$(sudo sed -n 's/^RELAY_PORT=//p' "$ENV_FILE" 2>/dev/null | tail -1)";     [ -n "$HW_PORT" ] || HW_PORT=auto
    HW_FLAGS=(--all)
    if [ "$YES_RELAY_TEST" = true ]; then HW_FLAGS+=(--yes-relay-test); else HW_FLAGS+=(--relay-test-confirm); fi
    HW_REPORT="$(mktemp)"
    # Run as the service user so freshly-added video/dialout groups apply and the
    # test frame is written under the service-owned data dir. stdout stays on the
    # terminal so the relay-pulse confirmation prompt is visible.
    sudo -u "$RUN_USER" env VIDEO_SOURCE="$HW_VS" RELAY_PORT="$HW_PORT" RELAY_BAUDRATE="$HW_BAUD" \
        CODEGLOFIX_DB_PATH="$DB_FOR_HW" \
        "$VENV_DIR/bin/python" "$APP_DIR/tools/hardware_check.py" "${HW_FLAGS[@]}" \
        --report-file "$HW_REPORT" || HW_RC=$?
    HW_CAMERA="$(sed -n 's/^camera=//p' "$HW_REPORT" 2>/dev/null | head -1)";        [ -n "$HW_CAMERA" ] || HW_CAMERA="(no result)"
    HW_RELAY="$(sed -n 's/^relay_serial=//p' "$HW_REPORT" 2>/dev/null | head -1)";    [ -n "$HW_RELAY" ]  || HW_RELAY="(no result)"
    HW_PULSE="$(sed -n 's/^relay_test=//p' "$HW_REPORT" 2>/dev/null | head -1)";      [ -n "$HW_PULSE" ]  || HW_PULSE="not run"
    HW_OVERALL="$(sed -n 's/^overall=//p' "$HW_REPORT" 2>/dev/null | head -1)";       [ -n "$HW_OVERALL" ] || HW_OVERALL="(no result)"
    rm -f "$HW_REPORT"
    if [ "$HW_RC" -ne 0 ]; then
        warn "HARDWARE VALIDATION FAILED (camera=$HW_CAMERA  relay_serial=$HW_RELAY)"
    else
        ok "hardware validation passed (camera=$HW_CAMERA  relay_serial=$HW_RELAY)"
    fi
fi

# ── Final install report ──────────────────────────────────────────────────────
REPORT_FILE="$DATA_DIR/install_report.txt"
_svc="$(systemctl is-active "$SERVICE" 2>/dev/null || echo inactive)"
_nginx="$(systemctl is-active nginx 2>/dev/null || echo inactive)"
_liveview="$(curl -k -s -o /dev/null -w '%{http_code}' https://localhost/ 2>/dev/null || echo 000)"
_admin="$(curl -k -s -o /dev/null -w '%{http_code}' https://localhost/admin 2>/dev/null || echo 000)"
if [ -f "$DB_FOR_HW" ]; then
    _db="exists; integrity=$("$VENV_DIR/bin/python" -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" "$DB_FOR_HW" 2>/dev/null || echo unknown)"
else
    _db="MISSING (database initialization failed)"
fi
FINAL_PASS="PASS"
{ [ "$VERIFY_RC" -ne 0 ] || { [ "$HARDWARE_CHECK" = true ] && [ "$TEST_MODE_NO_HW" != true ] && [ "$HW_RC" -ne 0 ]; }; } && FINAL_PASS="FAIL"
{
    echo "CodeGloFix Access Control — installation report"
    echo "==============================================="
    echo "Install date        : $(date '+%Y-%m-%d %H:%M:%S %z')"
    echo "Ubuntu version      : $UBUNTU_PRETTY (VERSION_ID=$UBUNTU_VER)"
    echo "Python version      : $PYTHON_VER ($SELECTED_PYTHON)"
    echo "App service ($SERVICE) : $_svc"
    echo "nginx status        : $_nginx"
    echo "Database status     : $_db"
    echo "Live view endpoint  : https://localhost/      → $_liveview (expect 200)"
    echo "Admin endpoint      : https://localhost/admin → $_admin (expect 401)"
    echo "App bind            : $(ss -ltn 2>/dev/null | grep -q '127.0.0.1:5000' && echo '127.0.0.1:5000 (localhost only)' || echo 'NOT BOUND')"
    echo "Test mode           : $TEST_MODE_NO_HW"
    echo "Hardware check       : $([ "$HARDWARE_CHECK" = true ] && echo requested || echo 'not requested')"
    echo "  camera check       : $HW_CAMERA"
    echo "  relay serial check : $HW_RELAY"
    echo "  relay pulse test   : $HW_PULSE"
    echo "  hardware overall   : $HW_OVERALL"
    echo "Verification result : $([ "$VERIFY_RC" -eq 0 ] && echo PASS || echo FAIL)"
    echo
    echo "FINAL: $FINAL_PASS"
} > "$REPORT_FILE"
chmod 640 "$REPORT_FILE" 2>/dev/null || true
ok "wrote $REPORT_FILE"
echo
echo "  ════════ INSTALL FINAL: $FINAL_PASS ════════"
[ "$FINAL_PASS" = "FAIL" ] && [ "$HARDWARE_CHECK" = true ] && [ "$TEST_MODE_NO_HW" != true ] && [ "$HW_RC" -ne 0 ] \
    && echo "  HARDWARE VALIDATION FAILED — fix the camera/relay and re-run --hardware-check."

# Roll the hardware-check result into the overall exit code.
[ "$HW_RC" -ne 0 ] && [ "$HARDWARE_CHECK" = true ] && [ "$TEST_MODE_NO_HW" != true ] && VERIFY_RC=1

# ── Done ──────────────────────────────────────────────────────────────────────
cat <<EOF

╔══════════════════════════════════════════════════╗
║                INSTALL COMPLETE                   ║
╚══════════════════════════════════════════════════╝

  Ubuntu : $UBUNTU_PRETTY
  Python : $PYTHON_VER ($SELECTED_PYTHON)
  Record : $INFO_FILE
  Report : $REPORT_FILE   (FINAL: $FINAL_PASS)

Manual re-checks:
  systemctl status $SERVICE --no-pager
  systemctl status nginx --no-pager
  curl -k -o /dev/null -w 'liveview=%{http_code}\n' https://localhost/       # → 200
  curl -k -o /dev/null -w 'admin=%{http_code}\n' https://localhost/admin     # → 401
  ss -ltnp | grep 5000                                                       # 127.0.0.1:5000
  ./deploy/install_customer_pc.sh --verify-only                              # re-run all checks

Validate connected hardware (camera + ESP32 relay):
  ./deploy/install_customer_pc.sh --hardware-check          # prompts before relay pulse
  $VENV_DIR/bin/python $APP_DIR/tools/hardware_check.py --all
  $VENV_DIR/bin/python $APP_DIR/tools/hardware_check.py --relay-test-confirm

Migrate members/data from another PC:
  (on old PC)   ./deploy/export_codeglofix_data.sh
  (on this PC)  ./deploy/import_codeglofix_data.sh /path/to/codeglofix_data_*.tar.gz

Enable / disable the live-display autostart later (systemd --user):
  enable :  systemctl --user enable --now $LIVE_DISPLAY_UNIT
  disable:  systemctl --user disable --now $LIVE_DISPLAY_UNIT
  (or launch it manually any time from the "CodeGloFix Live View" desktop icon)

Remove an OLD DigitGym/gym-access install:
  ./deploy/cleanup_old_digitgym_install.sh

Full guide: $APP_DIR/deploy/CUSTOMER_INSTALL_GUIDE.md
EOF

exit $VERIFY_RC
