#!/usr/bin/env bash
# Runs ON the instance (sent by GitHub Actions through SSM Run Command). Brings the compose project to the given
# image tags: secrets from SSM Parameter Store → files, pull, up, health check, roll back to the previous tags if the
# backend does not come up healthy.
#
#   deploy.sh <backend image> <web image>
set -euo pipefail

BACKEND_IMAGE="$1"
WEB_IMAGE="$2"
ROOT=/srv/ai-trader
REGION="${AWS_REGION:-$(curl -fsS --max-time 2 http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || echo ap-northeast-1)}"
cd "$ROOT"

param() { aws ssm get-parameter --region "$REGION" --with-decryption --name "$1" --query Parameter.Value --output text; }

echo "== secrets from SSM"
umask 077
param /ai-trader/app-env > app.env.new
echo "DATABASE_URL=$(param /ai-trader/database-url)" >> app.env.new      # RDS Postgres (storage/engine.py); remove the line to run on SQLite files
mv app.env.new app.env
{
  echo "BACKEND_IMAGE=$BACKEND_IMAGE"
  echo "WEB_IMAGE=$WEB_IMAGE"
  param /ai-trader/web-env | sed 's/\$/$$/g'       # compose reads $NAME as a variable: a bcrypt hash's $2a$14$… must be $$-escaped
} > .env.new
[ -f .env ] && cp .env .env.previous
mv .env.new .env
umask 022

echo "== registry login"
param /ai-trader/ghcr-token | docker login ghcr.io -u "$(param /ai-trader/ghcr-user)" --password-stdin

echo "== pull and start $BACKEND_IMAGE / $WEB_IMAGE"
docker compose pull --quiet
docker compose up -d --remove-orphans

echo "== waiting for the backend"
for i in $(seq 1 40); do
  if docker compose exec -T backend curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "healthy after $((i * 3))s"
    docker image prune -f >/dev/null
    exit 0
  fi
  sleep 3
done

echo "!! backend never became healthy — rolling back"
docker compose logs --tail=80 backend || true
if [ -f .env.previous ]; then
  mv .env.previous .env
  docker compose up -d --remove-orphans
fi
exit 1
