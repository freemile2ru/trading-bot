"""The market's tape: where BTC trades against its own 24-hour VWAP. A gate for rules that buy weakness — a leader's
alt long on a day the whole market is being sold is a different trade from the same long on a green day (the copy
books' first stops, 2026-11-01/02, were all alt longs on a red day; so was the reversal lane's worst day)."""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("trading.tape")

CACHE_S = 300
_cache: dict = {"at": 0.0, "data": None}


async def btc_tape(now: float | None = None) -> dict | None:
    """{price, vwap, pct (price vs VWAP, %), tilt: "up" | "down"} on 5-minute perpetual bars over the last 24 hours;
    None when the feed is down (callers must then not gate)."""
    now = now or time.time()
    if _cache["data"] is not None and now - _cache["at"] < CACHE_S:
        return _cache["data"]
    try:
        from data.binance_client import fetch_futures_klines
        bars = await fetch_futures_klines("BTCUSDT", "5m", 288)
        vol = sum(b.get("volume") or 0 for b in bars)
        if not bars or vol <= 0:
            return None
        vwap = sum((b["high"] + b["low"] + b["close"]) / 3 * (b.get("volume") or 0) for b in bars) / vol
        price = bars[-1]["close"]
        data = {"price": price, "vwap": round(vwap, 2), "pct": round((price / vwap - 1) * 100, 3), "tilt": "up" if price >= vwap else "down", "at": now}
    except Exception as e:
        logger.debug("BTC tape unavailable: %s", e)
        return None
    _cache.update(at=now, data=data)
    return data


def against_tape(direction: str, tape: dict | None) -> str | None:
    """Why a new leg in `direction` fights the tape, or None (also None when the tape is unknown)."""
    if not tape:
        return None
    if direction == "long" and tape["tilt"] == "down":
        return f"tape: BTC {tape['pct']:+.2f}% below its 24h VWAP — no new longs"
    if direction == "short" and tape["tilt"] == "up":
        return f"tape: BTC {tape['pct']:+.2f}% above its 24h VWAP — no new shorts"
    return None
