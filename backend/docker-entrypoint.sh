#!/bin/sh
# Production start: refresh the data package's modules onto the mounted data volume, then run one uvicorn worker
# without the reloader (every scheduler runs inside this process, and the SQLite files allow exactly one writer).
set -e
mkdir -p /app/data
cp /opt/data_pkg/*.py /app/data/
exec uvicorn main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --workers 1 --proxy-headers --forwarded-allow-ips="*"
