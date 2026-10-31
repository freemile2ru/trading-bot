"""Thesis-faded exit — close a running trade once the scanner has stopped believing in it.

From research/thesis_exit_lab.py (2026-10-19): on 206 replayed decisions, closing a trade 2h+ old
whose coin's conviction had fallen below 0.10 (in the trade's direction) on two readings in a row
beat holding it under protect by +0.084R a trade — +0.08 on the older half, +0.09 on the newer, 90%
range +0.03 to +0.14R. A placebo (the same trades closed at a random later time) got +0.05R, so
most of the gain is getting out of trades that are going nowhere, which the fading flags well.

Live on every crypto swing book (autotrader paper and live ledger, its rules twin, the signal
books, Claude swing) from 2026-10-19; manual trades, Hold-72 and carry are left alone.

Each fade close leaves a "fade_twin" in the wallet's shadow book: an exact copy of the position at
the moment it was closed, carrying on under its exit policy as if nothing happened. report() pairs
every fade close with its twin, so the rule is measured live, not assumed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger("trading.thesis_exit")

ENABLED = os.getenv("THESIS_EXIT_ENABLED", "true").lower() != "false"
FADE_BELOW = float(os.getenv("THESIS_EXIT_FADE_BELOW", "0.10"))
MIN_AGE_H = float(os.getenv("THESIS_EXIT_MIN_AGE_H", "2"))
FRESH_S = 1800                 # readings older than this mean the scanner isn't watching — do nothing
BOOKS = {"auto", "auto_live", "auto_rules", "signals_claude", "signals_rules", "claude_swing"}   # not the cascade/weekly books
REASON = "thesis_faded"
TWIN = "fade_twin"


def _latest_readings(symbol: str, since: float, n: int = 2) -> list[tuple[float, float]]:
    from storage import signals
    return signals.latest_convictions(symbol, since, n)


def fade_note(trade: dict, now: float | None = None) -> str | None:
    """Why this trade's thesis is gone, or None. Both of the coin's last two readings must be at least
    MIN_AGE_H into the trade, recent, and below FADE_BELOW in the trade's direction."""
    now = now or time.time()
    opened = trade.get("opened_ts")
    if not opened or now - opened < MIN_AGE_H * 3600:
        return None
    reads = _latest_readings(trade["symbol"], opened + MIN_AGE_H * 3600)
    if len(reads) < 2 or now - reads[0][0] > FRESH_S:
        return None
    s = 1 if trade["direction"] == "long" else -1
    signed = [c * s for _, c in reads]
    if all(c < FADE_BELOW for c in signed):
        return f"thesis faded — conviction {signed[1]:+.2f} then {signed[0]:+.2f} in the trade's direction, {(now - opened) / 3600:.1f}h in"
    return None


async def check(wallet) -> list[dict]:
    """Close the wallet's faded trades. Returns monitor-style "closed" events for the book to handle."""
    if not ENABLED or wallet.label not in BOOKS:
        return []
    from trading import monitor
    events = []
    for t in wallet.get_portfolio()["open_trades"]:
        if t.get("event_id") or t.get("unlock_id") or t.get("weekly_id") or t.get("lane") not in (None, "ta"):
            continue                                    # a mirrored book position: that book decides, not the scanner
        try:
            note = await asyncio.to_thread(fade_note, t)
        except Exception as e:
            logger.debug("Thesis check failed for %s: %s", t["symbol"], e)
            continue
        if not note:
            continue
        price = await monitor.live_price(t["symbol"])
        if not price:
            continue
        snapshot = dict(t)
        closed = await asyncio.to_thread(wallet.close_trade_manually, t["id"], price, REASON)
        if not closed:
            continue
        await asyncio.to_thread(wallet.add_twin, snapshot, TWIN)
        closed["exit_note"] = note
        logger.info("%s: closed %s %s — %s", wallet.label, t["direction"], t["symbol"], note)
        events.append({"type": "closed", "trade": closed})
    return events


def report() -> dict:
    """Every fade close against its twin (the same position, held on)."""
    from trading import guards
    pairs, pending = [], 0
    for book, w in guards._wallets().items():
        if book not in BOOKS:
            continue
        twins = {r["trade_id"]: r for r in w.shadow_report()["results"] if r.get("claude_verdict") == TWIN}
        for t in w.all_trades()["closed"]:
            if t.get("close_reason") != REASON or not t.get("risk_usd"):
                continue
            tw = twins.get(t["id"])
            if tw is None:
                pending += 1
                continue
            pairs.append({"book": book, "symbol": t["symbol"], "r": round(t["pnl_usd"] / t["risk_usd"], 3), "twin_r": tw["r"]})
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    return {"enabled": ENABLED, "fade_below": FADE_BELOW, "min_age_h": MIN_AGE_H, "closed": len(pairs) + pending,
            "twins_running": pending, "pairs": len(pairs), "avg_r": mean([p["r"] for p in pairs]),
            "twin_avg_r": mean([p["twin_r"] for p in pairs]),
            "saved_r": mean([p["r"] - p["twin_r"] for p in pairs]), "recent": pairs[-10:]}
