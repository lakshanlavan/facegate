#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# install_production.sh — One-shot IN-PLACE production setup for CodeGloFix
# (runs the app from the project folder in your home — used on the dev/bench PC).
#
# For a clean customer machine use deploy/install_customer_pc.sh instead, which
# installs to /opt/codeglofix and verifies the running system.
#
# This script makes the Ubuntu PC self-recovering after power off/on:
#   • codeglofix-access (FastAPI app) → systemd, Restart=always, enabled on boot
#   • nginx            (HTTPS proxy)  → enabled on boot
#   • outside 7" display              → PyQt live view (systemd --user service)
#   • inside admin                    → "CodeGloFix Admin" desktop icon (login)
#   • daily backup                    → systemd timer (Persistent=true)
#   • log rotation                    → logrotate (backend + live-display logs)
#
# The live gate display is the PyQt6 app (kiosk/live_display.py) — it is the ONLY
# live view (there is no Chromium kiosk) and is installed unconditionally.
#
# It also DISABLES any old DigitGym/gym-access units it is replacing, so no orphan
# enabled units are left behind.
#
# RUN AS THE NORMAL DESKTOP USER (NOT root). The script uses sudo where needed.
#   cd <project> && ./install_production.sh [--no-autostart]
#
#   --no-autostart   install the PyQt live-display --user service but leave it
#                    DISABLED (launch it manually from the "CodeGloFix Live View"
#                    icon, or enable the service later).
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: run this as the normal desktop user, not root (it calls sudo itself)."
    exit 1
fi

# ── CLI flags ─────────────────────────────────────────────────────────────────
NO_AUTOSTART=false
while [ $# -gt 0 ]; do
    case "$1" in
        --no-autostart)        NO_AUTOSTART=true; shift ;;
        # Deprecated no-ops (PyQt is now the only live view, always installed):
        --with-pyqt-display|--prefer-pyqt-display)
            echo "NOTE: $1 is deprecated and ignored — the PyQt live display is always installed now."
            shift ;;
        -h|--help)             sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
RUN_USER="$(id -un)"
USER_HOME="$HOME"
ICON="$PROJECT_DIR/static/codeglofix.png"
SERVICE="codeglofix-access@${RUN_USER}.service"
BACKUP_TIMER="codeglofix-backup@${RUN_USER}.timer"
NEW_ENV=/etc/codeglofix-access.env
OLD_ENV=/etc/gym-access.env
# PyQt live-display autostart (systemd --user) — the outside door display.
LIVE_DISPLAY_UNIT="codeglofix-live-display.service"
USER_UNIT_DIR="$USER_HOME/.config/systemd/user"
USER_UNIT="$USER_UNIT_DIR/$LIVE_DISPLAY_UNIT"

echo "=== CodeGloFix in-place production install ==="
echo "  user        : $RUN_USER"
echo "  project dir : $PROJECT_DIR"
echo

# ── 0. Replace any OLD DigitGym/gym-access units (no orphans) ──────────────────
echo "[0/9] Disabling any old DigitGym/gym-access units this replaces..."
for inst in "gym-access@${RUN_USER}.service" \
            "digitgym-backup@${RUN_USER}.timer" \
            "digitgym-backup@${RUN_USER}.service"; do
    if [ -n "$(systemctl is-enabled "$inst" 2>/dev/null || true)" ] \
       || [ "$(systemctl is-active "$inst" 2>/dev/null || true)" = "active" ]; then
        echo "      sudo systemctl disable --now $inst"
        sudo systemctl disable --now "$inst" 2>/dev/null || true
    fi
done
for u in gym-access@.service digitgym-backup@.service digitgym-backup@.timer; do
    if [ -e "/etc/systemd/system/$u" ]; then
        echo "      sudo rm -f /etc/systemd/system/$u"
        sudo rm -f "/etc/systemd/system/$u"
    fi
done
[ -e /etc/logrotate.d/gym-access ] && sudo rm -f /etc/logrotate.d/gym-access || true
[ -e /etc/logrotate.d/digitgym ]   && sudo rm -f /etc/logrotate.d/digitgym   || true
sudo systemctl daemon-reload
sudo systemctl reset-failed 2>/dev/null || true

# ── 1. Desktop dependencies (browser for the Admin icon + curl) ───────────────
# The live view is the PyQt app (no browser needed); the Admin icon still opens
# in Chrome/Chromium, so ensure a browser + curl are present.
echo "[1/9] Checking desktop dependencies (browser for Admin, curl)..."
MISSING=()
command -v curl >/dev/null 2>&1 || MISSING+=(curl)
if ! command -v chromium-browser >/dev/null 2>&1 \
   && ! command -v chromium >/dev/null 2>&1 \
   && ! command -v google-chrome >/dev/null 2>&1; then
    MISSING+=(chromium-browser)
