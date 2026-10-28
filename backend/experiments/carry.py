"""Carry — BTC, ETH and gold cash-and-carry, on paper: own the asset, short its perpetual, collect funding.

Why: the one edge that passed an untouched holdout in the 2026-10-07 search (research/carry_lab.py).
Leveraged longs pay shorts funding to keep the perp at spot; holding the coin and shorting the same
amount of its perp cancels the price risk and collects that. On the holdout year (Oct 2025 – Sep
2026) BTC made +3.8%/yr on notional (Sharpe 7.7, worst drawdown −0.4%) and ETH +2.1%/yr — much
less than 2022–24's ~9%/yr, and the same trade LOST money on altcoins, so only BTC and ETH run.

The book (its own ledger: swing_trading.db, table carry):
  * a position per coin (a second one tops it up when the target grows) — long spot and short perp, the
    same quantity; target notional per coin =
    equity × CARRY_NOTIONAL_X / number of coins (1.0 = all capital on notional; on Binance's unified
    account the spot coin margins the short, so 2–3× is possible at more liquidation risk)
  * opened at the live spot and perp prices; taker fees and slippage on both legs both ways
    (spot 0.10% + perp 0.05% + 0.03% slippage each)
  * funding: every settlement since the last check, from Binance's history, × the perp notional
  * marked every minute: spot P&L − perp P&L + funding − fees. The basis (perp − spot) moving is
    the main risk while it's on.
  * switched off on the Books tab: both legs are unwound (a carry trade has no natural exit)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid

from storage.db import SWING_DB, connect
from trading import book_switches, futures, monitor

logger = logging.getLogger("experiments.carry")

BOOK = "carry"
LABEL = "Carry · BTC, ETH & gold"
# Gold joined 2026-10-27 (research/tradfi_perp_lab.py): the XAU perp's longs paid +12.7%/yr funding over its 320 days
# (positive on 61% of days; ~7%/yr over the last three months), and tokenized gold on Binance spot (PAXG) tracks the
# perp within 0.3% — so the same cash-and-carry works: own PAXG, short XAUUSDT. Silver/platinum/palladium pay similar
# funding but have no spot leg here.
COINS = [c.strip() for c in os.getenv("CARRY_COINS", "BTCUSDT,ETHUSDT,XAUUSDT").split(",") if c.strip()]
SPOT_OF = {"XAUUSDT": "PAXGUSDT"}             # perp → the spot asset that hedges it, where the names differ
# 2× since 2026-10-23 (paper): funding was positive on 96% (BTC) / 93% (ETH) of the last 90 days, but at 1×
# that's ~$1.40 a day on the book. 2× doubles it; the risk is the short perp leg's margin in a sharp rally.
# 3× since 2026-10-28 (paper): flat markets are carry's best conditions and the one place scaling is sound. The spot
# leg margins the short; the risk at 3× is the short perp's margin in a sharp rally, which a real account must size for.
NOTIONAL_X = float(os.getenv("CARRY_NOTIONAL_X", "3.0"))
TOP_UP_BELOW = 0.8               # add a position when a coin holds under 80% of its target notional
INITIAL_BALANCE = 10_000.0
SPOT_FEE = 0.0010
FUNDING_CHECK_S = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS carry (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, qty REAL NOT NULL, spot_entry REAL NOT NULL, perp_entry REAL NOT NULL,
    notional REAL NOT NULL, opened_ts REAL NOT NULL, status TEXT NOT NULL, closed_ts REAL,
    spot_exit REAL, perp_exit REAL, close_reason TEXT, fees REAL DEFAULT 0, funding REAL DEFAULT 0,
    funding_through_ms INTEGER, spot_mark REAL, perp_mark REAL, net REAL, data TEXT
)"""

_lock = threading.Lock()
_con = None
_path = SWING_DB
_last_funding_check = {"at": 0.0}


def use(path: str) -> None:
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


def open_positions() -> list[dict]:
    return _rows("SELECT * FROM carry WHERE status='open' ORDER BY opened_ts")


def closed_positions() -> list[dict]:
    return _rows("SELECT * FROM carry WHERE status='closed' ORDER BY closed_ts")


# ── Accounting (pure) ──────────────────────────────────────────────────────────

def leg_fees(qty: float, spot: float, perp: float) -> float:
    """One side of both legs: spot taker + perp taker (slippage is in the fill prices)."""
    return qty * spot * SPOT_FEE + qty * perp * futures.TAKER_FEE


