"""Hold-72 — follow high-conviction scanner signals for three days, hedged against BTC.

Why: a year of replayed scans (87,520, research/setup_lab.py cache; 2026-10-06 analysis) said
the scanner's direction is informative over ~72 h — conviction ≥ 0.45 beat the average coin by
~+115 bps/day after costs, train +117 / test +112, 4 of 5 quarters positive — while the live
books, with ~1.5-ATR stops and trailing exits, are stopped out by noise within hours. That
result did NOT pass the lab's rule (95% CI −39…+215 bps/day includes zero). This book tests it
forward, on paper, without touching any other system.

The trade (one "pair" per coin at a time):
  entry   after a general sweep, any coin with |conviction| ≥ MIN_CONVICTION (not BTC itself):
          coin perpetual in the conviction's direction + BTC perpetual the opposite way, equal
          notional — a bet on the coin beating BTC, not on the market going up
  exit    after HOLD_HOURS (24 since 2026-10-23, was 72), if the pair is down DISASTER_PCT, or once the
          coin's conviction has faded below WATCH_BELOW in the pair's direction (checked every minute)
  costs   taker fee + slippage on all four fills; real funding on both legs, settled at close
  size    LEG_PCT of equity per leg, at most MAX_PAIRS pairs open and MAX_PER_SIDE facing one way

R is measured against the disaster stop (notional × DISASTER_PCT), so −1R = stopped out.
Its own ledger (swing_trading.db, table hold72): a paper wallet holds one position per coin,
and every pair here needs its own BTC leg.
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

logger = logging.getLogger("experiments.hold72")

BOOK = "hold72"
LABEL = "Hold vs BTC (24h)"
MIN_CONVICTION = float(os.getenv("HOLD72_MIN_CONVICTION", "0.45"))
# 24h cap and viability watcher (2026-10-23). Replaying our 34 live pairs on real candles: held 72h they
# averaged −4.4% of notional; capped at 24h −1.6%; closed once the coin's conviction fell below 0.25 in
# the pair's direction on two readings in a row −0.5% (average loss −7.9% → −2.3%). The year's replay
# disagreed — there, holding 72h paid (+0.10% of equity a pair vs ~0 managed) — but that edge never passed
# its own test, and live the signal has pointed the wrong way (research/hold72_manage_lab.py,
# research/hold72_live_check.py). Still called Hold-72 for continuity; the clock is HOLD_HOURS.
HOLD_HOURS = float(os.getenv("HOLD72_HOURS", "24"))
WATCH_BELOW = float(os.getenv("HOLD72_WATCH_BELOW", "0.25"))
WATCH_FRESH_S = 1800                     # older readings mean the scanner isn't watching the coin: don't act
# Risk per pair and per cluster (2026-10-19, research/hold72_exits_lab.py): tighter stops, breakevens,
# trailing and take-profits all made the year's replay WORSE (they cut the 72h winners the edge lives on),
# and the 12% stop is about right. The heavy live losses were size and clustering — six alt longs opened
# within a day hit their stops together. Legs went 15% → 10% and at most MAX_PER_SIDE pairs face one way:
# on the replay the worst day went −8.3% → −3.5% of equity and max drawdown −29% → −18%, with return
# per unit of drawdown about unchanged (4.7 → 4.9).
LEG_PCT = float(os.getenv("HOLD72_LEG_PCT", "10"))
MAX_PAIRS = int(os.getenv("HOLD72_MAX_PAIRS", "10"))
MAX_PER_SIDE = int(os.getenv("HOLD72_MAX_PER_SIDE", "4"))
DISASTER_PCT = float(os.getenv("HOLD72_DISASTER_PCT", "12"))
INITIAL_BALANCE = 10_000.0
HEDGE = "BTCUSDT"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hold72 (
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
    return _rows("SELECT * FROM hold72 WHERE status='open' ORDER BY opened_ts")


def closed_pairs() -> list[dict]:
    return _rows("SELECT * FROM hold72 WHERE status='closed' ORDER BY closed_ts")


# ── Accounting (pure) ──────────────────────────────────────────────────────────

def _s(direction: str) -> int:
    return 1 if direction == "long" else -1


def spread_pct(p: dict, coin_px: float, btc_px: float) -> float:
    """The pair's move in its direction, % of notional: the coin leg minus the BTC leg."""
    s = _s(p["direction"])
    return (s * (coin_px / p["coin_entry"] - 1) - s * (btc_px / p["btc_entry"] - 1)) * 100