fi
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "  Installing: ${MISSING[*]}"
    sudo apt-get update -qq || true
    sudo apt-get install -y "${MISSING[@]}" || \
        echo "  WARN: could not auto-install ${MISSING[*]} — install manually if the Admin icon fails."
else
    echo "  All present."
fi

# ── 2. Make scripts executable ────────────────────────────────────────────────
echo "[2/9] Making scripts executable..."
chmod +x "$PROJECT_DIR/start.sh" \
         "$PROJECT_DIR/backup_db.sh" \
         "$PROJECT_DIR/kiosk/run_live_display.sh" \
         "$PROJECT_DIR/kiosk/open_admin.sh"

# ── 3. Migrate the env file name (old → new) if needed ─────────────────────────
echo "[3/9] Ensuring $NEW_ENV exists..."
if sudo test -f "$NEW_ENV"; then
    echo "  $NEW_ENV present."
elif sudo test -f "$OLD_ENV"; then
    echo "  Migrating $OLD_ENV → $NEW_ENV (old file kept as fallback)."
    sudo cp "$OLD_ENV" "$NEW_ENV"
    sudo chown root:root "$NEW_ENV"; sudo chmod 600 "$NEW_ENV"
else
    echo "  NOTE: no system env file found. The service reads ADMIN_PASSWORD and"
    echo "        SESSION_SECRET from $NEW_ENV. Create it (chmod 600, root) or the"
    echo "        app will exit on start. See deploy/codeglofix-access.env.template."
fi

# ── 4. systemd units (app + backup) ───────────────────────────────────────────
echo "[4/9] Installing systemd units..."
sudo cp "$PROJECT_DIR/codeglofix-access.service"  /etc/systemd/system/codeglofix-access@.service
sudo cp "$PROJECT_DIR/codeglofix-backup.service"  /etc/systemd/system/codeglofix-backup@.service
sudo cp "$PROJECT_DIR/codeglofix-backup.timer"    /etc/systemd/system/codeglofix-backup@.timer
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl enable "$BACKUP_TIMER"
echo "  Enabled $SERVICE and $BACKUP_TIMER"

# ── 5. nginx site + enable ────────────────────────────────────────────────────
echo "[5/9] Configuring nginx..."
if [ -d /etc/nginx/sites-available ]; then
    sudo cp "$PROJECT_DIR/gym-access.nginx" /etc/nginx/sites-available/face-attendance
    sudo ln -sf /etc/nginx/sites-available/face-attendance \
                /etc/nginx/sites-enabled/face-attendance
    if [ ! -f /etc/nginx/ssl/face-attendance.crt ]; then
        echo "  Generating self-signed TLS cert..."
        sudo mkdir -p /etc/nginx/ssl
        sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
            -keyout /etc/nginx/ssl/face-attendance.key \
            -out    /etc/nginx/ssl/face-attendance.crt \
            -subj "/CN=localhost/O=CodeGloFix/C=LK" 2>/dev/null
    fi
    if sudo nginx -t 2>/dev/null; then
        sudo systemctl enable nginx
        sudo systemctl reload nginx || sudo systemctl restart nginx
        echo "  nginx enabled + reloaded."
    else
        echo "  WARN: 'nginx -t' failed — fix config, then: sudo systemctl reload nginx"
    fi
else
    echo "  WARN: nginx not installed. Install: sudo apt install nginx"
fi

# ── 6. /var/backups dir owned by the user ─────────────────────────────────────
echo "[6/9] Preparing backup directory..."
sudo mkdir -p /var/backups/codeglofix
sudo chown "$RUN_USER:$RUN_USER" /var/backups/codeglofix
sudo chmod 700 /var/backups/codeglofix

# ── 7. logrotate ──────────────────────────────────────────────────────────────
echo "[7/9] Installing logrotate config..."
TMP_LR="$(mktemp)"
sed -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" -e "s#__USER__#$RUN_USER#g" \
    "$PROJECT_DIR/codeglofix.logrotate" > "$TMP_LR"
sudo cp "$TMP_LR" /etc/logrotate.d/codeglofix
sudo chown root:root /etc/logrotate.d/codeglofix
sudo chmod 644 /etc/logrotate.d/codeglofix
rm -f "$TMP_LR"
sudo logrotate --debug /etc/logrotate.d/codeglofix >/dev/null 2>&1 \
    && echo "  logrotate config OK." || echo "  WARN: verify logrotate config."

