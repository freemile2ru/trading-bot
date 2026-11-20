"""Resolve the session on every request and apply the subscriber policy.

  no cookie, AUTH_REQUIRED false (the local instance)   → the owner, unpoliced, as before accounts existed
  no cookie, AUTH_REQUIRED true (production)             → 401 on everything but the public paths
  a valid cookie                                         → that user and tenant; admins are the owner tenant, unpoliced;
                                                           every other tenant goes through gate.policy
"""
from __future__ import annotations

import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse

from tenancy import accounts, context, gate, sessions

logger = logging.getLogger("tenancy.middleware")

def _default_required() -> bool:
    """Sign-in is required by default only in production AND once a Google client is configured — a production instance
    without one (today's: the Caddy password in front, no accounts set up) keeps serving the owner's view, never a
    sign-in page nobody can pass."""
    v = os.getenv("AUTH_REQUIRED")
    if v is not None:
        return v.lower() == "true"
    return os.getenv("APP_ENV") == "production" and bool(os.getenv("GOOGLE_CLIENT_ID"))


AUTH_REQUIRED = _default_required()
PUBLIC = ("/api/auth/", "/api/pricing", "/api/billing/webhook", "/api/health", "/docs", "/openapi.json", "/redoc")


def resolve(request: Request) -> tuple[str | None, dict | None]:
    """(tenant_id, user) for the request's cookie; (None, None) when there is none or it is invalid."""
    claims = sessions.read(request.cookies.get(sessions.COOKIE))
    if not claims:
        return None, None
    u = accounts.user(claims.get("sub", ""))
    if not u or u["tenant_id"] != claims.get("tid"):
        return None, None
    t = accounts.tenant(u["tenant_id"])
    user = {**u, "tier": (t or {}).get("tier", "free"), "tenant_status": (t or {}).get("status", "active")}
    return u["tenant_id"], user


async def session_middleware(request: Request, call_next):
    path = request.url.path
    tenant, user = resolve(request)
    if user is None and AUTH_REQUIRED and path.startswith("/api/") and not path.startswith(PUBLIC):
        return JSONResponse({"detail": "sign in first"}, status_code=401)
    tokens = context.set_current(context.OWNER if user and user.get("role") == "admin" else tenant, user)
    try:
        if user is not None and user.get("role") != "admin":
            refused = gate.policy(request.method, path, user.get("tier"))
            if refused:
                status, body = refused
                return JSONResponse(body, status_code=status)
        return await call_next(request)
    finally:
        context.reset(tokens)
