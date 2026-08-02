#!/usr/bin/env bash
set -euo pipefail

DUMP_FILE="/tmp/japanese-$(date +%F).sql.gz"
COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

docker compose -f "$COMPOSE_DIR/docker-compose.yml" exec -T db \
    pg_dump -U app japanese | gzip > "$DUMP_FILE"

# Push to R2 — requires rclone configured with an "r2" remote, or replace with:
#   aws s3 cp "$DUMP_FILE" s3://your-bucket/db-backups/ --endpoint-url "$R2_ENDPOINT_URL"
rclone copy "$DUMP_FILE" r2:your-bucket/db-backups/

# Keep only the last 14 local dumps
find /tmp -name "japanese-*.sql.gz" -mtime +14 -delete

echo "Backup complete: $DUMP_FILE"
