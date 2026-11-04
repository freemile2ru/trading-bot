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


# ── A coin's own tape: don't open into a move already running against the leg ─────────────────────────────────
MOVE_PCT = 3.0          # the last hour moved at least this much against the leg…
MOVE_VOL_X = 2.0        # …on at least this many times the day's average hourly volume: a dump or a squeeze in progress
_coin_cache: dict[str, dict] = {}


async def coin_tape(symbol: str, now: float | None = None) -> dict | None:
    """{move_pct: the last hour's close-to-close move, vol_x: that hour's volume over the day's hourly average} on
    5-minute perpetual bars; None when the feed is down (callers must then not gate). Cached for CACHE_S."""
    now = now or time.time()
    c = _coin_cache.get(symbol)
    if c and now - c["at"] < CACHE_S:
        return c["data"]
    try:
        from data.binance_client import fetch_futures_klines
        bars = await fetch_futures_klines(symbol, "5m", 288)
        if len(bars) < 24:
            return None
        hour, day = bars[-12:], bars[-288:]
        hour_vol = sum(b.get("volume") or 0 for b in hour)
        avg_hour_vol = sum(b.get("volume") or 0 for b in day) / (len(day) / 12)
        ref = bars[-13]["close"] if len(bars) >= 13 else hour[0]["open"]
        data = {"move_pct": round((hour[-1]["close"] / ref - 1) * 100, 3) if ref else 0.0,
                "vol_x": round(hour_vol / avg_hour_vol, 2) if avg_hour_vol else 0.0, "at": now}
    except Exception as e:
        logger.debug("%s tape unavailable: %s", symbol, e)
        return None
    _coin_cache[symbol] = {"at": now, "data": data}
    return data


def against_move(direction: str, tape: dict | None) -> str | None:
    """Why a new leg in `direction` would open into a move already running against it (TRUMP, 2026-11-04: the factor
    basket bought a coin 5% down on the half hour, on 5× volume, and it fell another 5% in the next five minutes), or
    None — also None when the coin's tape is unknown. A deferral, not a verdict: the caller tries again next tick."""
    if not tape or tape.get("vol_x", 0) < MOVE_VOL_X:
        return None
    m = tape.get("move_pct", 0.0)
    if direction == "long" and m <= -MOVE_PCT:
        return f"mid-dump: {m:+.1f}% in the last hour on {tape['vol_x']:.1f}× volume — no new long into it"
    if direction == "short" and m >= MOVE_PCT:
        return f"mid-squeeze: {m:+.1f}% in the last hour on {tape['vol_x']:.1f}× volume — no new short into it"
    return None
