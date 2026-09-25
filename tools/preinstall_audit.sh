#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# preinstall_audit.sh — READ-ONLY audit of any previous CodeGloFix / DigitGym /
# gym-access install on this PC. Changes NOTHING. Run before installing.
#
#   ./tools/preinstall_audit.sh
#
# Reports: services, nginx configs, autostart, desktop launchers, databases,
# install paths, env files — then a single verdict:
#     PREVIOUS INSTALL DETECTED: YES / NO
#
# Exit code: 0 if nothing found, 1 if a previous install was detected (handy in
# scripts). Nothing is ever stopped, removed, or written.
# ──────────────────────────────────────────────────────────────────────────────
set -u

USER_HOME="$HOME"
RUN_USER="$(id -un)"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$USER_HOME/Desktop")"
FOUND=0
mark() { FOUND=1; }

hdr()  { echo; echo "── $* ──"; }
yes()  { echo "  ● $*"; }
no()   { echo "  ○ $*"; }

echo "╔══════════════════════════════════════════════════╗"
echo "║   CodeGloFix — pre-install audit (read-only)      ║"
echo "╚══════════════════════════════════════════════════╝"
echo "  host : $(hostname 2>/dev/null)    user : $RUN_USER"

# ── A) Existing services ──────────────────────────────────────────────────────
hdr "A) systemd services (codeglofix-access*, gym-access*, codeglofix-backup*, digitgym-backup*)"
SVC_PATTERNS=(codeglofix-access gym-access codeglofix-backup digitgym-backup)
svc_found=0
# Template unit files present on disk.
for u in /etc/systemd/system/codeglofix-access@.service \
         /etc/systemd/system/gym-access@.service \
         /etc/systemd/system/codeglofix-backup@.service \
         /etc/systemd/system/codeglofix-backup@.timer \
         /etc/systemd/system/digitgym-backup@.service \
         /etc/systemd/system/digitgym-backup@.timer; do
    if [ -e "$u" ]; then yes "unit file present : $u"; svc_found=1; fi
done
# Enabled instances (symlinks in *.wants).
while IFS= read -r link; do
    [ -n "$link" ] || continue
    yes "enabled instance : $(basename "$link")  ($link)"
    svc_found=1
done < <(find /etc/systemd/system -type l \
            \( -name 'codeglofix-access@*' -o -name 'gym-access@*' \
               -o -name 'codeglofix-backup@*' -o -name 'digitgym-backup@*' \) 2>/dev/null)
# Active instances for the current user.
for inst in "codeglofix-access@${RUN_USER}.service" "gym-access@${RUN_USER}.service" \
            "codeglofix-backup@${RUN_USER}.timer"   "digitgym-backup@${RUN_USER}.timer"; do
    st="$(systemctl is-active "$inst" 2>/dev/null || true)"
    if [ "$st" = "active" ]; then yes "active : $inst"; svc_found=1; fi
done
[ "$svc_found" = 1 ] && mark || no "no previous service units found"

# ── B) nginx configs ──────────────────────────────────────────────────────────
hdr "B) nginx site configs"
nginx_found=0
for f in /etc/nginx/sites-available/face-attendance /etc/nginx/sites-enabled/face-attendance; do
    if [ -e "$f" ]; then yes "$f"; nginx_found=1; fi
done
[ "$nginx_found" = 1 ] && mark || no "no CodeGloFix nginx site found"

# ── C) autostart entries ──────────────────────────────────────────────────────
hdr "C) autostart entries (~/.config/autostart)"
auto_found=0
if [ -d "$USER_HOME/.config/autostart" ]; then
    while IFS= read -r df; do
        if grep -qiE 'CodeGloFix|/opt/(codeglofix|digitgym)' "$df" 2>/dev/null; then
            yes "$df"; auto_found=1
        fi
    done < <(find "$USER_HOME/.config/autostart" -maxdepth 1 -name '*.desktop' 2>/dev/null)
fi
[ "$auto_found" = 1 ] && mark || no "no CodeGloFix autostart entries"

# ── D) desktop launchers ──────────────────────────────────────────────────────
hdr "D) desktop launchers (Desktop + app menu)"
launch_found=0
for dir in "$DESKTOP_DIR" "$USER_HOME/.local/share/applications"; do
    [ -d "$dir" ] || continue
    while IFS= read -r df; do
        if grep -qiE 'CodeGloFix|/opt/(codeglofix|digitgym)' "$df" 2>/dev/null; then
            yes "$df"; launch_found=1
        fi
    done < <(find "$dir" -maxdepth 1 -name '*.desktop' 2>/dev/null)
done
[ "$launch_found" = 1 ] && mark || no "no CodeGloFix desktop launchers"

# ── E) databases ──────────────────────────────────────────────────────────────
hdr "E) databases"
db_found=0
for db in /var/lib/codeglofix/gym.db /var/lib/digitgym/gym.db; do
    if [ -e "$db" ]; then
        sz="$(du -h "$db" 2>/dev/null | cut -f1)"
        yes "$db  (size: ${sz:-?})"; db_found=1
    fi
done
[ "$db_found" = 1 ] && mark || no "no existing databases in /var/lib/{codeglofix,digitgym}"

# ── F) install paths ──────────────────────────────────────────────────────────
hdr "F) install paths"
path_found=0
for p in /opt/codeglofix /opt/digitgym /var/lib/codeglofix /var/lib/digitgym; do
    if [ -e "$p" ]; then yes "$p"; path_found=1; fi
done
[ "$path_found" = 1 ] && mark || no "no previous install paths"

# ── G) env files ──────────────────────────────────────────────────────────────
hdr "G) environment files"
env_found=0
for e in /etc/codeglofix-access.env /etc/gym-access.env; do
    if [ -e "$e" ]; then yes "$e"; env_found=1; fi
done
[ "$env_found" = 1 ] && mark || no "no previous env files"

# ── Verdict ───────────────────────────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════════════"
if [ "$FOUND" = 1 ]; then
    echo "PREVIOUS INSTALL DETECTED: YES"
    echo "  → Clean it first:   ./deploy/remove_previous_install.sh"
    echo "    (preview only:    ./deploy/remove_previous_install.sh --dry-run )"
    echo "═══════════════════════════════════════════════════"
    exit 1
else
    echo "PREVIOUS INSTALL DETECTED: NO"
    echo "  → This PC looks clean. Proceed with ./deploy/install_customer_pc.sh"
    echo "═══════════════════════════════════════════════════"
    exit 0
fi