def fill(price: float, direction: str, opening: bool) -> float:
    """Taker fill with slippage: buying pays up, selling gets less."""
    buying = (direction == "long") == opening
    return price * (1 + futures.SLIPPAGE) if buying else price * (1 - futures.SLIPPAGE)


def balance() -> float:
    return INITIAL_BALANCE + sum(p["net"] or 0 for p in closed_pairs())


def equity() -> float:
    return balance() + sum((p["mark_pct"] or 0) / 100 * p["notional"] for p in open_pairs())


# ── Trading ────────────────────────────────────────────────────────────────────

async def on_scans(scans: list[dict], now: float | None = None) -> list[dict]:
    """After a general sweep: open a pair for each new high-conviction coin. Returns opened pairs."""
    from experiments import books
    if not books.ENABLED or not book_switches.enabled(BOOK) or not scans:
        return []
    now = now or time.time()
    pairs = open_pairs()
    held = {p["symbol"] for p in pairs}
    side = {d: sum(p["direction"] == d for p in pairs) for d in ("long", "short")}
    candidates = sorted((s for s in scans if s["symbol"] != HEDGE and s["symbol"] not in held
                         and abs(s.get("conviction") or 0) >= MIN_CONVICTION and not s.get("held_only")),
                        key=lambda s: -abs(s["conviction"]))
    picks = []
    for s in candidates:
        d = "long" if s["conviction"] > 0 else "short"
        if len(held) + len(picks) >= MAX_PAIRS or side[d] >= MAX_PER_SIDE:
            continue
        side[d] += 1
        picks.append(s)
    if not picks:
        return []
    btc = await monitor.live_price(HEDGE)
    if not btc:
        return []
    opened = []
    for scan in picks:
        price = await monitor.live_price(scan["symbol"]) or scan.get("price")
        if not price:
            continue
        direction = "long" if scan["conviction"] > 0 else "short"
        notional = round(equity() * LEG_PCT / 100, 2)
        pair = {
            "id": uuid.uuid4().hex[:12], "symbol": scan["symbol"], "direction": direction,
            "conviction": scan["conviction"], "opened_ts": now,
            "coin_entry": fill(price, direction, True),
            "btc_entry": fill(btc, "short" if direction == "long" else "long", True),
            "notional": notional, "status": "open",
            "fees": round(2 * notional * futures.TAKER_FEE, 4),            # two opening fills
            "data": json.dumps({"stability_4h": _stability(scan["symbol"], direction, now),
                                "setups": (scan.get("conviction_detail") or {}).get("aligned_setups") or [],
                                "regime": (scan.get("regime_at_scan") or {}).get("label"),
                                "confidence": scan.get("confidence")}),
        }
        _write("INSERT INTO hold72 (id, symbol, direction, conviction, opened_ts, coin_entry, btc_entry, notional, "
               "status, fees, data) VALUES (:id,:symbol,:direction,:conviction,:opened_ts,:coin_entry,:btc_entry,"
               ":notional,:status,:fees,:data)", pair)
        logger.info("Hold-72 opened %s %s vs BTC (conviction %+.2f, $%.0f a leg)", direction, scan["symbol"],
                    scan["conviction"], notional)
        opened.append(pair)
    return opened


