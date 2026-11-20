"""Feature gates and the route policy for subscriber accounts.

Two layers. `require(feature)` is a FastAPI dependency a route can declare; it answers 402 with the tier to upgrade to.
`policy(method, path, tier, admin)` is what the middleware applies to EVERY request from a subscriber tenant: the
app's routes were written for one owner, so a subscriber may reach only the paths listed here — reads of the market
and the engines' output by feature, and writes to their own account, books and positions. Everything else (the Trade
terminal's order form, the live account, the paper wallet of the owner, ops) is refused until it is made tenant-aware,
never silently served from the owner's data. The owner tenant (no session locally, or an admin) is not policed.
"""
from __future__ import annotations

import re

from fastapi import HTTPException, Request

from tenancy import context, tiers

# path prefix → feature needed (GET). Longest prefix wins.
READ_FEATURES: dict[str, str | None] = {
    "/api/auth": None, "/api/pricing": None, "/api/billing": None, "/api/me": None, "/api/health": None,
    "/api/ops/market-state": None, "/api/ops/calendar": "calendar", "/api/calendar": "calendar", "/api/news": "news",
    "/api/signals/fired": "signals.delayed", "/api/signals": "signals.live", "/api/oracle": "signals.live",
    "/api/stream": "signals.live", "/api/market": "signals.delayed", "/api/ohlcv": "signals.delayed",
    "/api/trades": "trades", "/api/portfolio": "trades", "/api/books": "trades", "/api/positions": "trades",
    "/api/debriefs": "trades", "/api/history": "trades", "/api/analytics": "trades", "/api/capital": "trades",
    "/api/markets": "markets", "/api/copy": "copy.view", "/api/auction": "mentor", "/api/prop": "prop",
    "/api/composite": "one", "/api/control": "control", "/api/index-shadow": "control", "/api/lab": "lab",
    "/api/retired": "retired", "/api/notify": "alerts.email",
}
# Writes a subscriber may make: (method, path regex) → feature (None: any signed-in tenant).
WRITE_RULES: list[tuple[str, re.Pattern, str | None]] = [
    ("POST", re.compile(r"^/api/auth/"), None),
    ("POST", re.compile(r"^/api/billing/"), None),
    ("POST", re.compile(r"^/api/me/"), None),
    ("DELETE", re.compile(r"^/api/me/"), None),
    ("POST", re.compile(r"^/api/books/[a-z_]+$"), "paper.mirror"),               # their own book switches
    ("POST", re.compile(r"^/api/positions/[a-z_]+/[^/]+/(close|levels)$"), "paper.mirror"),   # their own legs
    ("POST", re.compile(r"^/api/notify/"), "alerts.email"),
]
NEVER_FOR_TENANTS = ("/api/live", "/api/paper", "/api/trade/", "/api/ops/", "/api/admin", "/api/guards", "/api/scalper", "/api/recorder")


def feature_for_read(path: str) -> str | None | bool:
    """The feature a GET of `path` needs; None for public; False when the path is not open to tenants at all."""
    if path.startswith(NEVER_FOR_TENANTS) and not path.startswith("/api/ops/market-state") and not path.startswith("/api/ops/calendar"):
        return False
    best = None
    for prefix, feat in READ_FEATURES.items():
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, feat)
    return best[1] if best else False


def policy(method: str, path: str, tier: str | None, admin: bool = False) -> tuple[int, dict] | None:
    """None when the request may proceed; else (status, body) to answer with."""
    if admin or context.is_owner():
        return None
    if not path.startswith("/api/"):
        return None
    if method in ("GET", "HEAD", "OPTIONS"):
        feat = feature_for_read(path)
        if feat is False:
            return 403, {"detail": "not available to subscriber accounts"}
        if feat and not tiers.has(tier, feat):
            return 402, {"detail": f"{tiers.FEATURES[feat][0]} is not in the {tiers.tier(tier)['name']} plan",
                         "feature": feat, "upgrade": tiers.upgrade_for(feat)}
        return None
    for m, rx, feat in WRITE_RULES:
        if m == method and rx.match(path):
            if feat and not tiers.has(tier, feat):
                return 402, {"detail": f"{tiers.FEATURES[feat][0]} is not in the {tiers.tier(tier)['name']} plan",
                             "feature": feat, "upgrade": tiers.upgrade_for(feat)}
            return None
    return 403, {"detail": "not available to subscriber accounts"}


def current_tier() -> str:
    u = context.user()
    return (u or {}).get("tier") or (tiers.ELITE if context.is_owner() else tiers.FREE)


def require(feature: str):
    """A route dependency: 402 when the caller's tier lacks the feature."""
    def dep(request: Request) -> None:
        if context.is_owner() or context.is_admin():
            return
        if not tiers.has(current_tier(), feature):
            raise HTTPException(402, {"detail": f"{tiers.FEATURES[feature][0]} needs the {tiers.tier(tiers.upgrade_for(feature))['name']} plan",
                                      "feature": feature, "upgrade": tiers.upgrade_for(feature)})
    return dep


def require_signed_in(request: Request) -> dict:
    u = context.user()
    if not u:
        raise HTTPException(401, "sign in first")
    return u


def require_admin(request: Request) -> dict:
    u = context.user()
    if u is None and context.is_owner():
        return {"id": "owner", "role": "admin"}                   # the local instance without a sign-in (production requires one)
    if not u or u.get("role") != "admin":
        raise HTTPException(403, "admins only")
    return u
