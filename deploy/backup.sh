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
tar -C "$TMP" -cf - . | zstd -q -T0 -o "$TMP.tar.zst"
aws s3 cp --only-show-errors "$TMP.tar.zst" "s3://$BUCKET/backups/$DAY/sqlite-$(date -u +%H%M).tar.zst"
rm -f "$TMP.tar.zst"
echo "backup $DAY done"