async def _funding(p: dict, until: float) -> float:
    """Funding paid (+) or received (−) on both legs between open and close."""
    from data.binance_client import fetch_funding_history
    s, start = _s(p["direction"]), int(p["opened_ts"] * 1000)
    paid = 0.0
    for symbol, side in ((p["symbol"], s), (HEDGE, -s)):
        try:
            for f in await fetch_funding_history(symbol, start):
                if f["time"] <= until * 1000:
                    paid += f["rate"] * p["notional"] * side         # longs pay positive funding
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
    _write("UPDATE hold72 SET status='closed', closed_ts=?, coin_exit=?, btc_exit=?, close_reason=?, fees=?, "
           "funding=?, gross=?, net=? WHERE id=?", (now, coin_exit, btc_exit, reason, fees, funding, gross, net, p["id"]))
    logger.info("Hold-72 closed %s %s — %s, net $%.2f", p["direction"], p["symbol"], reason, net)
    return {**p, "status": "closed", "closed_ts": now, "close_reason": reason, "gross": gross, "fees": fees,
            "funding": funding, "net": net}


def _signed_readings(p: dict, since: float, n: int) -> list[tuple[float, float]]:
    from storage import signals
    s = _s(p["direction"])
    return [(ts, c * s) for ts, c in signals.latest_convictions(p["symbol"], since, n)]


def _faded(p: dict, now: float) -> bool:
    """The viability watcher: the coin's last two scanner readings since entry are both below WATCH_BELOW
    in the pair's direction (and recent — a silent scanner never closes anything)."""
    try:
        reads = _signed_readings(p, p["opened_ts"] + 1, 2)
    except Exception:
        return False
    return len(reads) == 2 and now - reads[0][0] <= WATCH_FRESH_S and all(c < WATCH_BELOW for _, c in reads)


def _stability(symbol: str, direction: str, now: float) -> float | None:
    """The weakest reading in the pair's direction over the 4h before entry — recorded, not yet a filter
    (live: 26 of 28 pairs entered on a sudden spike; the 2 steady ones did better, but 2 is no sample)."""
    try:
        from storage import signals
        rows = signals.latest_convictions(symbol, now - 4 * 3600, 200)
    except Exception:
        return None
    s = _s(direction)
    return round(min(c * s for _, c in rows), 4) if len(rows) >= 5 else None


async def monitor_pairs(now: float | None = None) -> list[dict]:
    """Every minute: mark each pair, close it at 72 h or at the disaster stop. Returns closed pairs."""
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
        _write("UPDATE hold72 SET mark_pct=?, best_pct=MAX(best_pct, ?), worst_pct=MIN(worst_pct, ?) WHERE id=?",
               (round(mark, 4), round(mark, 4), round(mark, 4), p["id"]))
        if mark <= -DISASTER_PCT:
            closed.append(await close(p, px, btc, "disaster_stop", now))
        elif now - p["opened_ts"] >= HOLD_HOURS * 3600:
            closed.append(await close(p, px, btc, "hold_complete", now))
        elif _faded(p, now):
            closed.append(await close(p, px, btc, "thesis_faded", now))
    return closed


# ── Reporting ──────────────────────────────────────────────────────────────────

def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def rows() -> list[dict]:
    """Pairs in the shape the Trades page, Lab and Books read (overview.all_rows)."""
    out, now = [], time.time()
    for p in _rows("SELECT * FROM hold72"):
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
            "timeframe": f"{HOLD_HOURS:g}h hold",
            "note": f"{p['direction']} {p['symbol']} vs {'short' if p['direction'] == 'long' else 'long'} BTC, "
                    f"conviction {p['conviction']:+.2f}",
            "mfe_r": round((p["best_pct"] or 0) / DISASTER_PCT, 2), "quantity": None, "liquidation": None, "tp_set": False,
            "spread_pct": round(p["mark_pct"] or 0, 3) if is_open else round(spread_pct(p, p["coin_exit"], p["btc_exit"]), 3),
            "opened_at": _iso(p["opened_ts"]),
        })
    return out


def config() -> dict:
    return {"min_conviction": MIN_CONVICTION, "hold_hours": HOLD_HOURS, "watch_below": WATCH_BELOW, "leg_pct": LEG_PCT, "max_pairs": MAX_PAIRS, "max_per_side": MAX_PER_SIDE,
            "disaster_pct": DISASTER_PCT, "hedge": HEDGE, "initial_balance": INITIAL_BALANCE}


def replay() -> dict | None:
    """The year-long replay this book is testing (research/hold_lab.py), if it has been run."""
    from research import hold_lab
    return hold_lab.load()
