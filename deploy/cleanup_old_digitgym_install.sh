#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# cleanup_old_digitgym_install.sh — Safely remove a previous DigitGym / gym-access
# install so it cannot shadow or fight the new CodeGloFix install.
#
# It removes ONLY the OLD names. It NEVER touches the new codeglofix-* units,
# /opt/codeglofix, /var/lib/codeglofix, etc.
#
# What it cleans (old → gone):
#   • systemd units : gym-access@*, digitgym-backup@*.service, digitgym-backup@*.timer
#                     (disabled --now so NO orphan enabled units remain)
#   • logrotate     : /etc/logrotate.d/digitgym, /etc/logrotate.d/gym-access
#   • desktop files : any *.desktop (Desktop / app menu / autostart) whose Exec=
#                     points at /opt/digitgym
#   • old paths     : /opt/digitgym  /var/log/digitgym  /var/backups/digitgym
#                     /var/lib/digitgym (DATA — only with confirmation)
#                     /etc/gym-access.env (old secrets file)
#
# Destructive steps run ONLY after you confirm (or pass --yes). Use --keep-data to
# preserve /var/lib/digitgym and /var/backups/digitgym, and --dry-run to preview.
#
# RUN AS THE NORMAL DESKTOP USER (not root). It uses sudo where required.
#   ./cleanup_old_digitgym_install.sh [--yes] [--keep-data] [--dry-run]
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: run as the normal desktop user, not root (it calls sudo itself)."
    exit 1
fi

ASSUME_YES=false
KEEP_DATA=false
DRY_RUN=false
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y)    ASSUME_YES=true; shift ;;
        --keep-data) KEEP_DATA=true; shift ;;
        --dry-run)   DRY_RUN=true; shift ;;
        -h|--help)
            sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

RUN_USER="$(id -un)"
USER_HOME="$HOME"

run() {  # run <description> -- <command...>
    local desc="$1"; shift
    [ "$1" = "--" ] && shift
    echo "  \$ $*"
    if [ "$DRY_RUN" = true ]; then
        echo "    (dry-run: skipped)"
    else
        "$@" 2>/dev/null || true
    fi
}

confirm() {  # confirm <prompt>  → returns 0 if yes
    [ "$ASSUME_YES" = true ] && return 0
    local a; read -rp "$1 [y/N] " a; [ "${a,,}" = "y" ]
}

echo "╔══════════════════════════════════════════════════╗"
echo "║   CodeGloFix — old DigitGym install cleanup       ║"
echo "╚══════════════════════════════════════════════════╝"
[ "$DRY_RUN" = true ] && echo "  (DRY RUN — nothing will actually be changed)"
echo

# ── 1. Disable + stop OLD systemd units (no orphan enabled units left) ─────────
echo "── 1. Disabling old systemd units (gym-access@*, digitgym-backup@*) ──"
OLD_INSTANCES=()
# Enabled instances appear as symlinks inside *.wants directories.
while IFS= read -r link; do
    OLD_INSTANCES+=("$(basename "$link")")
done < <(find /etc/systemd/system -type l \
            \( -name 'gym-access@*' -o -name 'digitgym-backup@*' \) 2>/dev/null)
# Also try the obvious current-user instances even if not symlinked.
OLD_INSTANCES+=("gym-access@${RUN_USER}.service"
               "digitgym-backup@${RUN_USER}.timer"
               "digitgym-backup@${RUN_USER}.service")
# De-duplicate.
mapfile -t OLD_INSTANCES < <(printf '%s\n' "${OLD_INSTANCES[@]}" | sort -u)

for inst in "${OLD_INSTANCES[@]}"; do
    [ -n "$inst" ] || continue
    state="$(systemctl is-enabled "$inst" 2>/dev/null || true)"
    active="$(systemctl is-active  "$inst" 2>/dev/null || true)"
    if [ "$state" != "" ] || [ "$active" = "active" ]; then
        echo "  • $inst (enabled=$state active=$active)"
        run "disable+stop $inst" -- sudo systemctl disable --now "$inst"
    fi
