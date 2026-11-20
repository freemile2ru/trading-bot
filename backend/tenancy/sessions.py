"""The session: a signed token in an HttpOnly cookie (PyJWT, HS256). SESSION_SECRET signs it — required in production;
a local instance without one gets a per-process secret, so sessions end with a restart there."""
from __future__ import annotations

import logging
import os
import secrets
import time

import jwt

logger = logging.getLogger("tenancy.sessions")

COOKIE = "oracle_session"
TTL_S = float(os.getenv("SESSION_TTL_DAYS", "30")) * 86400
_fallback: str | None = None


def secret() -> str:
    global _fallback
    s = os.getenv("SESSION_SECRET")
    if s:
        return s
    if os.getenv("APP_ENV") == "production":
        raise RuntimeError("SESSION_SECRET is required in production")
    if _fallback is None:
        _fallback = secrets.token_urlsafe(32)
        logger.warning("SESSION_SECRET not set — sessions end when this process does")
    return _fallback


def issue(user_id: str, tenant_id: str, ttl_s: float | None = None) -> str:
    now = time.time()
    return jwt.encode({"sub": user_id, "tid": tenant_id, "iat": int(now), "exp": int(now + (ttl_s or TTL_S))}, secret(), algorithm="HS256")


def read(token: str | None) -> dict | None:
    """The claims of a valid token, else None (expired, tampered, missing)."""
    if not token:
        return None
    try:
        return jwt.decode(token, secret(), algorithms=["HS256"])
    except (jwt.PyJWTError, RuntimeError):                        # tampered, expired — or production without SESSION_SECRET: no session, not a crash
        return None


def cookie_kwargs() -> dict:
    return {"key": COOKIE, "httponly": True, "samesite": "lax", "secure": os.getenv("APP_ENV") == "production",
            "max_age": int(TTL_S), "path": "/"}
