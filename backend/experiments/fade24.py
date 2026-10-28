"""Fade-24 — trade AGAINST the scanner's conviction for 24 hours, hedged against BTC (paper).

Why (2026-10-24): on 21 days of live readings (research/signal_scores.py) the scanner's conviction had
an IC of −0.26 against the next 24h return (t −5.1) — it behaves like a 24h momentum reading, and 24h
moves have been reversing. Fading |conviction| ≥ 0.30 against BTC for 24h made about +127 bps per pair
after costs over the last 15 days (108 entries, 9 of 15 days positive). Over the year-long replay
(research/hold_lab.py) the same fade LOST about 30 bps a trade — so this is a bet that the current
market keeps behaving this way, tested on paper and judged by the usual pre-set verdict.

The trade (one pair per coin at a time):
  entry   after a general sweep, any coin with |conviction| ≥ MIN_CONVICTION (not BTC): the coin perp
          the OPPOSITE way to its conviction + BTC perp the other way, equal notional
  exit    after HOLD_HOURS, or if the pair is down DISASTER_PCT (checked every minute)
  costs   taker fee + slippage on all four fills; real funding on both legs, settled at close
  size    LEG_PCT of equity per leg, at most MAX_PAIRS open and MAX_PER_SIDE facing one way
Its own ledger (swing_trading.db, table fade24). R is measured against the disaster stop.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from storage.db import SWING_DB, connect
from trading import book_switches, futures, monitor

logger = logging.getLogger("experiments.fade24")

BOOK = "fade24"
LABEL = "Fade the scanner (24h vs BTC)"
MIN_CONVICTION = float(os.getenv("FADE24_MIN_CONVICTION", "0.30"))
HOLD_HOURS = float(os.getenv("FADE24_HOURS", "24"))
LEG_PCT = float(os.getenv("FADE24_LEG_PCT", "10"))
MAX_PAIRS = int(os.getenv("FADE24_MAX_PAIRS", "8"))
MAX_PER_SIDE = int(os.getenv("FADE24_MAX_PER_SIDE", "4"))
DISASTER_PCT = float(os.getenv("FADE24_DISASTER_PCT", "12"))
INITIAL_BALANCE = 10_000.0
HEDGE = "BTCUSDT"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fade24 (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL, conviction REAL,
    opened_ts REAL NOT NULL, coin_entry REAL NOT NULL, btc_entry REAL NOT NULL, notional REAL NOT NULL,
    status TEXT NOT NULL, closed_ts REAL, coin_exit REAL, btc_exit REAL, close_reason TEXT,
    fees REAL DEFAULT 0, funding REAL DEFAULT 0, gross REAL, net REAL,
    best_pct REAL DEFAULT 0, worst_pct REAL DEFAULT 0, mark_pct REAL DEFAULT 0, data TEXT
)"""

_lock = threading.Lock()
_con = None
_path = SWING_DB


def use(path: str) -> None:
    """Point at another database (tests)."""
    global _con, _path
    with _lock:
        _con, _path = None, path


def _db():
    global _con
    if _con is None:
        _con = connect(_path)
        _con.execute(SCHEMA)
    return _con


def _rows(sql: str, params=()) -> list[dict]:
    with _lock:
        cur = _db().execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _write(sql: str, params=()) -> None:
    with _lock:
        _db().execute(sql, params)
        _db().commit()


def open_pairs() -> list[dict]:
    return _rows("SELECT * FROM fade24 WHERE status='open' ORDER BY opened_ts")


def closed_pairs() -> list[dict]:
    return _rows("SELECT * FROM fade24 WHERE status='closed' ORDER BY closed_ts")


def _s(direction: str) -> int:
    return 1 if direction == "long" else -1


def spread_pct(p: dict, coin_px: float, btc_px: float) -> float:
    """The pair's move in its direction, % of notional: the coin leg minus the BTC leg."""
    s = _s(p["direction"])
    return (s * (coin_px / p["coin_entry"] - 1) - s * (btc_px / p["btc_entry"] - 1)) * 100


