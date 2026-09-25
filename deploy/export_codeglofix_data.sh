#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# export_codeglofix_data.sh — Bundle CodeGloFix data for migration to a customer PC.
#
# Run on the SOURCE PC (dev or an existing production box). Produces:
#   codeglofix_data_<host>_<date>.tar.gz
# containing:
#   gym.db
#   enrolments/shared/faces/arcface_embeddings.npz   (+ any face images)
#   gate_config.json
#   codeglofix-access.env.SAMPLE   (env WITHOUT secrets — for reference only)
#
# It reads from env-configured locations if /etc/codeglofix-access.env exists
# (production), otherwise from the project folder (dev). No secrets are exported.
# Safe: read-only on the source; does not touch the running app.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"

# Pick up production data-path overrides only if the env file is readable by us
# (it is root-only 600 under systemd; skip silently otherwise).
[ -r /etc/codeglofix-access.env ] && { set -a; . /etc/codeglofix-access.env; set +a; }

DB_FILE="${CODEGLOFIX_DB_PATH:-$PROJECT_DIR/gym.db}"
ENROLMENTS_DIR="${CODEGLOFIX_ENROLMENTS_DIR:-$PROJECT_DIR/enrolments/shared}"
GATE_CONFIG_FILE="${CODEGLOFIX_GATE_CONFIG:-$PROJECT_DIR/gate_config.json}"

HOST="$(hostname -s 2>/dev/null || echo host)"
DATE="$(date +%Y-%m-%d_%H%M)"
OUT="${OUT:-$PWD/codeglofix_data_${HOST}_${DATE}.tar.gz}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "=== CodeGloFix data export ==="
echo "  DB         : $DB_FILE"
echo "  enrolments : $ENROLMENTS_DIR"
echo "  gate config: $GATE_CONFIG_FILE"

# DB — use sqlite online backup so it is consistent even while the app runs.
if [ -f "$DB_FILE" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$DB_FILE" ".backup '$STAGE/gym.db'"
    else
        cp "$DB_FILE" "$STAGE/gym.db"
    fi
    echo "  + gym.db"
else
    echo "  ! no gym.db found — exporting without it"
fi

# Enrolments (embeddings + face images).
if [ -d "$ENROLMENTS_DIR" ]; then
    mkdir -p "$STAGE/enrolments/shared"
    # Carry only the live embeddings + face images, not the historical
    # *_backup_* snapshots the app writes before bulk deletes.
    rsync -a --exclude '*_backup_*' "$ENROLMENTS_DIR/" "$STAGE/enrolments/shared/" 2>/dev/null \
        || cp -r "$ENROLMENTS_DIR/." "$STAGE/enrolments/shared/"
    echo "  + enrolments/shared (live embeddings)"
else
    echo "  ! no enrolments dir found"
fi

# Gate config.
if [ -f "$GATE_CONFIG_FILE" ]; then
    cp "$GATE_CONFIG_FILE" "$STAGE/gate_config.json"
    echo "  + gate_config.json"
fi

# Env SAMPLE (NO secrets) for reference.
if [ -f "$DEPLOY_DIR/codeglofix-access.env.template" ]; then
    cp "$DEPLOY_DIR/codeglofix-access.env.template" "$STAGE/codeglofix-access.env.SAMPLE"
fi

tar -czf "$OUT" -C "$STAGE" .
chmod 600 "$OUT"
echo
echo "=== Export complete ==="
echo "  $OUT  ($(du -sh "$OUT" | cut -f1))"
echo "Copy it to the customer PC, then run:"
echo "  ./deploy/import_codeglofix_data.sh $OUT"
