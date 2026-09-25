#!/bin/bash
# backup_db.sh — production backup for CodeGloFix Access Control
# Path-independent: place this file in the project folder next to gym.db.
# Backs up gym.db, face embeddings, gate_config.json, and environment secrets.
# Stores backups outside the project/web root by default: /var/backups/gym-access

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
LOCAL_ENV_FILE="$PROJECT_DIR/.env"
# Current production env file; the legacy /etc/gym-access.env is read only if the
# new one is absent, so an in-place upgrade keeps working until it is migrated.
SYSTEM_ENV_FILE="/etc/codeglofix-access.env"
LEGACY_ENV_FILE="/etc/gym-access.env"

# Source the env file ONLY if it is readable by us, so this script sees the same
# data-path overrides the app uses (CODEGLOFIX_DB_PATH etc). Under systemd the
# unit already injects these via EnvironmentFile, and the file is root-only 600 —
# so this is skipped there (and on the dev PC) without error.
if [ -r "$SYSTEM_ENV_FILE" ]; then
    set -a; . "$SYSTEM_ENV_FILE"; set +a
elif [ -r "$LEGACY_ENV_FILE" ]; then
    set -a; . "$LEGACY_ENV_FILE"; set +a
fi

# Production data locations come from env (set in /etc/codeglofix-access.env); dev
# PC (env unset) keeps the project-relative paths — no behaviour change. Legacy
# DIGITGYM_* names are honoured as an internal fallback during migration.
DB_FILE="${CODEGLOFIX_DB_PATH:-${DIGITGYM_DB_PATH:-$PROJECT_DIR/gym.db}}"
ENROLMENTS_DIR="${CODEGLOFIX_ENROLMENTS_DIR:-${DIGITGYM_ENROLMENTS_DIR:-$PROJECT_DIR/enrolments/shared}}"
EMBEDDINGS_FILE="$ENROLMENTS_DIR/faces/arcface_embeddings.npz"
GATE_CONFIG_FILE="${CODEGLOFIX_GATE_CONFIG:-${DIGITGYM_GATE_CONFIG:-$PROJECT_DIR/gate_config.json}}"

BACKUP_DIR="${BACKUP_DIR:-/var/backups/codeglofix}"
USB_BACKUP_DIR="${USB_BACKUP_DIR:-/media/$USER/BACKUP/codeglofix}"
KEEP_DAYS="${KEEP_DAYS:-30}"
# Archive access logs at 11 months — one month BEFORE the app's 12-month
# in-process cleanup deletes them (gym_db.cleanup_old_access_logs). This
# guarantees the nightly backup archives every old log to the yearly archive
# DB before it can be hard-deleted, closing the archive-vs-delete race.
ARCHIVE_MONTHS="${ARCHIVE_MONTHS:-11}"

DATE="$(date +%Y-%m-%d)"
TIMESTAMP="$(date +%Y-%m-%dT%H:%M:%S)"
RUN_DIR="$BACKUP_DIR/$DATE"
DB_BACKUP="$RUN_DIR/gym_${DATE}.db"
META_BACKUP="$RUN_DIR/gym_meta_${DATE}.tar.gz"
ENV_BACKUP="$RUN_DIR/gym_env_${DATE}.tar.gz"
ARCHIVE_DB="$BACKUP_DIR/access_logs_archive_$(date +%Y).db"

umask 077
mkdir -p "$RUN_DIR"
chmod 700 "$BACKUP_DIR" "$RUN_DIR" 2>/dev/null || true

echo "=== CodeGloFix backup started at $TIMESTAMP ==="
echo "Project: $PROJECT_DIR"
echo "Backup : $RUN_DIR"

if [ ! -f "$DB_FILE" ]; then
    echo "ERROR: Database not found at $DB_FILE"
    exit 1
fi

# Safe SQLite online backup while app is running.
sqlite3 "$DB_FILE" ".backup '$DB_BACKUP'"
echo "DB backup: $DB_BACKUP ($(du -sh "$DB_BACKUP" | cut -f1))"

