#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# import_codeglofix_data.sh — Import a data bundle into /var/lib/codeglofix safely.
#
# Run on the CUSTOMER PC after install_customer_pc.sh. It:
#   1. stops the gym-access service,
#   2. backs up the current /var/lib/codeglofix to /var/backups/codeglofix,
#   3. restores gym.db, enrolments/shared, gate_config.json from the bundle,
#   4. fixes ownership/permissions,
#   5. restarts the service.
#
# Usage:  ./import_codeglofix_data.sh /path/to/codeglofix_data_*.tar.gz
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: run as the normal desktop user, not root (uses sudo internally)."
    exit 1
fi

BUNDLE="${1:-}"
if [ -z "$BUNDLE" ] || [ ! -f "$BUNDLE" ]; then
    echo "Usage: $0 /path/to/codeglofix_data_*.tar.gz"
    exit 1
fi

DATA_DIR=/var/lib/codeglofix
BACKUP_DIR=/var/backups/codeglofix
RUN_USER="$(id -un)"
SERVICE="codeglofix-access@${RUN_USER}.service"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "=== CodeGloFix data import ==="
echo "  bundle : $BUNDLE"
echo "  target : $DATA_DIR"
tar -xzf "$BUNDLE" -C "$STAGE"

if [ ! -f "$STAGE/gym.db" ] && [ ! -d "$STAGE/enrolments" ]; then
    echo "ERROR: bundle has no gym.db or enrolments — wrong file?"
    exit 1
fi

read -rp "This overwrites current data in $DATA_DIR (a backup is taken first). Continue? [y/N] " a
[ "${a,,}" = "y" ] || { echo "Aborted."; exit 0; }

# 1. Stop service (ignore if not yet running).
sudo systemctl stop "$SERVICE" 2>/dev/null || true

# 2. Backup current data.
if [ -d "$DATA_DIR" ] && [ -n "$(sudo ls -A "$DATA_DIR" 2>/dev/null)" ]; then
    SAFE="$BACKUP_DIR/predeploy_$(date +%Y-%m-%d_%H%M)"
    sudo mkdir -p "$SAFE"
    sudo cp -a "$DATA_DIR/." "$SAFE/" 2>/dev/null || true
    echo "  current data backed up to $SAFE"
fi

# 3. Restore from bundle.
sudo mkdir -p "$DATA_DIR/enrolments/shared/faces"
[ -f "$STAGE/gym.db" ] && sudo cp "$STAGE/gym.db" "$DATA_DIR/gym.db"
if [ -d "$STAGE/enrolments/shared" ]; then
    sudo rsync -a --delete "$STAGE/enrolments/shared/" "$DATA_DIR/enrolments/shared/"
fi
[ -f "$STAGE/gate_config.json" ] && sudo cp "$STAGE/gate_config.json" "$DATA_DIR/gate_config.json"

# 4. Ownership / perms (not world-readable).
sudo chown -R "$RUN_USER:$RUN_USER" "$DATA_DIR"
sudo chmod 750 "$DATA_DIR"
sudo find "$DATA_DIR" -type f -name '*.db' -exec chmod 640 {} +
sudo find "$DATA_DIR" -type f -name '*.npz' -exec chmod 640 {} +

# 5. Restart service.
sudo systemctl start "$SERVICE"
echo
echo "=== Import complete — service restarted ==="
echo "Verify: curl -k -o /dev/null -w '%{http_code}\n' https://localhost/  (expect 200)"
