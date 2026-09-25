#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# remove_previous_install.sh — safely remove a previous CodeGloFix / DigitGym /
# gym-access install so a clean install can proceed.
#
# Removes: services (stopped + disabled), autostart entries, desktop launchers,
# nginx site, systemd unit files, logrotate configs, /opt installs, and env files.
#
# DATABASES ARE NEVER DELETED AUTOMATICALLY. If a database is found you are asked
# to back it up; the data dirs (/var/lib/codeglofix, /var/lib/digitgym) are LEFT
# IN PLACE so the next install can reuse the members/enrolments.
#
# RUN AS THE NORMAL DESKTOP USER (not root). It uses sudo where required.
#   ./deploy/remove_previous_install.sh [--dry-run] [--yes] [--no-db-backup]
#
#   --dry-run        print every action, change NOTHING
#   --yes            assume "yes" to prompts (still backs the DB up unless --no-db-backup)
#   --no-db-backup   skip the database backup (data dirs are still preserved)
# ──────────────────────────────────────────────────────────────────────────────
set -u

if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: run as the normal desktop user, not root (it calls sudo itself)."
    exit 1
fi

DRY_RUN=false
ASSUME_YES=false
NO_DB_BACKUP=false
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)      DRY_RUN=true; shift ;;
        --yes|-y)       ASSUME_YES=true; shift ;;
        --no-db-backup) NO_DB_BACKUP=true; shift ;;
        -h|--help)      sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

RUN_USER="$(id -un)"
USER_HOME="$HOME"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$USER_HOME/Desktop")"
TS="$(date +%Y-%m-%d_%H%M%S)"

run() {  # run <cmd...> — echo, then execute unless --dry-run
    echo "  \$ $*"
    if [ "$DRY_RUN" = false ]; then "$@" 2>/dev/null || true; fi
}
confirm() { [ "$ASSUME_YES" = true ] && return 0; local a; read -rp "$1 " a; [ "${a,,}" = "y" ] || [ -z "$a" ]; }

echo "╔══════════════════════════════════════════════════╗"
echo "║   CodeGloFix — remove previous install            ║"
echo "╚══════════════════════════════════════════════════╝"
[ "$DRY_RUN" = true ] && echo "  (DRY RUN — nothing will be changed)"

# ── 1. Stop + disable services (no orphan enabled units) ──────────────────────
echo
echo "── 1. Stopping + disabling services ──"
INSTANCES=()
while IFS= read -r link; do
    [ -n "$link" ] && INSTANCES+=("$(basename "$link")")
done < <(find /etc/systemd/system -type l \
            \( -name 'codeglofix-access@*' -o -name 'gym-access@*' \
               -o -name 'codeglofix-backup@*' -o -name 'digitgym-backup@*' \) 2>/dev/null)
INSTANCES+=("codeglofix-access@${RUN_USER}.service" "gym-access@${RUN_USER}.service"
            "codeglofix-backup@${RUN_USER}.timer"   "digitgym-backup@${RUN_USER}.timer"
            "codeglofix-backup@${RUN_USER}.service"  "digitgym-backup@${RUN_USER}.service"
            "codeglofix-health.timer"               "codeglofix-health.service")
mapfile -t INSTANCES < <(printf '%s\n' "${INSTANCES[@]}" | sort -u)
for inst in "${INSTANCES[@]}"; do
    [ -n "$inst" ] || continue
    state="$(systemctl is-enabled "$inst" 2>/dev/null || true)"
    active="$(systemctl is-active  "$inst" 2>/dev/null || true)"
    if [ -n "$state" ] || [ "$active" = "active" ]; then
        run sudo systemctl disable --now "$inst"
    fi
done

# ── 2. Remove unit files ──────────────────────────────────────────────────────
echo
echo "── 2. Removing systemd unit files ──"
for u in codeglofix-access@.service gym-access@.service \
         codeglofix-backup@.service codeglofix-backup@.timer \
         digitgym-backup@.service digitgym-backup@.timer \
         codeglofix-health.service codeglofix-health.timer; do
    [ -e "/etc/systemd/system/$u" ] && run sudo rm -f "/etc/systemd/system/$u"
done
# USB autosuspend udev rule (installed by the health/reliability step)
[ -e /etc/udev/rules.d/50-usb-no-autosuspend.rules ] \
    && run sudo rm -f /etc/udev/rules.d/50-usb-no-autosuspend.rules
run sudo systemctl daemon-reload
run sudo systemctl reset-failed

# ── 2b. Remove the PyQt live-display user service (systemd --user) ─────────────
# The PyQt live display (the outside door display) runs as a per-user service,
# not a system unit, so it is removed here in --user scope. No data is touched.
echo
echo "── 2b. Removing PyQt live-display user service (if any) ──"
PYQT_USER_UNIT="$USER_HOME/.config/systemd/user/codeglofix-live-display.service"
if [ "$(systemctl --user is-active codeglofix-live-display.service 2>/dev/null || true)" = "active" ] \
   || [ -e "$PYQT_USER_UNIT" ]; then
    run systemctl --user disable --now codeglofix-live-display.service
    [ -e "$PYQT_USER_UNIT" ] && run rm -f "$PYQT_USER_UNIT"
    run systemctl --user daemon-reload
    run systemctl --user reset-failed
