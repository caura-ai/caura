#!/usr/bin/env bash
# Database backup: pg_dump → gzip → optional GCS upload
# Usage: ./backup-db.sh [gcs-bucket-name]
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
BACKUP_FILE="${BACKUP_DIR}/memclaw_${TIMESTAMP}.sql.gz"

echo "=== Backing up ${DB_NAME}@${DB_HOST}:${DB_PORT} ==="
PGPASSWORD="${ALLOYDB_PASSWORD:-changeme}" pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --no-owner \
    --no-privileges \
    -Fc \
    | gzip > "$BACKUP_FILE"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "Backup saved: ${BACKUP_FILE} (${SIZE})"

# Upload to GCS if bucket specified
GCS_BUCKET="${1:-}"
if [ -n "$GCS_BUCKET" ]; then
    echo "=== Uploading to gs://${GCS_BUCKET}/ ==="
    gsutil cp "$BACKUP_FILE" "gs://${GCS_BUCKET}/backups/$(basename "$BACKUP_FILE")"
    echo "Uploaded to GCS"
fi

# Clean up local backups older than 7 days
find "$BACKUP_DIR" -name "memclaw_*.sql.gz" -mtime +7 -delete 2>/dev/null || true
echo "=== Backup complete ==="
