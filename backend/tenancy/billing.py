"""Stripe: Checkout for a new subscription, the customer portal for changes, the webhook that sets the tier.

Three REST calls over httpx (no SDK): the surface is small and the signature check is an HMAC. Without
STRIPE_SECRET_KEY the routes answer 503 and the Plans tab says billing is not set up; admins set tiers by hand.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time

import httpx

from tenancy import accounts, tiers

logger = logging.getLogger("tenancy.billing")

SECRET_KEY = os.getenv("STRIPE_SECRET_KEY") or None
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET") or None
PRICES = {tiers.PRO: os.getenv("STRIPE_PRICE_PRO") or None, tiers.ELITE: os.getenv("STRIPE_PRICE_ELITE") or None}
APP_URL = (os.getenv("APP_URL") or "http://localhost:3000").rstrip("/")
API = "https://api.stripe.com/v1"
TOLERANCE_S = 300


def configured() -> bool:
    return bool(SECRET_KEY and PRICES[tiers.PRO] and PRICES[tiers.ELITE])


def tier_of_price(price_id: str | None) -> str | None:
    return next((t for t, p in PRICES.items() if p and p == price_id), None)


async def _post(path: str, form: dict) -> dict:
    async with httpx.AsyncClient(timeout=20, auth=(SECRET_KEY or "", "")) as c:
        r = await c.post(f"{API}{path}", data=form)
    if r.status_code >= 400:
        msg = (r.json().get("error") or {}).get("message", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
        raise RuntimeError(f"Stripe: {msg}")
    return r.json()


async def checkout_url(tenant: dict, user: dict, tier: str) -> str:
    if tier not in PRICES or not PRICES[tier]:
        raise ValueError(f"no price for the {tier} plan")
    form = {
        "mode": "subscription", "line_items[0][price]": PRICES[tier], "line_items[0][quantity]": "1",
        "success_url": f"{APP_URL}/#pricing?checkout=success", "cancel_url": f"{APP_URL}/#pricing?checkout=cancel",
        "client_reference_id": tenant["id"], "metadata[tenant_id]": tenant["id"], "metadata[tier]": tier,
        "subscription_data[metadata][tenant_id]": tenant["id"], "subscription_data[metadata][tier]": tier,
        "allow_promotion_codes": "true",
    }
    if tenant.get("stripe_customer_id"):
        form["customer"] = tenant["stripe_customer_id"]
    elif user.get("email"):
        form["customer_email"] = user["email"]
    s = await _post("/checkout/sessions", form)
    return s["url"]


async def portal_url(tenant: dict) -> str:
    if not tenant.get("stripe_customer_id"):
        raise ValueError("no billing account yet — subscribe first")
    s = await _post("/billing_portal/sessions", {"customer": tenant["stripe_customer_id"], "return_url": f"{APP_URL}/#pricing"})
    return s["url"]


def verify_signature(payload: bytes, header: str | None, secret: str | None = None, now: float | None = None) -> bool:
    """Stripe-Signature: t=<ts>,v1=<hmac>[,v1=…]; HMAC-SHA256 of "<ts>.<payload>" with the endpoint secret."""
    secret = secret or WEBHOOK_SECRET
    if not secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    sigs = [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    try:
        ts = float(parts.get("t", "0"))
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), f"{int(ts)}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def handle_event(event: dict) -> dict:
    """Apply one Stripe event to a tenant. Idempotent by event id."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    tenant_id = meta.get("tenant_id") or obj.get("client_reference_id")
    if not tenant_id and obj.get("customer"):
        t = accounts.tenant_by_customer(obj["customer"])
        tenant_id = t["id"] if t else None
    if not accounts.record_event(event.get("id") or f"noid-{time.time()}", kind, tenant_id, {"object": obj.get("object"), "status": obj.get("status")}):
        return {"handled": False, "why": "already handled"}
    if not tenant_id or not accounts.tenant(tenant_id):
        logger.warning("Stripe %s for an unknown tenant (%s)", kind, tenant_id)
        return {"handled": False, "why": "unknown tenant"}
    if kind == "checkout.session.completed":
        tier = meta.get("tier") or tiers.PRO
        accounts.set_tier(tenant_id, tier, "active", stripe_customer_id=obj.get("customer"), stripe_subscription_id=obj.get("subscription"))
        return {"handled": True, "tenant": tenant_id, "tier": tier}
    if kind in ("customer.subscription.created", "customer.subscription.updated"):
        items = ((obj.get("items") or {}).get("data") or [{}])
        price_id = ((items[0].get("price") or {}).get("id")) if items else None
        tier = tier_of_price(price_id) or meta.get("tier") or tiers.PRO
        status = obj.get("status") or "active"
        status = status if status in accounts.STATUSES else ("canceled" if status in ("unpaid", "incomplete_expired") else "active")
        accounts.set_tier(tenant_id, tier if status != "canceled" else tiers.FREE, status,
                          stripe_customer_id=obj.get("customer"), stripe_subscription_id=obj.get("id"),
                          current_period_end=obj.get("current_period_end"), cancel_at_period_end=obj.get("cancel_at_period_end", False))
        return {"handled": True, "tenant": tenant_id, "tier": tier, "status": status}
    if kind == "customer.subscription.deleted":
        accounts.set_tier(tenant_id, tiers.FREE, "canceled", stripe_subscription_id=None, current_period_end=None, cancel_at_period_end=False)
        return {"handled": True, "tenant": tenant_id, "tier": tiers.FREE}
    return {"handled": False, "why": f"ignored {kind}"}


def status(tenant: dict) -> dict:
    return {"configured": configured(), "tier": tenant.get("tier"), "status": tenant.get("status"),
            "current_period_end": tenant.get("current_period_end"), "cancel_at_period_end": bool(tenant.get("cancel_at_period_end")),
            "stripe_customer": bool(tenant.get("stripe_customer_id"))}


def webhook_payload(body: bytes) -> dict:
    return json.loads(body.decode())
