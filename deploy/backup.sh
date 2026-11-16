#!/usr/bin/env bash
# Nightly backup (Postgres dump + result pictures in var/uploads), 14-day retention. cron: 15 2 * * * /opt/stelnexus/deploy/backup.sh
# Also copy the output off this machine, and back up .env (PII_KEY!) separately: without
# PII_KEY the encrypted player data in a backup cannot be read.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
DEST="${BACKUP_DIR:-/var/backups/stelnexus}"; mkdir -p "$DEST"; umask 077
pg_dump "${DATABASE_URL/+psycopg/}" | gzip > "$DEST/stelnexus-$(date +%F-%H%M).sql.gz"
# Result pictures are evidence and are not in the database: archive them too.
UPLOADS="${UPLOAD_DIR:-var/uploads}"
if [ -d "$UPLOADS" ]; then
  tar -czf "$DEST/stelnexus-uploads-$(date +%F-%H%M).tar.gz" -C "$(dirname "$UPLOADS")" "$(basename "$UPLOADS")"
fi
find "$DEST" \( -name 'stelnexus-*.sql.gz' -o -name 'stelnexus-uploads-*.tar.gz' \) -mtime +14 -delete
echo "backup ok"
