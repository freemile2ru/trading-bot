"""Accounts, plans, billing, the tenant's settings and the admin's view (docs/saas.md)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from tenancy import accounts, billing, context, gate, google, sessions, tiers, wallets, runner

logger = logging.getLogger("api.tenancy")
router = APIRouter(tags=["accounts"])


def _session(user: dict | None) -> dict:
    """The Session the frontend renders. No user: the owner session (the local instance without a sign-in)."""
    if user is None:
        t = accounts.tenant(context.OWNER) or {"id": context.OWNER, "name": "Owner", "tier": tiers.ELITE, "status": "active"}
        u = {"id": "owner", "email": None, "name": "Owner", "picture": None, "role": "admin"}
    else:
        tid = context.OWNER if user.get("role") == "admin" else user["tenant_id"]
        t = accounts.tenant(tid) or {"id": tid, "name": "?", "tier": tiers.FREE, "status": "active"}
        u = {k: user.get(k) for k in ("id", "email", "name", "picture", "role")}
    tier = t.get("tier") or tiers.FREE
    return {"user": u,
            "tenant": {"id": t["id"], "name": t.get("name"), "tier": tier, "status": t.get("status", "active"),
                       "current_period_end": t.get("current_period_end"), "cancel_at_period_end": bool(t.get("cancel_at_period_end"))},
            "features": tiers.features(tier), "limits": tiers.limits(tier)}


def _set_cookie(resp: Response, user: dict) -> None:
    resp.set_cookie(value=sessions.issue(user["id"], user["tenant_id"]), **sessions.cookie_kwargs())


# ── Auth ──────────────────────────────────────────────────────────────────────

@router.get("/auth/config")
async def auth_config():
    from tenancy.middleware import AUTH_REQUIRED
    from tenancy import engines
    return {"google_client_id": google.CLIENT_ID, "dev_login": google.dev_login_enabled(), "auth_required": AUTH_REQUIRED,
            "tenant_engines": engines.FLAG}


class GoogleIn(BaseModel):
    credential: str = Field(..., min_length=20, max_length=4096)


@router.post("/auth/google")
async def auth_google(body: GoogleIn, response: Response):
    try:
        claims = await google.verify(body.credential)
    except google.BadToken as e:
        raise HTTPException(401, str(e))
    user = await asyncio.to_thread(accounts.sign_in, claims["email"], claims.get("name"), claims.get("picture"), claims.get("sub"))
    _set_cookie(response, user)
    t = accounts.tenant(user["tenant_id"])
    return _session({**user, "tier": t["tier"] if t else tiers.FREE})


class DevLoginIn(BaseModel):
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    name: str | None = Field(None, max_length=120)


@router.post("/auth/dev-login")
async def auth_dev_login(body: DevLoginIn, response: Response):
    if not google.dev_login_enabled():
        raise HTTPException(404, "not here")
    user = await asyncio.to_thread(accounts.sign_in, body.email, body.name)
    _set_cookie(response, user)
    t = accounts.tenant(user["tenant_id"])
    return _session({**user, "tier": t["tier"] if t else tiers.FREE})


@router.post("/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(sessions.COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def auth_me():
    from tenancy.middleware import AUTH_REQUIRED
    u = context.user()
    if u is None and AUTH_REQUIRED:
        raise HTTPException(401, "sign in first")
    return _session(u)


# ── Plans & billing ───────────────────────────────────────────────────────────

@router.get("/pricing")
async def pricing():
    from tenancy.middleware import AUTH_REQUIRED
    u = context.user()
    current = u.get("tier") if u else (tiers.ELITE if not AUTH_REQUIRED else None)
    return tiers.catalogue(current, billing.configured())


class CheckoutIn(BaseModel):
    tier: str = Field(..., pattern=r"^(pro|elite)$")


@router.post("/billing/checkout")
async def billing_checkout(body: CheckoutIn, user: dict = Depends(gate.require_signed_in)):
    if not billing.configured():
        raise HTTPException(503, "billing is not set up yet")
    t = accounts.tenant(user["tenant_id"])
    try:
        return {"url": await billing.checkout_url(t, user, body.tier)}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@router.post("/billing/portal")
async def billing_portal(user: dict = Depends(gate.require_signed_in)):
    if not billing.configured():
        raise HTTPException(503, "billing is not set up yet")
    try:
        return {"url": await billing.portal_url(accounts.tenant(user["tenant_id"]))}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@router.get("/billing/status")
async def billing_status():
    u = context.user()
    t = accounts.tenant(u["tenant_id"] if u else context.OWNER) or {}
    return billing.status(t)


@router.post("/billing/webhook")
async def billing_webhook(request: Request):
    body = await request.body()
    if not billing.verify_signature(body, request.headers.get("stripe-signature")):
        raise HTTPException(400, "bad signature")
    try:
        event = billing.webhook_payload(body)
    except ValueError:
        raise HTTPException(400, "bad payload")
    return await asyncio.to_thread(billing.handle_event, event)


# ── The tenant's own settings ─────────────────────────────────────────────────

def _settings_out(tid: str) -> dict:
    from tenancy import engines
    t = accounts.tenant(tid) or {"id": tid, "tier": tiers.FREE}
    s = accounts.settings(tid)
    return {**s, "allowed_books": list(tiers.books_allowed(t["tier"])), "exchange_keys": accounts.exchange_keys(tid),
            "engine_modes_allowed": engines.modes_allowed(t)}


class SettingsIn(BaseModel):
    risk_x: float | None = Field(None, ge=0.25, le=2.0)
    paper_capital_usd: float | None = Field(None, ge=1000, le=1_000_000)
    books: dict[str, bool] | None = None
    engine_mode: str | None = Field(None, pattern=r"^(mirror|own)$")


@router.get("/me/settings")
async def me_settings():
    return _settings_out(context.tenant_id())


@router.post("/me/settings")
async def me_settings_post(body: SettingsIn):
    tid = context.tenant_id()
    if tid == context.OWNER:
        raise HTTPException(400, "the owner's books are switched on the Books tab")
    try:
        accounts.update_settings(tid, risk_x=body.risk_x, paper_capital_usd=body.paper_capital_usd, books=body.books, engine_mode=body.engine_mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _settings_out(tid)


class KeysIn(BaseModel):
    venue: str = Field(..., pattern=r"^(binance|hyperliquid)$")
    api_key: str = Field(..., min_length=8, max_length=512)
    api_secret: str = Field(..., min_length=8, max_length=512)


@router.post("/me/exchange-keys", dependencies=[Depends(gate.require("mirror.exchange"))])
async def me_keys(body: KeysIn):
    tid = context.tenant_id()
    await asyncio.to_thread(accounts.set_exchange_key, tid, body.venue, body.api_key, body.api_secret)
    return {"ok": True}


@router.delete("/me/exchange-keys/{venue}")
async def me_keys_delete(venue: str):
    await asyncio.to_thread(accounts.delete_exchange_key, context.tenant_id(), venue)
    return {"ok": True}


# ── Admin ─────────────────────────────────────────────────────────────────────

@router.get("/admin/tenants", dependencies=[Depends(gate.require_admin)])
async def admin_tenants():
    def load():
        out = []
        for t in accounts.tenants():
            users = accounts.users_of(t["id"])
            owner = next((u for u in users if u["id"] == t.get("owner_user_id")), users[0] if users else None)
            out.append({"id": t["id"], "name": t["name"], "tier": t["tier"], "status": t["status"], "owner_email": (owner or {}).get("email"),
                        "users": len(users), "created_ts": t["created_ts"], "stripe_customer": bool(t.get("stripe_customer_id")),
                        "equity_usd": None if t["id"] == context.OWNER else wallets.equity(t["id"])})
        return out
    return {"tenants": await asyncio.to_thread(load)}


class TierIn(BaseModel):
    tier: str = Field(..., pattern=r"^(free|pro|elite)$")


@router.post("/admin/tenants/{tenant_id}/tier", dependencies=[Depends(gate.require_admin)])
async def admin_set_tier(tenant_id: str, body: TierIn):
    if not accounts.tenant(tenant_id):
        raise HTTPException(404, "no such tenant")
    if tenant_id == context.OWNER:
        raise HTTPException(400, "the owner tenant is always Elite")
    return accounts.set_tier(tenant_id, body.tier, "active")


@router.get("/admin/runner", dependencies=[Depends(gate.require_admin)])
async def admin_runner():
    from tenancy import engines
    return {**runner.status(), "engines": engines.status()}
