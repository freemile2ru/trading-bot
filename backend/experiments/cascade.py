"""Cascade fade — after a liquidation cascade, wait an hour, then take the other side for four hours (paper).

Why (2026-10-27): on the recorder's last 30 days (946k liquidations, 12 coins), buying the flush at once
LOST (−24 bps at 15 min, −23 at 1h: the cascade keeps going), but a fade held 4 hours made +34 bps a trade
after costs, t +2.5, 61% winners, positive in both halves (199 cascades). Thin evidence — 30 days, 12 coins —
so it runs forward on paper, judged by the usual pre-set verdict. The literature says cascades overshoot and
revert; what it doesn't say is when, which is the part this tests.

Rule change (2026-10-30, research/cascade_lab.py): the first rule — enter the minute after the burst, 3% stop — was
replayed on the recorder's own minute bars and lost in BOTH halves (−25 bps a trade, 46% winners; the 3% stop closed a
quarter of the legs while the cascade was still running). Entering 60 minutes after the burst, with only a wide
disaster stop, made +52 bps a trade (57% winners, t 3.4) — but its first half was flat (+1 bps) and three days carry
the gain, so it is a NEW forward test, not a proven edge: the verdict counts only trades opened under this rule.

The trade (paper wallet books.WALLETS["cascade"]):
  cascade   a completed 5-minute bucket whose liquidation notional on a coin is ≥ its 99th percentile over the
            trailing 30 days (the recorder's liquidation stream, scalper_market.db)
  entry     ENTRY_DELAY_MIN minutes after the bucket closes, at market: LONG if mostly longs were liquidated (price
            was flushed down), SHORT if shorts were squeezed; one entry per coin per hour; NOTIONAL_PCT of equity
  exit      HOLD_HOURS after entry (expires_at) or a STOP_PCT disaster stop; nothing else
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
STOP_PCT = float(os.getenv("CASCADE_STOP_PCT", "8"))             # a disaster stop only (was 3): the lab's 3% stop fired on the continuation
ENTRY_DELAY_MIN = float(os.getenv("CASCADE_ENTRY_DELAY_MIN", "60"))   # the cascade keeps going for about an hour; fade after it
RULE_SINCE = 1793372000.0              # 2026-10-30 14:53 UTC: trades opened before this ran the old rule and don't count for the verdict
PENDING_KEY = kv.key("cascade", "pending")
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


def pending() -> list[dict]:
    return list(kv.get_sync(PENDING_KEY) or [])


async def tick(now: float | None = None) -> list[dict]:
    """Every minute: note new cascades as pending fades (to fire ENTRY_DELAY_MIN later), then fire the ones that are due."""
    from experiments import books
    now = now or time.time()
    if not books.ENABLED or not book_switches.enabled(BOOK):
        return []
    queue = pending()
    start, buckets = last_bucket(now)
    if buckets and not kv.get_sync(kv.key("cascade", "bucket_done", str(start))):
        kv.set_sync(kv.key("cascade", "bucket_done", str(start)), 1, 3600)
        thr = thresholds(now)
        queued = {q["symbol"] for q in queue}
        for sym, (longs, shorts) in buckets.items():
            if sym not in thr or longs + shorts <= thr[sym] or sym in queued or kv.get_sync(kv.key("cascade", "cd", sym)):
                continue
            kv.set_sync(kv.key("cascade", "cd", sym), 1, COOLDOWN_S)
            queue.append({"symbol": sym, "direction": "long" if longs > shorts else "short", "longs": round(longs), "shorts": round(shorts),
                          "threshold": round(thr[sym]), "bucket": start, "fire_at": start + BUCKET_S + ENTRY_DELAY_MIN * 60})
            logger.info("Cascade on %s: $%.0f liquidated — fade queued for %+.0f min", sym, longs + shorts, ENTRY_DELAY_MIN)
    opened, keep = [], []
    w = books.WALLETS[BOOK]
    held = {t["symbol"] for t in w.get_portfolio()["open_trades"]}
    for q in queue:
        if now < q["fire_at"]:
            keep.append(q)
            continue
        if now - q["fire_at"] > 600 or q["symbol"] in held or len(held) + len(opened) >= MAX_OPEN:
            continue                                                   # missed by more than 10 minutes, already held, or the book is full
        price = await monitor.live_price(q["symbol"])
        if not price:
            keep.append(q)
            continue
        s = 1 if q["direction"] == "long" else -1
        equity = w.get_portfolio()["total_equity"] or 1
        t = w.open_trade(symbol=q["symbol"], direction=q["direction"], entry_price=price, stop_loss=price * (1 - s * STOP_PCT / 100),
                         take_profit_1=price * (1 + s * 0.5), take_profit_2=price * (1 + s * 0.9),
                         notional_usd=equity * NOTIONAL_PCT / 100, timeframe="4h", oracle_confidence=0,
                         oracle_reasoning=f"cascade fade: ${q['longs']:,.0f} longs / ${q['shorts']:,.0f} shorts liquidated in 5 min "
                                          f"(threshold ${q['threshold']:,.0f}), entered {ENTRY_DELAY_MIN:g} min later",
                         hold_duration=f"{HOLD_HOURS:g}h",
                         extra={"strategy": BOOK, "exit_policy": "manual", "shadows": False, "claude_verdict": "skipped",
                                "entry_type": "market", "cascade_notional": q["longs"] + q["shorts"], "cascade_bucket": q["bucket"],
                                "entry_delay_min": ENTRY_DELAY_MIN, "rule": "v2"})
        if t.get("error"):
            logger.info("Cascade %s %s refused: %s", q["direction"], q["symbol"], t["error"])
            continue
        w.annotate(t["id"], expires_at=now + HOLD_HOURS * 3600)
        opened.append(t)
        logger.info("Cascade fade: %s %s @ %s, %g min after $%.0f liquidated", q["direction"], q["symbol"], price, ENTRY_DELAY_MIN, q["longs"] + q["shorts"])
    if keep or queue:
        kv.set_sync(PENDING_KEY, keep, 86400)
    return opened


def config() -> dict:
    return {"hold_hours": HOLD_HOURS, "notional_pct": NOTIONAL_PCT, "stop_pct": STOP_PCT, "max_open": MAX_OPEN,
            "entry_delay_min": ENTRY_DELAY_MIN, "pending": pending(),
            "quantile": QUANTILE, "symbols": list(C.SYMBOLS)}