def fill(price: float, direction: str, opening: bool) -> float:
    buying = (direction == "long") == opening
    return price * (1 + futures.SLIPPAGE) if buying else price * (1 - futures.SLIPPAGE)


def balance() -> float:
    return INITIAL_BALANCE + sum(p["net"] or 0 for p in closed_pairs())


def equity() -> float:
    return balance() + sum((p["mark_pct"] or 0) / 100 * p["notional"] for p in open_pairs())


async def on_scans(scans: list[dict], now: float | None = None) -> list[dict]:
    """After a general sweep: open a faded pair for each new coin with strong conviction."""
    from experiments import books
    if not books.ENABLED or not book_switches.enabled(BOOK) or not scans:
        return []
    now = now or time.time()
    pairs = open_pairs()
    held = {p["symbol"] for p in pairs}
    side = {d: sum(p["direction"] == d for p in pairs) for d in ("long", "short")}
    picks = []
    for sc in sorted((s for s in scans if s["symbol"] != HEDGE and s["symbol"] not in held
                      and abs(s.get("conviction") or 0) >= MIN_CONVICTION and not s.get("held_only")),
                     key=lambda s: -abs(s["conviction"])):
        d = "short" if sc["conviction"] > 0 else "long"            # the fade
        if len(held) + len(picks) >= MAX_PAIRS or side[d] >= MAX_PER_SIDE:
            continue
        side[d] += 1
        picks.append((sc, d))
    if not picks:
        return []
    btc = await monitor.live_price(HEDGE)
    if not btc:
        return []
    opened = []
    for sc, direction in picks:
        price = await monitor.live_price(sc["symbol"]) or sc.get("price")
        if not price:
            continue
        notional = round(equity() * LEG_PCT / 100, 2)
        pair = {
            "id": uuid.uuid4().hex[:12], "symbol": sc["symbol"], "direction": direction, "conviction": sc["conviction"],
            "opened_ts": now, "coin_entry": fill(price, direction, True),
            "btc_entry": fill(btc, "short" if direction == "long" else "long", True),
            "notional": notional, "status": "open", "fees": round(2 * notional * futures.TAKER_FEE, 4),
            "data": json.dumps({"setups": (sc.get("conviction_detail") or {}).get("aligned_setups") or [],
                                "regime": (sc.get("regime_at_scan") or {}).get("label")}),
        }
        _write("INSERT INTO fade24 (id, symbol, direction, conviction, opened_ts, coin_entry, btc_entry, notional, "
               "status, fees, data) VALUES (:id,:symbol,:direction,:conviction,:opened_ts,:coin_entry,:btc_entry,"
               ":notional,:status,:fees,:data)", pair)
        logger.info("Fade-24 opened %s %s vs BTC (fading conviction %+.2f, $%.0f a leg)", direction, sc["symbol"],
                    sc["conviction"], notional)
        opened.append(pair)
    return opened


async def _funding(p: dict, until: float) -> float:
    from data.binance_client import fetch_funding_history
    s, start, paid = _s(p["direction"]), int(p["opened_ts"] * 1000), 0.0
    for symbol, side in ((p["symbol"], s), (HEDGE, -s)):
        try:
            for f in await fetch_funding_history(symbol, start):
                if f["time"] <= until * 1000:
                    paid += f["rate"] * p["notional"] * side
        except Exception as e:
            logger.debug("Funding for %s unavailable: %s", symbol, e)
    return round(paid, 4)


