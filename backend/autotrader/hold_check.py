"""Is a reversal leg still worth holding? (2026-11-09, replacing the 24-hour expiry the user asked to drop.)

The lab measured plain 24-hour returns, so the lane left every leg at the bell — and the bell gave the peaks back:
of 42 closed legs, the 29 that expired as losers had peaked at +1–3% of notional, and the expired winners kept 10–48%
of theirs. Instead of a clock, every monitor pass asks whether the thesis still stands. The stop and the profit lock
still act first (trading/exits.py); this decides only the exits a clock used to make. Pre-registered rules:

  grace        the first GRACE_H hours are the trade's: nothing but the stop acts.
  flipped      the coin now ranks on the other side of the reversal score (a short's coin scoring above zero): the
               reason for the leg is gone — close.
  reverted     the leg is in profit and the price has come back through the 24-hour VWAP in its favour: the mean
               reversion it was bought for has happened — bank it.
  horizon      past HORIZON_H (the lab's horizon), hold only while the leg is in profit AND the last FOLLOW_H hours
               moved its way; otherwise the edge is spent — close. HARD_CAP_H ends anything.

Each close carries the rule that fired, so the Books tab can judge the rules separately.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("autotrader.hold_check")

GRACE_H = float(os.getenv("AUTOTRADER_HOLD_GRACE_H", "4"))
HORIZON_H = float(os.getenv("AUTOTRADER_HOLD_HORIZON_H", "24"))
HARD_CAP_H = float(os.getenv("AUTOTRADER_HOLD_CAP_H", "72"))
FOLLOW_H = 4
CACHE_S = 300
_tape: dict[str, dict] = {}


async def tape(symbol: str, now: float | None = None) -> dict | None:
    """{price, vwap24, move_4h_pct} on 5-minute perpetual bars; None when the feed is down. Cached CACHE_S."""
    now = now or time.time()
    c = _tape.get(symbol)
    if c and now - c["at"] < CACHE_S:
        return c["data"]
    try:
        from data.binance_client import fetch_futures_klines
        bars = await fetch_futures_klines(symbol, "5m", 288)
        if len(bars) < 60:
            return None
        vol = sum(b.get("volume") or 0 for b in bars)
        vwap = sum((b["high"] + b["low"] + b["close"]) / 3 * (b.get("volume") or 0) for b in bars) / vol if vol else None
        price = bars[-1]["close"]
        ref = bars[-(FOLLOW_H * 12) - 1]["close"]
        data = {"price": price, "vwap24": vwap, "move_4h_pct": round((price / ref - 1) * 100, 3) if ref else None, "at": now}
        _tape[symbol] = {"at": now, "data": data}
        return data
    except Exception as e:
        logger.warning("Hold-check tape for %s unavailable: %s", symbol, e)
        return None


def verdict(trade: dict, now: float, score: float | None, t: dict | None) -> tuple[bool, str, str]:
    """(hold, why, rule). `score` is the coin's current reversal score if the sweep ranked it; `t` the tape or None."""
    s = 1 if trade["direction"] == "long" else -1
    held_h = (now - (trade.get("opened_ts") or now)) / 3600
    pnl = trade.get("pnl_usd") or 0.0
    if held_h < GRACE_H:
        return True, f"grace: {held_h:.1f}h of {GRACE_H:g}h", "grace"
    if score is not None and score * s < 0:                       # a short's coin was sold at the bottom of the score: above zero it ranks with the longs
        return False, f"ranks on the other side now (score {score:+.3f})", "flipped"
    if t and t.get("vwap24") and pnl > 0 and (t["price"] - t["vwap24"]) * s >= 0:
        return False, f"reverted to the 24h VWAP ({t['vwap24']:.4g}) with ${pnl:,.0f}", "reverted"
    if held_h >= HARD_CAP_H:
        return False, f"{HARD_CAP_H:g}h cap", "cap"
    if held_h >= HORIZON_H:
        move = (t or {}).get("move_4h_pct")
        if pnl > 0 and move is not None and move * s > 0:
            return True, f"past {HORIZON_H:g}h, +${pnl:,.0f} and the last {FOLLOW_H}h moved {move:+.2f}% its way", "running"
        if pnl > 0:
            return False, f"past {HORIZON_H:g}h with +${pnl:,.0f} and no follow-through ({move:+.2f}% in {FOLLOW_H}h)" if move is not None else f"past {HORIZON_H:g}h with +${pnl:,.0f}, tape unknown", "horizon"
        return False, f"past {HORIZON_H:g}h and underwater (${pnl:,.0f})", "horizon"
    return True, f"holding: {held_h:.1f}h, ${pnl:,.0f}", "hold"


def describe() -> dict:
    return {"grace_h": GRACE_H, "horizon_h": HORIZON_H, "hard_cap_h": HARD_CAP_H, "follow_h": FOLLOW_H,
            "rule": f"No clock: after {GRACE_H:g}h a reversal leg is closed when its coin ranks on the other side of the score, when it is in "
                    f"profit back through the 24h VWAP, or past {HORIZON_H:g}h unless it is in profit with the last {FOLLOW_H}h moving its way; "
                    f"{HARD_CAP_H:g}h ends anything. The stop and the profit lock act first."}
