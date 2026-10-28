"""The market regime a trade or scan happened in — so results can be split by conditions.

  trend       BTC above ("up") or below ("down") its 20-day average close
  volatility  BTC's 30-day realised volatility (annualised %, from hourly closes — the same
              measure as the autotrader's regime gate) — "calm" below CALM_BELOW, else "volatile"
  label       e.g. "BTC uptrend · calm"
  activity    how much BTC is moving right now: its 24h high-low range against its median 24h range over
              the last 30 days — "quiet" under QUIET_X (0.7×), "volatile" over BUSY_X (1.5×), else "normal".
              On a year of 15m data (research/quiet_market_lab.py) BTC was quiet 23% of the time, and no
              quiet-market strategy we tried — range fades, squeeze breakouts, 24h reversal — made money
              after costs: tight ranges mean tight stops, so fees take ~0.17R a trade. Quiet = sit out.

Stamped on every new trade (regime_at_entry) and every scan reading, cached for 15 minutes.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("regime")

CALM_BELOW = 37.0                 # matches AUTOTRADER_MAX_BTC_VOL: the 1-year replay's calm/volatile line
QUIET_X, BUSY_X = 0.7, 1.5
TTL = 900
_cache: dict = {"at": 0.0, "value": None}


def label(trend: str | None, vol: float | None) -> str | None:
    if trend is None or vol is None:
        return None
    return f"BTC {trend}trend · {'calm' if vol < CALM_BELOW else 'volatile'}"


async def _activity() -> dict:
    """BTC's 24h range now vs its 30-day median 24h range (from hourly bars)."""
    from data.binance_client import fetch_ohlcv
    try:
        bars = await fetch_ohlcv("BTCUSDT", "1h", 24 * 31)
    except Exception:
        return {"activity": None}
    if len(bars) < 24 * 10:
        return {"activity": None}
    ranges = []
    for i in range(24, len(bars) + 1):
        w = bars[i - 24:i]
        ranges.append((max(b["high"] for b in w) - min(b["low"] for b in w)) / w[-1]["close"])
    now, past = ranges[-1], sorted(ranges[:-1])
    median = past[len(past) // 2]
    ratio = now / median if median else None
    state = None if ratio is None else "quiet" if ratio < QUIET_X else "volatile" if ratio > BUSY_X else "normal"
    return {"activity": state, "btc_range_24h_pct": round(now * 100, 2), "btc_range_median_pct": round(median * 100, 2),
            "activity_ratio": round(ratio, 2) if ratio else None}


async def current() -> dict | None:
    """The regime right now, or None if BTC data is unavailable (never blocks a trade)."""
    if time.time() - _cache["at"] < TTL and _cache["value"]:
        return _cache["value"]
    try:
        from autotrader.engine import btc_vol_pct
        from data.binance_client import fetch_ohlcv
        daily = await fetch_ohlcv("BTCUSDT", "1d", 30)
        closes = [c["close"] for c in daily]
        sma20 = sum(closes[-20:]) / 20
        trend = "up" if closes[-1] >= sma20 else "down"
        vol = await btc_vol_pct()
        value = {"btc_trend": trend, "btc_vol_30d": round(vol, 1) if vol is not None else None,
                 "btc_7d_pct": round((closes[-1] / closes[-8] - 1) * 100, 2) if len(closes) >= 8 else None,
                 "label": label(trend, vol), **await _activity()}
    except Exception as e:
        logger.warning("Regime unavailable (%s)", e)
        return _cache["value"]
    _cache.update(at=time.time(), value=value)
    return value
