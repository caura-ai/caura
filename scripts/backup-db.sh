#!/usr/bin/env bash
# Database backup: pg_dump (custom format) → optional GCS upload
# Usage: ./backup-db.sh [gcs-bucket-name]
# Restore: pg_restore -h HOST -U USER -d DB --no-owner --no-privileges FILE
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="${PROJECT_DIR}/backups"
mkdir -p "$BACKUP_DIR"

# Load DB credentials from .env if present
if [ -f "${PROJECT_DIR}/.env" ]; then
    set -a
    source "${PROJECT_DIR}/.env"
    set +a
fi

DB_HOST="${ALLOYDB_HOST:-127.0.0.1}"
DB_PORT="${ALLOYDB_PORT:-5432}"
DB_USER="${ALLOYDB_USER:-memclaw}"
DB_NAME="${ALLOYDB_DATABASE:-memclaw}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
# ``.dump``, not ``.sql.gz``. ``-Fc`` is pg_dump's custom format: already
# compressed, and NOT SQL. The old name promised something ``gunzip | psql``
# could restore; what it held was a gzipped binary archive that only
# ``pg_restore`` reads, so the name misdirected the one operation a backup
# exists for. Piping it through gzip compressed it a second time for no gain.
BACKUP_FILE="${BACKUP_DIR}/caura_${TIMESTAMP}.dump"
# Dump to a partial name and rename only on success. ``set -e`` aborts on a
# failed dump, but the output file is created the moment the redirect opens —
# so a dump that died halfway used to leave a truncated file sitting in the
# backup directory under a name indistinguishable from a good one, which the
# retention sweep below would then keep for a week.
PARTIAL="${BACKUP_FILE}.partial"
trap 'rm -f "$PARTIAL"' EXIT

echo "=== Backing up ${DB_NAME}@${DB_HOST}:${DB_PORT} ==="
PGPASSWORD="${ALLOYDB_PASSWORD:-changeme}" pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --no-owner \
    --no-privileges \
    -Fc \
    -f "$PARTIAL"

mv "$PARTIAL" "$BACKUP_FILE"
SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "Backup saved: ${BACKUP_FILE} (${SIZE})"
echo "Restore with: pg_restore -h ${DB_HOST} -p ${DB_PORT} -U ${DB_USER} -d ${DB_NAME} --no-owner --no-privileges ${BACKUP_FILE}"

# Upload to GCS if bucket specified
GCS_BUCKET="${1:-}"
if [ -n "$GCS_BUCKET" ]; then
    echo "=== Uploading to gs://${GCS_BUCKET}/ ==="
    gsutil cp "$BACKUP_FILE" "gs://${GCS_BUCKET}/backups/$(basename "$BACKUP_FILE")"
    echo "Uploaded to GCS"
fi

# Clean up local backups older than 7 days. Both patterns: a backup directory
# written by an older revision of this script still holds ``*.sql.gz`` files,
# and dropping that pattern would leave them to accumulate forever. The old
# literal below is the name those files already carry on disk — no rename
# reaches them, and rewording it would simply stop matching.
find "$BACKUP_DIR" \( -name "caura_*.dump" -o -name "memclaw_*.sql.gz" \) -mtime +7 -delete 2>/dev/null || true  # legacy-name-floor: on-disk filenames written by earlier runs
echo "=== Backup complete ==="
