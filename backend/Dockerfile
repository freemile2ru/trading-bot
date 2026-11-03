# The backend: one stateful process (FastAPI + every scheduler in-process) on a single worker, no reloader.
# Built for linux/arm64 (the Tokyo instance is Graviton) and linux/amd64 alike.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 APP_ENV=production HOST=0.0.0.0 PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# backend/data is both a Python package (the exchange clients) and the directory the SQLite files live in. In
# production the SQLite files sit on a mounted volume at /app/data, which hides the package's modules — so the
# modules are kept here and copied onto the volume at every start (docker-entrypoint.sh).
RUN mkdir -p /opt/data_pkg && cp data/*.py /opt/data_pkg/ \
    && chmod +x docker-entrypoint.sh && chown -R app:app /app /opt/data_pkg

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 CMD curl -fsS http://127.0.0.1:8000/health || exit 1
ENTRYPOINT ["./docker-entrypoint.sh"]
