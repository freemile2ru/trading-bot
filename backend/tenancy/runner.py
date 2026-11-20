"""Per-tenant execution: the engines' entries mirrored into subscribers' own wallets, their legs managed every minute.

The engines run once, as the owner. Each time an owner-tenant PaperWallet opens or closes a trade it publishes an
event here (paper_trader → publish). The runner's tick drains the queue: every opened leg is mirrored into each active
tenant that runs that book — sized by RISK PARITY (the same share of equity at the stop as the source leg, times the
tenant's risk multiplier), the same entry, stop, targets, leverage and exit policy — and every source close closes
the mirror at the source's price unless the tenant's own stop or lock already did. Then `monitor.run` manages the
tenants' open legs (stops, targets, trails) with the same code that manages the owner's.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

from tenancy import accounts, context, tiers, wallets

logger = logging.getLogger("tenancy.runner")

ENABLED = os.getenv("TENANT_RUNNER", "true").lower() != "false"
TICK_S = int(os.getenv("TENANT_RUNNER_TICK_S", "60"))
MIRRORED_FIELDS = ("exit_policy", "lock_arm_pct", "lock_keep", "lock_trail_atr", "atr_pct", "strategy", "lane", "stop_pct", "be_at_pct", "market")

_queue: deque = deque(maxlen=5000)
_state = {"last_tick": None, "mirrored_24h": deque(maxlen=5000), "errors": deque(maxlen=20), "ticks": 0}


def publish(kind: str, book: str, trade: dict, equity: float) -> None:
    """Called by the owner's wallets on open and close. Cheap: it appends; the tick does the work."""
    if not ENABLED or book not in tiers.ALL_BOOKS:
        return
    _queue.append({"kind": kind, "book": book, "trade": dict(trade), "equity": float(equity or 0.0), "ts": time.time()})


def size_for(trade: dict, source_equity: float, tenant_equity: float, risk_x: float) -> tuple[float, float]:
    """(risk_usd, notional_usd) for the tenant's mirror: the source's risk share of equity, on the tenant's equity."""
    risk = float(trade.get("risk_usd") or 0.0)
    notional = float(trade.get("notional_usd") or (trade.get("entry_price", 0) * trade.get("quantity", 0)) or 0.0)
    if risk <= 0 or source_equity <= 0 or notional <= 0:
        return 0.0, 0.0
    share = risk / source_equity
    t_risk = share * tenant_equity * risk_x
    return round(t_risk, 2), round(notional * (t_risk / risk), 2)


def _mirror_open(tenant: dict, ev: dict) -> dict | None:
    tid, book, src = tenant["id"], ev["book"], ev["trade"]
    s = accounts.settings(tid)
    w = wallets.get(tid, book)
    eq = w.get_portfolio()["total_equity"]
    risk, notional = size_for(src, ev["equity"], eq, s["risk_x"])
    if notional < 10:
        return None
    if any(t.get("source_trade_id") == src["id"] for t in w.get_portfolio()["open_trades"]):
        return None
    extra = {k: src[k] for k in MIRRORED_FIELDS if src.get(k) is not None}
    extra.update({"source_trade_id": src["id"], "source_book": book, "mirror": True, "shadows": False, "claude_verdict": "skipped",
                  "entry_type": "mirror", "risk_usd": risk})
    t = w.open_trade(symbol=src["symbol"], direction=src["direction"], entry_price=float(src["entry_price"]),
                     stop_loss=float(src["stop_loss"]), take_profit_1=float(src.get("take_profit_1") or 0), take_profit_2=float(src.get("take_profit_2") or 0),
                     notional_usd=notional, leverage=src.get("leverage"), timeframe=src.get("timeframe") or "mirror",
                     hold_duration=src.get("hold_duration") or "as the source", extra=extra)
    if t.get("error"):
        logger.info("Mirror of %s %s for tenant %s refused: %s", book, src["symbol"], tid, t["error"])
        return None
    if risk:
        w.annotate(t["id"], risk_usd=risk)
    _state["mirrored_24h"].append(time.time())
    return t


def _mirror_close(tenant: dict, ev: dict) -> dict | None:
    tid, book, src = tenant["id"], ev["book"], ev["trade"]
    if (tid, book) not in wallets._cache and book not in wallets.books_on(tid):
        return None
    w = wallets.get(tid, book)
    mine = next((t for t in w.get_portfolio()["open_trades"] if t.get("source_trade_id") == src["id"]), None)
    if not mine:
        return None
    price = float(src.get("exit_price") or src.get("current_price") or mine.get("current_price") or mine["entry_price"])
    return w.close_trade_manually(mine["id"], price, f"source: {src.get('close_reason') or 'closed'}")


def _apply(ev: dict) -> int:
    n = 0
    from tenancy import engines
    for tenant in accounts.active_tenants():
        if engines.enabled_for(tenant):
            continue                                                 # their engines run on their own account; nothing to mirror
        with context.as_tenant(tenant["id"]):
            try:
                if ev["book"] not in wallets.books_on(tenant["id"]) and ev["kind"] == "opened":
                    continue
                done = _mirror_open(tenant, ev) if ev["kind"] == "opened" else _mirror_close(tenant, ev)
                n += 1 if done else 0
            except Exception as e:                                   # one tenant's trouble never stops the others
                logger.exception("Runner: %s %s for tenant %s failed: %s", ev["kind"], ev["book"], tenant["id"], e)
                _state["errors"].append({"ts": time.time(), "tenant": tenant["id"], "error": repr(e)[:200]})
    return n


async def tick(now: float | None = None) -> dict:
    """Drain the event queue into the tenants' wallets, then manage every tenant's open legs."""
    if not ENABLED:
        return {"skipped": "off"}
    now = now or time.time()
    applied = 0
    while _queue:
        applied += await asyncio.to_thread(_apply, _queue.popleft())
    managed = 0
    from trading import monitor
    for tenant in accounts.active_tenants():
        with context.as_tenant(tenant["id"]):
            for book in wallets.existing(tenant["id"]):
                w = wallets.get(tenant["id"], book)
                if not w.get_portfolio()["open_trades"]:
                    continue
                try:
                    await monitor.run(w)
                    managed += 1
                except Exception as e:
                    logger.warning("Runner: managing %s for tenant %s failed: %s", book, tenant["id"], e)
                    _state["errors"].append({"ts": now, "tenant": tenant["id"], "error": repr(e)[:200]})
    _state["last_tick"] = now
    _state["ticks"] += 1
    return {"applied": applied, "managed": managed}


def status() -> dict:
    cut = time.time() - 86400
    return {"enabled": ENABLED, "tenants_active": len(accounts.active_tenants()), "queued": len(_queue),
            "mirrored_24h": sum(1 for t in _state["mirrored_24h"] if t > cut), "last_tick": _state["last_tick"],
            "ticks": _state["ticks"], "errors": list(_state["errors"])[-10:]}
