#!/bin/bash
# First boot of the Tokyo instance (cloud-init): Docker, the AWS CLI, sqlite3; the data volume formatted and mounted
# at /srv/ai-trader/data; the nightly backup timer; and a check that Binance answers from this address — the one
# thing US regions cannot do. The application itself arrives with the first deploy (deploy/deploy.sh via SSM).
set -euxo pipefail
exec > >(tee -a /var/log/ai-trader-firstboot.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg sqlite3 zstd unzip jq

# Docker (official repository: the Ubuntu package lags on the compose plugin)
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker

# AWS CLI v2 (arm64)
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip

# The data volume: attached as /dev/sdf, which Nitro exposes as an NVMe device. Format once, mount by UUID.
mkdir -p /srv/ai-trader/data
for i in $(seq 1 30); do
  DEV=$(lsblk -dpno NAME,TYPE | awk '$2=="disk"{print $1}' | while read -r d; do [ "$(lsblk -no MOUNTPOINT "$d" | grep -c /)" -eq 0 ] && echo "$d"; done | head -n1)
  [ -n "$DEV" ] && break
  sleep 5
done
if [ -n "$DEV" ]; then
  blkid "$DEV" >/dev/null 2>&1 || mkfs.ext4 -L ai-trader-data "$DEV"
  UUID=$(blkid -s UUID -o value "$DEV")
  grep -q "$UUID" /etc/fstab || echo "UUID=$UUID /srv/ai-trader/data ext4 defaults,nofail 0 2" >> /etc/fstab
  mount -a
fi
chown -R 1000:1000 /srv/ai-trader/data   # the backend container runs as uid 1000

# Backups: nightly to S3 (deploy/backup.sh arrives with each release; the bucket name is pinned here)
mkdir -p /etc/ai-trader
echo "${backup_bucket}" > /etc/ai-trader/backup-bucket
cat > /etc/systemd/system/ai-trader-backup.service <<'EOF'
[Unit]
Description=AI trader: SQLite backups to S3
[Service]
Type=oneshot
Environment=AWS_REGION=${region}
ExecStart=/bin/bash /srv/ai-trader/backup.sh
EOF
cat > /etc/systemd/system/ai-trader-backup.timer <<'EOF'
[Unit]
Description=AI trader: nightly backup
[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable ai-trader-backup.timer

# Day-0 check: does Binance answer from here? (US addresses get HTTP 451.)
{
  echo "== Binance reachability from $(curl -fsS --max-time 5 https://checkip.amazonaws.com || echo '?') at $(date -u)"
  for u in https://fapi.binance.com/fapi/v1/time https://api.binance.com/api/v3/time https://testnet.binancefuture.com/fapi/v1/time; do
    printf '%s -> ' "$u"; curl -s -o /dev/null -w '%%{http_code}\n' --max-time 10 "$u" || echo "unreachable"
  done
} | tee /var/log/ai-trader-binance-check.log

echo "first boot done"
