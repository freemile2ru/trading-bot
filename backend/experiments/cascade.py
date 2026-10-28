"""Cascade fade — after a liquidation cascade, take the other side for four hours (paper).

Why (2026-10-27): on the recorder's last 30 days (946k liquidations, 12 coins), buying the flush at once
LOST (−24 bps at 15 min, −23 at 1h: the cascade keeps going), but a fade held 4 hours made +34 bps a trade
after costs, t +2.5, 61% winners, positive in both halves (199 cascades). Thin evidence — 30 days, 12 coins —
so it runs forward on paper, judged by the usual pre-set verdict. The literature says cascades overshoot and
revert; what it doesn't say is when, which is the part this tests.

The trade (paper wallet books.WALLETS["cascade"]):
  cascade   a completed 5-minute bucket whose liquidation notional on a coin is ≥ its 99th percentile over the
            trailing 30 days (the recorder's liquidation stream, scalper_market.db)
  entry     at market the minute after the bucket closes: LONG if mostly longs were liquidated (price was
            flushed down), SHORT if shorts were squeezed; one entry per coin per hour; NOTIONAL_PCT of equity
  exit      HOLD_HOURS later (expires_at) or a STOP_PCT disaster stop; nothing else
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

from scalper import config as C
from storage import state as kv
from trading import book_switches, monitor

logger = logging.getLogger("experiments.cascade")

BOOK = "cascade"
HOLD_HOURS = float(os.getenv("CASCADE_HOLD_HOURS", "4"))
NOTIONAL_PCT = float(os.getenv("CASCADE_NOTIONAL_PCT", "10"))
STOP_PCT = float(os.getenv("CASCADE_STOP_PCT", "3"))
MAX_OPEN = int(os.getenv("CASCADE_MAX_OPEN", "6"))
QUANTILE = 0.99
BUCKET_S = 300
LOOKBACK_DAYS = 30
COOLDOWN_S = 3600
STALE_STREAM_S = 600
_thresholds: dict = {"at": 0.0, "by": {}}


def _con() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{C.MARKET_DB}?mode=ro", uri=True, timeout=5)


def thresholds(now: float) -> dict[str, float]:
    """Each recorder coin's 99th-percentile 5-minute liquidation notional over the trailing 30 days (hourly cache)."""
    if now - _thresholds["at"] < 3600 and _thresholds["by"]:
        return _thresholds["by"]
    by = {}
    con = _con()
    try:
        for sym in C.SYMBOLS:
            rows = con.execute("SELECT CAST(ts / ? AS INTEGER) AS b, SUM(notional) FROM liquidations WHERE symbol=? AND ts >= ? GROUP BY b",
                               (BUCKET_S, sym, now - LOOKBACK_DAYS * 86400)).fetchall()
            totals = sorted(r[1] for r in rows)
            if len(totals) >= 200:
                by[sym] = totals[int(QUANTILE * len(totals)) - 1]
    finally:
        con.close()
    _thresholds.update(at=now, by=by)
    return by


def last_bucket(now: float) -> tuple[float, dict[str, tuple[float, float]]]:
    """(bucket start, {symbol: (longs liquidated $, shorts liquidated $)}) for the last COMPLETED 5-minute bucket."""
    start = (int(now) // BUCKET_S - 1) * BUCKET_S
    out: dict[str, tuple[float, float]] = {}
    con = _con()
    try:
        newest = con.execute("SELECT MAX(ts) FROM liquidations").fetchone()[0] or 0
        if now - newest > STALE_STREAM_S:
            return start, {}                                     # the recorder isn't hearing the stream: do nothing
        for sym, side, tot in con.execute("SELECT symbol, side, SUM(notional) FROM liquidations WHERE ts >= ? AND ts < ? GROUP BY symbol, side",
                                          (start, start + BUCKET_S)):
            longs, shorts = out.get(sym, (0.0, 0.0))
            # a liquidated LONG is closed with a SELL order, a liquidated SHORT with a BUY
            out[sym] = (longs + tot, shorts) if side == "SELL" else (longs, shorts + tot)
    finally:
        con.close()
    return start, out


async def tick(now: float | None = None) -> list[dict]:
    from experiments import books
    now = now or time.time()
    if not books.ENABLED or not book_switches.enabled(BOOK):
        return []
    start, buckets = last_bucket(now)
    if not buckets or kv.get_sync(kv.key("cascade", "bucket_done", str(start))):
        return []
    kv.set_sync(kv.key("cascade", "bucket_done", str(start)), 1, 3600)
    thr = thresholds(now)
    w = books.WALLETS[BOOK]
    held = {t["symbol"] for t in w.get_portfolio()["open_trades"]}
    opened = []
    for sym, (longs, shorts) in buckets.items():
        if sym not in thr or longs + shorts <= thr[sym] or sym in held or len(held) + len(opened) >= MAX_OPEN:
            continue
        if kv.get_sync(kv.key("cascade", "cd", sym)):
            continue
        price = await monitor.live_price(sym)
        if not price:
            continue
        direction = "long" if longs > shorts else "short"
        s = 1 if direction == "long" else -1
        equity = w.get_portfolio()["total_equity"] or 1
        t = w.open_trade(symbol=sym, direction=direction, entry_price=price, stop_loss=price * (1 - s * STOP_PCT / 100),
                         take_profit_1=price * (1 + s * 0.5), take_profit_2=price * (1 + s * 0.9),
                         notional_usd=equity * NOTIONAL_PCT / 100, timeframe="4h", oracle_confidence=0,
                         oracle_reasoning=f"cascade fade: ${longs:,.0f} longs / ${shorts:,.0f} shorts liquidated in 5 min (threshold ${thr[sym]:,.0f})",
                         hold_duration=f"{HOLD_HOURS:g}h",
                         extra={"strategy": BOOK, "exit_policy": "manual", "shadows": False, "claude_verdict": "skipped",
                                "entry_type": "market", "cascade_notional": round(longs + shorts), "cascade_bucket": start})
        if t.get("error"):
            logger.info("Cascade %s %s refused: %s", direction, sym, t["error"])
            continue
        w.annotate(t["id"], expires_at=now + HOLD_HOURS * 3600)
        kv.set_sync(kv.key("cascade", "cd", sym), 1, COOLDOWN_S)
        opened.append(t)
        logger.info("Cascade fade: %s %s @ %s after $%.0f liquidated", direction, sym, price, longs + shorts)
    return opened


def config() -> dict:
    return {"hold_hours": HOLD_HOURS, "notional_pct": NOTIONAL_PCT, "stop_pct": STOP_PCT, "max_open": MAX_OPEN,
            "quantile": QUANTILE, "symbols": list(C.SYMBOLS)}
