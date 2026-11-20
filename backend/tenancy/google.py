"""Google sign-in: the browser gets an ID token from Google Identity Services and posts it here; we verify it with
Google's tokeninfo endpoint (signature, expiry) and check it was issued to OUR client and to a verified email."""
from __future__ import annotations

import os
import time

import httpx

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID") or None
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


def configured() -> bool:
    return bool(CLIENT_ID)


def dev_login_enabled() -> bool:
    """A local instance without a Google client can sign in by email, to exercise accounts; never in production."""
    return not configured() and os.getenv("APP_ENV") != "production" and os.getenv("DEV_LOGIN", "true").lower() != "false"


class BadToken(ValueError):
    pass


def check_claims(claims: dict, now: float | None = None) -> dict:
    """The parts of verification that are ours: audience, issuer, expiry, a verified email."""
    if claims.get("aud") != CLIENT_ID:
        raise BadToken("token was not issued to this app")
    if claims.get("iss") not in ISSUERS:
        raise BadToken("token was not issued by Google")
    if float(claims.get("exp") or 0) < (now or time.time()):
        raise BadToken("token has expired")
    if str(claims.get("email_verified", "false")).lower() != "true" or not claims.get("email"):
        raise BadToken("Google has not verified this email")
    return {"email": claims["email"], "name": claims.get("name"), "picture": claims.get("picture"), "sub": claims.get("sub")}


async def verify(id_token: str) -> dict:
    if not configured():
        raise BadToken("Google sign-in is not configured (GOOGLE_CLIENT_ID)")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(TOKENINFO, params={"id_token": id_token})
    if r.status_code != 200:
        raise BadToken("Google rejected the token")
    return check_claims(r.json())
