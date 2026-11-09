"""Propr's REST API (api.propr.xyz/v1, docs at github.com/XBorgLabs/propr-docs) — the prop book's orders, mirrored
from its paper wallet when PROP_EXEC is "beta" (api.beta.propr.xyz, the firm's sandbox) or "live" and PROPR_API_KEY
is set. Off by default: the book is paper-only until the user has an account and a key.

The contract (read 2026-11-09): X-API-Key header, 1,200 requests a minute; the account is the active challenge attempt
(GET /challenge-attempts?status=active → accountId); leverage is set per asset (PUT margin-config) not per order; an
order is {intentId (ULID), type, side, positionSide, asset (bare coin, "xyz:NVDA" for HIP-3), base, quote "USDC",
quantity in base units as a string, timeInForce}; a stop is a separate stop_market reduce-only order that must share an
orderGroupId with the entry; closing is a reduce-only market order with closePosition. Nothing here reads a balance:
the API has no equity endpoint, so the rules run on our ledger (experiments/prop.py).
"""
from __future__ import annotations

import logging
import os
import secrets
import time

logger = logging.getLogger("experiments.prop_exec")

MODE = os.getenv("PROP_EXEC", "off")                          # off | beta | live
API_KEY = os.getenv("PROPR_API_KEY", "")
URLS = {"beta": "https://api.beta.propr.xyz/v1", "live": "https://api.propr.xyz/v1"}
TIMEOUT = 20.0
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def enabled() -> bool:
    return MODE in URLS and bool(API_KEY)


def ulid(now_ms: int | None = None) -> str:
    """A ULID (Crockford base32: 48-bit time, 80 random bits) — the API's idempotency key."""
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    out = []
    for _ in range(10):
        out.append(_B32[ts & 31])
        ts >>= 5
    rnd = int.from_bytes(secrets.token_bytes(10), "big")
    for _ in range(16):
        out.append(_B32[rnd & 31])
        rnd >>= 5
    return "".join(reversed(out))


class ProprError(Exception):
    def __init__(self, status: int, code: int | None, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class Client:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, transport=None):
        self.base = (base_url or URLS.get(MODE, URLS["beta"])).rstrip("/")
        self.key = api_key if api_key is not None else API_KEY
        self._transport = transport                               # tests pass an httpx.MockTransport

    async def _req(self, method: str, path: str, json: dict | None = None, params: dict | None = None):
        import httpx
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as c:
            r = await c.request(method, f"{self.base}{path}", json=json, params=params, headers={"X-API-Key": self.key, "content-type": "application/json"})
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {}
            raise ProprError(r.status_code, body.get("code"), body.get("message") or r.text[:200])
        return r.json() if r.content else {}

    async def health(self) -> bool:
        return (await self._req("GET", "/health")).get("status") == "OK"

    async def account_id(self) -> str | None:
        d = await self._req("GET", "/challenge-attempts", params={"status": "active", "limit": 5})
        rows = d.get("data") if isinstance(d, dict) else d
        return (rows or [{}])[0].get("accountId")

    async def leverage_limits(self) -> dict:
        return await self._req("GET", "/leverage-limits/effective")

    async def set_leverage(self, account: str, coin: str, leverage: int, margin_mode: str = "cross") -> dict:
        cfg = await self._req("GET", f"/accounts/{account}/margin-config/{coin}")
        return await self._req("PUT", f"/accounts/{account}/margin-config/{cfg['configId']}",
                               json={"exchange": "hyperliquid", "asset": coin, "marginMode": margin_mode, "leverage": int(leverage)})

    @staticmethod
    def _order(account: str, coin: str, side: str, position_side: str, qty: str, type_: str, tif: str, **more) -> dict:
        return {"accountId": account, "intentId": ulid(), "exchange": "hyperliquid", "productType": "perp", "type": type_, "side": side,
                "positionSide": position_side, "asset": coin, "base": coin, "quote": "USDC", "quantity": qty, "timeInForce": tif, **more}

    def entry_with_stop(self, account: str, coin: str, direction: str, qty: str, stop_px: float) -> dict:
        """The body for a market entry and its reduce-only stop in one group."""
        long = direction == "long"
        group = ulid()
        entry = self._order(account, coin, "buy" if long else "sell", "long" if long else "short", qty, "market", "IOC")
        stop = self._order(account, coin, "sell" if long else "buy", "long" if long else "short", qty, "stop_market", "GTC",
                           triggerPrice=f"{stop_px:.10g}", reduceOnly=True)
        return {"orderGroupId": group, "orders": [entry, stop]}

    async def open(self, account: str, coin: str, direction: str, qty: str, stop_px: float) -> list[dict]:
        d = await self._req("POST", f"/accounts/{account}/orders", json=self.entry_with_stop(account, coin, direction, qty, stop_px))
        return d.get("data") or []

    async def close(self, account: str, coin: str, direction: str, qty: str) -> list[dict]:
        long = direction == "long"
        body = {"orders": [self._order(account, coin, "sell" if long else "buy", "long" if long else "short", qty, "market", "IOC",
                                       reduceOnly=True, closePosition=True)]}
        d = await self._req("POST", f"/accounts/{account}/orders", json=body)
        return d.get("data") or []

    async def positions(self, account: str, status: str = "open") -> list[dict]:
        d = await self._req("GET", f"/accounts/{account}/positions", params={"status": status, "limit": 50})
        return d.get("data") or []


def describe() -> dict:
    return {"mode": MODE, "enabled": enabled(), "url": URLS.get(MODE), "key_set": bool(API_KEY)}