# Verify copied DB integrity.
INTEGRITY="$(sqlite3 "$DB_BACKUP" "PRAGMA integrity_check;")"
if [ "$INTEGRITY" != "ok" ]; then
    echo "ERROR: Backup integrity check failed: $INTEGRITY"
    exit 2
fi
echo "DB integrity: ok"

# Archive old access logs out of the active DB after backup.
# This keeps the live DB fast for 10-year operation.
if [ "${DISABLE_LOG_ARCHIVE:-false}" != "true" ]; then
    CUTOFF_EXPR="-$ARCHIVE_MONTHS months"
    echo "Archiving access_logs older than $ARCHIVE_MONTHS months to $ARCHIVE_DB"
    sqlite3 "$DB_FILE" <<SQL
ATTACH DATABASE '$ARCHIVE_DB' AS arch;
CREATE TABLE IF NOT EXISTS arch.access_logs (
    id INTEGER,
    member_id INTEGER,
    member_name TEXT NOT NULL,
    ts TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    door_opened INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO arch.access_logs (id, member_id, member_name, ts, decision, reason, door_opened)
SELECT id, member_id, member_name, ts, decision, reason, door_opened
FROM main.access_logs
WHERE ts < date('now', '$CUTOFF_EXPR')
  AND id NOT IN (SELECT id FROM arch.access_logs);
DELETE FROM main.access_logs
WHERE ts < date('now', '$CUTOFF_EXPR');
DETACH DATABASE arch;
PRAGMA wal_checkpoint(TRUNCATE);
SQL
fi

# Backup non-secret metadata together.
TMP_META_DIR="$(mktemp -d)"
mkdir -p "$TMP_META_DIR/enrolments/shared/faces"
if [ -f "$EMBEDDINGS_FILE" ]; then
    cp "$EMBEDDINGS_FILE" "$TMP_META_DIR/enrolments/shared/faces/arcface_embeddings.npz"
fi
if [ -f "$GATE_CONFIG_FILE" ]; then
    cp "$GATE_CONFIG_FILE" "$TMP_META_DIR/gate_config.json"
fi
tar -czf "$META_BACKUP" -C "$TMP_META_DIR" .
rm -rf "$TMP_META_DIR"
echo "Meta backup: $META_BACKUP"

# Backup env secrets separately with strict permissions, outside project root.
TMP_ENV_DIR="$(mktemp -d)"
if [ -f "$SYSTEM_ENV_FILE" ]; then
    cp "$SYSTEM_ENV_FILE" "$TMP_ENV_DIR/codeglofix-access.env"
elif [ -f "$LEGACY_ENV_FILE" ]; then
    cp "$LEGACY_ENV_FILE" "$TMP_ENV_DIR/codeglofix-access.env"
elif [ -f "$LOCAL_ENV_FILE" ]; then
    cp "$LOCAL_ENV_FILE" "$TMP_ENV_DIR/env"
fi
if [ "$(find "$TMP_ENV_DIR" -type f | wc -l)" -gt 0 ]; then
    tar -czf "$ENV_BACKUP" -C "$TMP_ENV_DIR" .
    chmod 600 "$ENV_BACKUP" 2>/dev/null || true
    echo "Env backup: $ENV_BACKUP"
else
    echo "Env backup: skipped (no env file found)"
fi
rm -rf "$TMP_ENV_DIR"

# Optional USB/external copy.
if [ -d "$(dirname "$USB_BACKUP_DIR")" ] || mkdir -p "$USB_BACKUP_DIR" 2>/dev/null; then
    mkdir -p "$USB_BACKUP_DIR/$DATE"
    cp "$DB_BACKUP" "$META_BACKUP" "$USB_BACKUP_DIR/$DATE/"
    [ -f "$ENV_BACKUP" ] && cp "$ENV_BACKUP" "$USB_BACKUP_DIR/$DATE/" || true
    echo "USB/external copy: $USB_BACKUP_DIR/$DATE"
else
    echo "USB/external backup skipped"
fi

# Prune old local dated folders.
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -print -exec rm -rf {} \;

echo "=== Backup complete at $(date +%Y-%m-%dT%H:%M:%S) ==="