def mark_pnl(p: dict, spot: float, perp: float) -> float:
    """Long spot, short perp, plus funding received, less fees paid so far."""
    return p["qty"] * (spot - p["spot_entry"]) - p["qty"] * (perp - p["perp_entry"]) + (p["funding"] or 0) - (p["fees"] or 0)


def balance() -> float:
    from trading import capital
    return capital.initial("carry", INITIAL_BALANCE) + sum(p["net"] or 0 for p in closed_positions())


def equity() -> float:
    return balance() + sum(mark_pnl(p, p["spot_mark"] or p["spot_entry"], p["perp_mark"] or p["perp_entry"])
                           for p in open_positions())


# ── Trading ────────────────────────────────────────────────────────────────────

async def _prices(symbol: str) -> tuple[float | None, float | None]:
    from data import feeds
    try:
        spot = (await feeds.cached_ticker(SPOT_OF.get(symbol, symbol))).get("price")
    except Exception:
        spot = None
    return spot, await monitor.live_price(symbol)


def target_notional() -> float:
    from trading import capital
    return capital.sizing_equity("carry", equity()) * NOTIONAL_X / len(COINS)


async def _open(symbol: str, now: float, notional: float | None = None) -> dict | None:
    spot, perp = await _prices(symbol)
    if not spot or not perp:
        return None
    notional = notional or target_notional()
    spot_fill, perp_fill = spot * (1 + futures.SLIPPAGE), perp * (1 - futures.SLIPPAGE)     # buy spot, sell perp
    qty = notional / spot_fill
    p = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "qty": qty, "spot_entry": spot_fill, "perp_entry": perp_fill,
         "notional": round(notional, 2), "opened_ts": now, "status": "open", "fees": round(leg_fees(qty, spot_fill, perp_fill), 4),
         "funding": 0.0, "funding_through_ms": int(now * 1000), "spot_mark": spot, "perp_mark": perp,
         "data": json.dumps({"basis_bps_at_entry": round((perp / spot - 1) * 1e4, 2)})}
    _write("INSERT INTO carry (id, symbol, qty, spot_entry, perp_entry, notional, opened_ts, status, fees, funding, "
           "funding_through_ms, spot_mark, perp_mark, data) VALUES (:id,:symbol,:qty,:spot_entry,:perp_entry,:notional,"
           ":opened_ts,:status,:fees,:funding,:funding_through_ms,:spot_mark,:perp_mark,:data)", p)
    logger.info("Carry opened %s: long %.6f spot @ %.2f, short perp @ %.2f ($%.0f a leg)", symbol, qty, spot_fill, perp_fill, notional)
    return p


async def _close(p: dict, reason: str, now: float) -> dict | None:
    spot, perp = await _prices(p["symbol"])
    if not spot or not perp:
        return None
    spot_x, perp_x = spot * (1 - futures.SLIPPAGE), perp * (1 + futures.SLIPPAGE)      # sell spot, buy back perp
    fees = (p["fees"] or 0) + leg_fees(p["qty"], spot_x, perp_x)
    done = {**p, "fees": fees}
    net = round(mark_pnl(done, spot_x, perp_x), 4)
    _write("UPDATE carry SET status='closed', closed_ts=?, spot_exit=?, perp_exit=?, close_reason=?, fees=?, net=? WHERE id=?",
           (now, spot_x, perp_x, reason, round(fees, 4), net, p["id"]))
    logger.info("Carry closed %s — %s, net $%.2f", p["symbol"], reason, net)
    return {**done, "status": "closed", "net": net, "close_reason": reason}


async def _settle_funding(p: dict, now: float) -> float:
    """Add every funding settlement since the last one counted. Positive rate: the short receives."""
    from data.binance_client import fetch_funding_history
    try:
        rows = await fetch_funding_history(p["symbol"], int(p["funding_through_ms"] or p["opened_ts"] * 1000) + 1)
    except Exception as e:
        logger.debug("Funding for %s unavailable: %s", p["symbol"], e)
        return 0.0
    rows = [r for r in rows if r["time"] <= now * 1000]
    if not rows:
        return 0.0
    got = sum(r["rate"] * p["qty"] * (r["mark"] or p["perp_mark"] or p["perp_entry"]) for r in rows)
    _write("UPDATE carry SET funding = funding + ?, funding_through_ms=? WHERE id=?", (got, max(r["time"] for r in rows), p["id"]))
    return got