async def close(p: dict, coin_px: float, btc_px: float, reason: str, now: float | None = None) -> dict:
    now = now or time.time()
    coin_exit = fill(coin_px, p["direction"], False)
    btc_exit = fill(btc_px, "short" if p["direction"] == "long" else "long", False)
    gross = round(spread_pct(p, coin_exit, btc_exit) / 100 * p["notional"], 4)
    fees = round((p["fees"] or 0) + 2 * p["notional"] * futures.TAKER_FEE, 4)
    funding = await _funding(p, now)
    net = round(gross - fees - funding, 4)
    _write("UPDATE fade24 SET status='closed', closed_ts=?, coin_exit=?, btc_exit=?, close_reason=?, fees=?, "
           "funding=?, gross=?, net=? WHERE id=?", (now, coin_exit, btc_exit, reason, fees, funding, gross, net, p["id"]))
    logger.info("Fade-24 closed %s %s — %s, net $%.2f", p["direction"], p["symbol"], reason, net)
    return {**p, "status": "closed", "closed_ts": now, "close_reason": reason, "gross": gross, "fees": fees,
            "funding": funding, "net": net}


async def monitor_pairs(now: float | None = None) -> list[dict]:
    """Every minute: mark each pair; close it after HOLD_HOURS or at the disaster stop."""
    pairs = open_pairs()
    if not pairs:
        return []
    now = now or time.time()
    btc = await monitor.live_price(HEDGE)
    if not btc:
        return []
    closed = []
    for p in pairs:
        px = await monitor.live_price(p["symbol"])
        if not px:
            continue
        mark = spread_pct(p, px, btc)
        _write("UPDATE fade24 SET mark_pct=?, best_pct=MAX(best_pct, ?), worst_pct=MIN(worst_pct, ?) WHERE id=?",
               (round(mark, 4), round(mark, 4), round(mark, 4), p["id"]))
        if mark <= -DISASTER_PCT:
            closed.append(await close(p, px, btc, "disaster_stop", now))
        elif now - p["opened_ts"] >= HOLD_HOURS * 3600:
            closed.append(await close(p, px, btc, "hold_complete", now))
    return closed


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def rows() -> list[dict]:
    """Pairs in the shape the Trades page, Books and verdicts read (overview.all_rows)."""
    out, now = [], time.time()
    for p in _rows("SELECT * FROM fade24"):
        is_open = p["status"] == "open"
        risk = p["notional"] * DISASTER_PCT / 100
        net = (p["mark_pct"] or 0) / 100 * p["notional"] - (p["fees"] or 0) if is_open else (p["net"] or 0)
        end = now if is_open else p["closed_ts"]
        out.append({
            "id": p["id"], "system": BOOK, "symbol": p["symbol"], "direction": p["direction"],
            "status": "open" if is_open else "closed", "opened_ts": p["opened_ts"],
            "closed_ts": None if is_open else p["closed_ts"], "hold_min": round((end - p["opened_ts"]) / 60, 1),
            "entry_price": p["coin_entry"], "exit_price": None if is_open else p["coin_exit"], "mark_price": None,
            "stop_loss": None, "take_profit": None, "notional": round(p["notional"], 2), "leverage": None,
            "margin": None, "risk_usd": round(risk, 2),
            "gross_pnl": round(p["gross"] if not is_open else net + (p["fees"] or 0), 2),
            "fees": round(p["fees"] or 0, 2), "funding": round(p["funding"] or 0, 2), "net_pnl": round(net, 2),
            "r": round(net / risk, 2) if risk else None,
            "exit_reason": None if is_open else p["close_reason"],
            "outcome": None if is_open else ("win" if net > 0 else "loss"),
            "timeframe": f"{HOLD_HOURS:g}h fade",
            "note": f"{p['direction']} {p['symbol']} vs {'short' if p['direction'] == 'long' else 'long'} BTC, "
                    f"fading conviction {p['conviction']:+.2f}",
            "mfe_r": round((p["best_pct"] or 0) / DISASTER_PCT, 2), "quantity": None, "liquidation": None, "tp_set": False,
            "opened_at": _iso(p["opened_ts"]),
        })
    return out


def config() -> dict:
    return {"min_conviction": MIN_CONVICTION, "hold_hours": HOLD_HOURS, "leg_pct": LEG_PCT, "max_pairs": MAX_PAIRS,
            "max_per_side": MAX_PER_SIDE, "disaster_pct": DISASTER_PCT, "hedge": HEDGE, "initial_balance": INITIAL_BALANCE}
