# The web tier: the React build served by Caddy, which also terminates TLS, enforces the password and proxies /api
# to the backend. Build context is the repository root (docker build -f frontend/Dockerfile .) so the Caddyfile in
# deploy/ can be copied in.
FROM node:24-alpine AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
COPY deploy/Caddyfile /etc/caddy/Caddyfile