async def tick(now: float | None = None) -> None:
    """Every minute: open what's missing, mark, settle funding; unwind when switched off."""
    from experiments import books
    now = now or time.time()
    if not books.ENABLED:
        return
    on = book_switches.enabled(BOOK)
    positions = open_positions()
    if not on:
        for p in positions:
            await _close(p, "switched_off", now)
        return
    target = target_notional()
    for sym in COINS:
        held = sum(p["notional"] for p in positions if p["symbol"] == sym)
        if held < TOP_UP_BELOW * target:
            # Top up rather than close and reopen (which would pay both legs' fees twice).
            await _open(sym, now, target - held)
    check_funding = now - _last_funding_check["at"] >= FUNDING_CHECK_S
    for p in open_positions():
        spot, perp = await _prices(p["symbol"])
        if spot and perp:
            _write("UPDATE carry SET spot_mark=?, perp_mark=? WHERE id=?", (spot, perp, p["id"]))
        if check_funding:
            await _settle_funding({**p, "perp_mark": perp or p["perp_mark"]}, now)
    if check_funding:
        _last_funding_check["at"] = now


# ── Reporting ──────────────────────────────────────────────────────────────────

def rows() -> list[dict]:
    """Positions in the shape the Trades page, Lab and Books read (overview.all_rows)."""
    out, now = [], time.time()
    for p in _rows("SELECT * FROM carry"):
        is_open = p["status"] == "open"
        net = mark_pnl(p, p["spot_mark"] or p["spot_entry"], p["perp_mark"] or p["perp_entry"]) if is_open else (p["net"] or 0)
        end = now if is_open else p["closed_ts"]
        price_pnl = net - (p["funding"] or 0) + (p["fees"] or 0)
        out.append({
            "id": p["id"], "system": BOOK, "symbol": p["symbol"], "direction": "short", "status": "open" if is_open else "closed",
            "opened_ts": p["opened_ts"], "closed_ts": None if is_open else p["closed_ts"],
            "hold_min": round((end - p["opened_ts"]) / 60, 1), "entry_price": p["perp_entry"],
            "exit_price": None if is_open else p["perp_exit"], "mark_price": p["perp_mark"] if is_open else None,
            "stop_loss": None, "take_profit": None, "notional": round(p["notional"], 2), "leverage": None, "margin": None,
            "risk_usd": None, "gross_pnl": round(price_pnl, 2), "fees": round(p["fees"] or 0, 2),
            # Funding here is RECEIVED (positive = earned) — the carry's income, unlike a normal position.
            "funding": round(-(p["funding"] or 0), 2), "net_pnl": round(net, 2), "r": None,
            "exit_reason": None if is_open else p["close_reason"], "outcome": None if is_open else ("win" if net > 0 else "loss"),
            "timeframe": "carry", "note": f"long {p['symbol'][:-4]} spot / short perp · funding earned ${p['funding'] or 0:.2f}",
            "mfe_r": None, "quantity": round(p["qty"], 8), "liquidation": None, "tp_set": False,
            "funding_earned": round(p["funding"] or 0, 4), "basis_pnl": round(price_pnl, 4),
        })
    return out


def status() -> dict:
    ps = open_positions()
    earned = sum(p["funding"] or 0 for p in _rows("SELECT funding FROM carry"))
    return {"coins": COINS, "notional_x": NOTIONAL_X, "open": len(ps), "equity": round(equity(), 2),
            "funding_earned": round(earned, 2), "positions": [
                {"symbol": p["symbol"], "notional": p["notional"], "funding": round(p["funding"] or 0, 4),
                 "basis_bps_now": round(((p["perp_mark"] or 0) / (p["spot_mark"] or 1) - 1) * 1e4, 2) if p["spot_mark"] else None,
                 "pnl": round(mark_pnl(p, p["spot_mark"] or p["spot_entry"], p["perp_mark"] or p["perp_entry"]), 2),
                 "days": round((time.time() - p["opened_ts"]) / 86400, 1)} for p in ps]}


def research() -> dict | None:
    """The carry lab's holdout result for the coins this book runs."""
    try:
        res = json.load(open(os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "carry_lab_results.json")))
    except (OSError, ValueError):
        return None
    return {x["coin"]: x["holdout"] for x in res.get("per_coin_always", []) if x["coin"] in COINS} | {"holdout_from": res.get("holdout_from")}