# ── 8. Desktop launchers (Admin + Live View) ──────────────────────────────────
echo "[8/9] Installing desktop launchers (Admin + Live View)..."
APPS_DIR="$USER_HOME/.local/share/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$USER_HOME/Desktop")"
AUTOSTART_DIR="$USER_HOME/.config/autostart"
mkdir -p "$APPS_DIR" "$DESKTOP_DIR" "$AUTOSTART_DIR"
render() {  # render <src.desktop> <dest>
    sed -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" -e "s#__ICON__#$ICON#g" "$1" > "$2"
    chmod +x "$2"
    gio set "$2" "metadata::trusted" true 2>/dev/null || true
}
# Remove any stale kiosk icon/autostart from a previous install so nothing lingers.
rm -f "$APPS_DIR/CodeGloFix-Kiosk.desktop" "$DESKTOP_DIR/CodeGloFix-Kiosk.desktop" \
      "$AUTOSTART_DIR/CodeGloFix-Kiosk.desktop" "$AUTOSTART_DIR/CodeGloFix-Kiosk.desktop.disabled"
render "$PROJECT_DIR/kiosk/CodeGloFix-Admin.desktop"  "$APPS_DIR/CodeGloFix-Admin.desktop"
render "$PROJECT_DIR/kiosk/CodeGloFix-Admin.desktop"  "$DESKTOP_DIR/CodeGloFix-Admin.desktop"
render "$PROJECT_DIR/kiosk/CodeGloFix-LiveView.desktop" "$APPS_DIR/CodeGloFix-LiveView.desktop"
render "$PROJECT_DIR/kiosk/CodeGloFix-LiveView.desktop" "$DESKTOP_DIR/CodeGloFix-LiveView.desktop"
echo "  Admin + Live View icons placed."

# ── 8b. PyQt live-display wrapper + autostart (REQUIRED — the only live view) ──
# Installs the display deps into the app venv (never globally) and installs +
# enables the systemd --user service that auto-starts the PyQt display on login.
echo "[8b/9] Installing PyQt live-display wrapper + autostart..."
PYQT_VENV="${CODEGLOFIX_VENV:-$PROJECT_DIR/../venv}"
if [ -x "$PYQT_VENV/bin/pip" ]; then
    echo "  Installing requirements-display.txt into $PYQT_VENV ..."
    "$PYQT_VENV/bin/pip" install -r "$PROJECT_DIR/requirements-display.txt" \
        || echo "  WARN: PyQt display deps failed to install — the live display cannot run yet."
else
    echo "  WARN: no project venv at $PYQT_VENV — NOT installing globally."
    echo "        Install manually into your venv:"
    echo "          <venv>/bin/pip install -r \"$PROJECT_DIR/requirements-display.txt\""
fi
chmod +x "$PROJECT_DIR/kiosk/run_live_display.sh" 2>/dev/null || true
echo "  kiosk/run_live_display.sh executable; live_display.py + service template in place."

# Install the systemd --user service and (unless --no-autostart) enable + start it.
mkdir -p "$USER_UNIT_DIR"
sed -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" \
    -e "s#__VENV_DIR__#$PYQT_VENV#g" \
    "$PROJECT_DIR/kiosk/codeglofix-live-display.service" \
    > "$USER_UNIT"
systemctl --user daemon-reload 2>/dev/null || true
if [ "$NO_AUTOSTART" = true ]; then
    systemctl --user disable "$LIVE_DISPLAY_UNIT" 2>/dev/null || true
    echo "  --no-autostart: --user service installed but DISABLED."
    echo "    Launch via the \"CodeGloFix Live View\" icon, or: systemctl --user enable --now $LIVE_DISPLAY_UNIT"
else
    if systemctl --user enable "$LIVE_DISPLAY_UNIT" 2>/dev/null; then
        echo "  PyQt live-display --user service ENABLED (auto-starts on graphical login)."
    else
        echo "  WARN: could not enable --user service (no user systemd bus here)."
        echo "        Enable later:  systemctl --user enable --now $LIVE_DISPLAY_UNIT"
    fi
    if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ] \
       && systemctl --user start "$LIVE_DISPLAY_UNIT" 2>/dev/null; then
        echo "  PyQt live-display started now."
    else
        echo "  No graphical session detected — start after login with:"
        echo "    systemctl --user start $LIVE_DISPLAY_UNIT"
    fi
fi

# ── 9. Start app now ──────────────────────────────────────────────────────────
echo "[9/9] Starting $SERVICE now..."
sudo systemctl restart "$SERVICE" || true
sudo systemctl start  "$BACKUP_TIMER" || true

echo
echo "=== DONE ==="
echo "Verify now (or reboot to test full power-on recovery):"
echo "  systemctl status $SERVICE --no-pager"
echo "  systemctl --user status $LIVE_DISPLAY_UNIT --no-pager   # PyQt live display"
echo "  curl -k -o /dev/null -w 'liveview=%{http_code}\n' https://localhost/     # → 200"
echo "  curl -k -o /dev/null -w 'admin=%{http_code}\n' https://localhost/admin   # → 401"
echo "To DISABLE the live-display autostart:  systemctl --user disable --now $LIVE_DISPLAY_UNIT"