done

# ── 2. Remove OLD template unit files ──────────────────────────────────────────
echo
echo "── 2. Removing old unit files from /etc/systemd/system ──"
for u in gym-access@.service digitgym-backup@.service digitgym-backup@.timer; do
    if [ -e "/etc/systemd/system/$u" ]; then
        run "remove $u" -- sudo rm -f "/etc/systemd/system/$u"
    fi
done
run "daemon-reload" -- sudo systemctl daemon-reload
run "reset-failed"  -- sudo systemctl reset-failed

# ── 3. Remove OLD logrotate configs ────────────────────────────────────────────
echo
echo "── 3. Removing old logrotate configs ──"
for lr in /etc/logrotate.d/digitgym /etc/logrotate.d/gym-access; do
    [ -e "$lr" ] && run "remove $lr" -- sudo rm -f "$lr"
done

# ── 4. Remove OLD desktop launchers / autostart pointing at /opt/digitgym ──────
echo
echo "── 4. Removing old desktop launchers that point at /opt/digitgym ──"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$USER_HOME/Desktop")"
for dir in "$DESKTOP_DIR" "$USER_HOME/.local/share/applications" "$USER_HOME/.config/autostart"; do
    [ -d "$dir" ] || continue
    while IFS= read -r df; do
        if grep -q '^Exec=.*/opt/digitgym/' "$df" 2>/dev/null; then
            run "remove old launcher $df" -- rm -f "$df"
        fi
    done < <(find "$dir" -maxdepth 1 -name '*.desktop' 2>/dev/null)
done

# ── 5. Remove OLD filesystem paths (with confirmation) ─────────────────────────
echo
echo "── 5. Removing old install paths ──"
# Non-data paths: safe to remove.
for p in /opt/digitgym /var/log/digitgym; do
    if [ -e "$p" ]; then
        if confirm "Remove old path $p ?"; then
            run "remove $p" -- sudo rm -rf "$p"
        else
            echo "    kept $p"
        fi
    fi
done

# Old secrets file.
if sudo test -e /etc/gym-access.env; then
    if confirm "Remove old secrets file /etc/gym-access.env ? (the new one is /etc/codeglofix-access.env)"; then
        run "remove /etc/gym-access.env" -- sudo rm -f /etc/gym-access.env
    else
        echo "    kept /etc/gym-access.env"
    fi
fi

# Data + backups: extra-careful (member DB + face embeddings live here).
if [ "$KEEP_DATA" = true ]; then
    echo "  • --keep-data: leaving /var/lib/digitgym and /var/backups/digitgym in place."
    echo "    Migrate them first with deploy/export_codeglofix_data.sh if needed."
else
    for p in /var/lib/digitgym /var/backups/digitgym; do
        if [ -e "$p" ]; then
            echo "  ⚠ $p contains the member database / face embeddings / backups."
            echo "    Export them first if you have not: deploy/export_codeglofix_data.sh"
            if confirm "PERMANENTLY delete $p ?"; then
                run "remove $p" -- sudo rm -rf "$p"
            else
                echo "    kept $p"
            fi
        fi
    done
fi

echo
echo "── Verify nothing old remains ──"
echo "  \$ systemctl list-unit-files | grep -E 'gym-access|digitgym'   (expect: empty)"
if [ "$DRY_RUN" != true ]; then
    LEFT="$(systemctl list-unit-files 2>/dev/null | grep -E 'gym-access|digitgym' || true)"
    if [ -z "$LEFT" ]; then echo "  ✓ no old gym-access/digitgym units remain"
    else echo "  ⚠ still present:"; echo "$LEFT" | sed 's/^/      /'; fi
fi
echo
echo "=== Old DigitGym cleanup complete ==="
echo "Now (re)install CodeGloFix with:"
echo "  ./deploy/install_customer_pc.sh"