else
    echo "  no PyQt live-display user service found"
fi
# Clean up any leftover kiosk autostart from an OLD (pre-PyQt) install.
for stale in "$USER_HOME/.config/autostart/CodeGloFix-Kiosk.desktop" \
             "$USER_HOME/.config/autostart/CodeGloFix-Kiosk.desktop.disabled"; do
    [ -e "$stale" ] && run rm -f "$stale"
done

# ── 3. Remove autostart entries ───────────────────────────────────────────────
echo
echo "── 3. Removing autostart entries ──"
if [ -d "$USER_HOME/.config/autostart" ]; then
    while IFS= read -r df; do
        grep -qiE 'CodeGloFix|/opt/(codeglofix|digitgym)' "$df" 2>/dev/null \
            && run rm -f "$df"
    done < <(find "$USER_HOME/.config/autostart" -maxdepth 1 -name '*.desktop' 2>/dev/null)
fi

# ── 4. Remove desktop launchers ───────────────────────────────────────────────
echo
echo "── 4. Removing desktop launchers ──"
for dir in "$DESKTOP_DIR" "$USER_HOME/.local/share/applications"; do
    [ -d "$dir" ] || continue
    while IFS= read -r df; do
        grep -qiE 'CodeGloFix|/opt/(codeglofix|digitgym)' "$df" 2>/dev/null \
            && run rm -f "$df"
    done < <(find "$dir" -maxdepth 1 -name '*.desktop' 2>/dev/null)
done

# ── 5. Remove nginx site + logrotate ──────────────────────────────────────────
echo
echo "── 5. Removing nginx site + logrotate configs ──"
for f in /etc/nginx/sites-enabled/face-attendance /etc/nginx/sites-available/face-attendance; do
    [ -e "$f" ] && run sudo rm -f "$f"
done
for lr in /etc/logrotate.d/codeglofix /etc/logrotate.d/digitgym /etc/logrotate.d/gym-access; do
    [ -e "$lr" ] && run sudo rm -f "$lr"
done
if [ "$DRY_RUN" = false ] && command -v nginx >/dev/null 2>&1; then
    sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx 2>/dev/null || true
fi

# ── 6. DATABASE — detect, offer backup, NEVER auto-delete ─────────────────────
echo
echo "── 6. Database (NOT deleted — backed up on request) ──"
DBS=()
for db in /var/lib/codeglofix/gym.db /var/lib/digitgym/gym.db; do
    [ -e "$db" ] && DBS+=("$db")
done
if [ "${#DBS[@]}" -eq 0 ]; then
    echo "  no database found in /var/lib/{codeglofix,digitgym}"
else
    for db in "${DBS[@]}"; do echo "  found: $db"; done
    if [ "$NO_DB_BACKUP" = true ]; then
        echo "  --no-db-backup given — skipping backup (database left in place)."
    elif confirm "Database found. Backup before removal? [Y/n]"; then
        BACKUP_DIR="/var/backups/codeglofix"
        ARCHIVE="$BACKUP_DIR/preremove_db_${TS}.tar.gz"
        run sudo mkdir -p "$BACKUP_DIR"
        # Back up the whole data dir(s) (gym.db + enrolments + gate_config).
        SRCS=()
        [ -e /var/lib/codeglofix ] && SRCS+=(/var/lib/codeglofix)
        [ -e /var/lib/digitgym ]   && SRCS+=(/var/lib/digitgym)
        run sudo tar -czf "$ARCHIVE" "${SRCS[@]}"
        if [ "$DRY_RUN" = false ]; then
            sudo chown "$RUN_USER:$RUN_USER" "$ARCHIVE" 2>/dev/null || true
            echo "  ✓ backup written: $ARCHIVE ($(du -h "$ARCHIVE" 2>/dev/null | cut -f1))"
        fi
    else
        echo "  backup skipped by user (database left in place)."
    fi
    echo "  NOTE: the database / data dir is PRESERVED on purpose. The next install"
    echo "        reuses it. To wipe member data, delete /var/lib/codeglofix manually."
fi

# ── 7. Remove /opt installs + env files ───────────────────────────────────────
echo
echo "── 7. Removing /opt installs + env files ──"
for p in /opt/codeglofix /opt/digitgym; do
    [ -e "$p" ] && run sudo rm -rf "$p"
done
for e in /etc/codeglofix-access.env /etc/gym-access.env; do
    [ -e "$e" ] && run sudo rm -f "$e"
done

# ── Done ──────────────────────────────────────────────────────────────────────
echo
if [ "$DRY_RUN" = true ]; then
    echo "=== DRY RUN complete — re-run without --dry-run to apply. ==="
else
    echo "=== Previous install removed. Data dirs preserved (DB kept/backed up). ==="
    echo "Verify clean:  ./tools/preinstall_audit.sh"
    echo "Then install:  ./deploy/install_customer_pc.sh --hardware-check"
fi
