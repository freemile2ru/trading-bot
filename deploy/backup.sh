#!/usr/bin/env bash
# Nightly on the instance (systemd timer installed by Terraform's user-data): a consistent copy of every SQLite file
# (sqlite3's online .backup, safe while the app writes) to S3 under backups/<date>/. The bucket's lifecycle rule
# expires objects after 30 days. Secrets are not backed up — they live in SSM Parameter Store.
set -euo pipefail
ROOT=/srv/ai-trader
BUCKET="${BACKUP_BUCKET:-$(cat /etc/ai-trader/backup-bucket)}"
DAY=$(date -u +%F)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

for db in "$ROOT"/data/*.db; do
  [ -e "$db" ] || continue
  sqlite3 "$db" ".backup '$TMP/$(basename "$db")'"
done
if DBURL=$(aws ssm get-parameter --region "${AWS_REGION:-ap-northeast-1}" --with-decryption --name /ai-trader/database-url --query Parameter.Value --output text 2>/dev/null) && [ -n "$DBURL" ]; then
  docker run --rm -e PGURL="$DBURL" postgres:16-alpine sh -c 'pg_dump --no-owner --no-privileges "$PGURL"' > "$TMP/postgres.sql" || echo "pg_dump failed" >&2
fi
tar -C "$TMP" -cf - . | zstd -q -T0 -o "$TMP.tar.zst"
aws s3 cp --only-show-errors "$TMP.tar.zst" "s3://$BUCKET/backups/$DAY/sqlite-$(date -u +%H%M).tar.zst"
rm -f "$TMP.tar.zst"
echo "backup $DAY done"
