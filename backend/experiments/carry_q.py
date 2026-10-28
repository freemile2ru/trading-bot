"""Carry Q — cash-and-carry against the QUARTERLY future instead of the perpetual, on paper.

Why (2026-10-27, research/carry_upgrade_lab.py): over 2023–26 the quarterly's basis and the perp's funding paid
about the same (7.5%/yr each on BTC), but the quarterly's return is LOCKED at entry while funding floats — and
right now the quarterly pays more than funding is expected to (BTC 5.4% vs 4.0%, ETH 4.4% vs 3.8%).

The trade (own ledger, table carry_q in swing_trading.db):
  open     long spot BTC/ETH, short the same quantity of the current-quarter USDⓈ-M future (e.g. BTCUSDT_261225);
           target notional per coin = equity × NOTIONAL_X / coins, topped up with a second position when it grows. The annualised basis at entry is recorded: that is
           the return the position earns if held to delivery, whatever happens to price.
  roll     ROLL_DAYS before delivery the future is bought back and the next quarter sold (four fills).
  costs    spot 0.10% + future 0.05% + slippage on every fill; no funding on either leg.
  mark     every minute at the live spot and future prices; the basis converging to zero at delivery is the P&L.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid

from storage.db import SWING_DB, connect
from trading import book_switches, futures

logger = logging.getLogger("experiments.carry_q")

BOOK = "carry_q"
LABEL = "Carry Q · quarterly basis"
COINS = [c.strip() for c in os.getenv("CARRY_Q_COINS", "BTCUSDT,ETHUSDT").split(",") if c.strip()]
NOTIONAL_X = float(os.getenv("CARRY_Q_NOTIONAL_X", "3.0"))       # 3× since 2026-10-28 (paper), with carry
TOP_UP_BELOW = 0.8               # add a position when a coin holds under 80% of its target notional
ROLL_DAYS = float(os.getenv("CARRY_Q_ROLL_DAYS", "7"))
INITIAL_BALANCE = 10_000.0
SPOT_FEE = 0.0010
FAPI = "https://fapi.binance.com/fapi/v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS carry_q (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, contract TEXT NOT NULL, qty REAL NOT NULL,
    spot_entry REAL NOT NULL, fut_entry REAL NOT NULL, notional REAL NOT NULL, opened_ts REAL NOT NULL,
    delivery_ts REAL NOT NULL, basis_ann REAL, status TEXT NOT NULL, closed_ts REAL, spot_exit REAL, fut_exit REAL,
    close_reason TEXT, fees REAL DEFAULT 0, net REAL, spot_mark REAL, fut_mark REAL, data TEXT
)"""

_lock = threading.Lock()
_con = None
_path = SWING_DB
_contracts: dict = {"at": 0.0, "by": {}}


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
    return _rows("SELECT * FROM carry_q WHERE status='open' ORDER BY opened_ts")


def closed_positions() -> list[dict]:
    return _rows("SELECT * FROM carry_q WHERE status='closed' ORDER BY closed_ts")


def leg_fees(qty: float, spot: float, fut: float) -> float:
    return qty * spot * SPOT_FEE + qty * fut * futures.TAKER_FEE


def mark_pnl(p: dict, spot: float, fut: float) -> float:
    return p["qty"] * (spot - p["spot_entry"]) - p["qty"] * (fut - p["fut_entry"]) - (p["fees"] or 0)


def balance() -> float:
    from trading import capital
    return capital.initial("carry_q", INITIAL_BALANCE) + sum(p["net"] or 0 for p in closed_positions())


def equity() -> float:
    return balance() + sum(mark_pnl(p, p["spot_mark"] or p["spot_entry"], p["fut_mark"] or p["fut_entry"]) for p in open_positions())


# ── Contracts and prices ───────────────────────────────────────────────────────

async def contracts(now: float | None = None) -> dict[str, list[tuple[str, float]]]:
    """{pair: [(contract symbol, delivery ts), …] soonest first} from exchangeInfo (cached 6h)."""
    now = now or time.time()
    if now - _contracts["at"] < 6 * 3600 and _contracts["by"]:
        return _contracts["by"]
    from data.http import get_json
    info = await get_json(f"{FAPI}/exchangeInfo", timeout=20.0)
    by: dict[str, list[tuple[str, float]]] = {}
    for s in info.get("symbols", []):
        if s.get("contractType") in ("CURRENT_QUARTER", "NEXT_QUARTER") and s.get("status") == "TRADING":
            by.setdefault(s["pair"], []).append((s["symbol"], s["deliveryDate"] / 1000))
    for v in by.values():
        v.sort(key=lambda x: x[1])
    _contracts.update(at=now, by=by)
    return by


async def pick_contract(symbol: str, now: float) -> tuple[str, float] | None:
    """The quarterly to be short: the current one unless it delivers within ROLL_DAYS, then the next."""
    opts = (await contracts(now)).get(symbol) or []
    for c, d in opts:
        if d - now > ROLL_DAYS * 86400:
            return c, d
    return None


async def _prices(symbol: str, contract: str) -> tuple[float | None, float | None]:
    from data import feeds
    from data.http import get_json
    try:
        spot = (await feeds.cached_ticker(symbol)).get("price")
    except Exception:
        spot = None
    try:
        fut = float((await get_json(f"{FAPI}/ticker/price", params={"symbol": contract}))["price"])
    except Exception:
        fut = None
    return spot, fut


# ── Trading ────────────────────────────────────────────────────────────────────

def target_notional() -> float:
    from trading import capital
    return capital.sizing_equity("carry_q", equity()) * NOTIONAL_X / len(COINS)


async def _open(symbol: str, now: float, notional: float | None = None) -> dict | None:
    picked = await pick_contract(symbol, now)
    if not picked:
        return None
    contract, delivery = picked
    spot, fut = await _prices(symbol, contract)
    if not spot or not fut:
        return None
    notional = notional or target_notional()
    spot_fill, fut_fill = spot * (1 + futures.SLIPPAGE), fut * (1 - futures.SLIPPAGE)       # buy spot, sell the future
    qty = notional / spot_fill
    days = max((delivery - now) / 86400, 1)
    basis_ann = (fut_fill / spot_fill - 1) * 365 / days
    p = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "contract": contract, "qty": qty, "spot_entry": spot_fill,
         "fut_entry": fut_fill, "notional": round(notional, 2), "opened_ts": now, "delivery_ts": delivery,
         "basis_ann": round(basis_ann, 5), "status": "open", "fees": round(leg_fees(qty, spot_fill, fut_fill), 4),
         "spot_mark": spot, "fut_mark": fut, "data": json.dumps({"basis_bps_at_entry": round((fut / spot - 1) * 1e4, 2)})}
    _write("INSERT INTO carry_q (id, symbol, contract, qty, spot_entry, fut_entry, notional, opened_ts, delivery_ts, basis_ann, "
           "status, fees, spot_mark, fut_mark, data) VALUES (:id,:symbol,:contract,:qty,:spot_entry,:fut_entry,:notional,"
           ":opened_ts,:delivery_ts,:basis_ann,:status,:fees,:spot_mark,:fut_mark,:data)", p)
    logger.info("Carry Q opened %s: long %.6f spot @ %.2f, short %s @ %.2f — basis locked %+.2f%%/yr to %s",
                symbol, qty, spot_fill, contract, fut_fill, basis_ann * 100, time.strftime("%Y-%m-%d", time.gmtime(delivery)))
    return p


async def _close(p: dict, reason: str, now: float) -> dict | None:
    spot, fut = await _prices(p["symbol"], p["contract"])
    if not spot or not fut:
        return None
    spot_x, fut_x = spot * (1 - futures.SLIPPAGE), fut * (1 + futures.SLIPPAGE)              # sell spot, buy back
    fees = (p["fees"] or 0) + leg_fees(p["qty"], spot_x, fut_x)
    net = round(mark_pnl({**p, "fees": fees}, spot_x, fut_x), 4)
    _write("UPDATE carry_q SET status='closed', closed_ts=?, spot_exit=?, fut_exit=?, close_reason=?, fees=?, net=? WHERE id=?",
           (now, spot_x, fut_x, reason, round(fees, 4), net, p["id"]))
    logger.info("Carry Q closed %s (%s) — %s, net $%.2f", p["symbol"], p["contract"], reason, net)
    return {**p, "status": "closed", "net": net, "close_reason": reason}


async def tick(now: float | None = None) -> None:
    """Every minute: open what's missing, roll what's near delivery, mark; unwind when switched off."""
    from experiments import books
    now = now or time.time()
    if not books.ENABLED:
        return
    positions = open_positions()
    if not book_switches.enabled(BOOK):
        for p in positions:
            await _close(p, "switched_off", now)
        return
    target = target_notional()
    for sym in COINS:
        mine = [p for p in positions if p["symbol"] == sym]
        for p in list(mine):                                 # a copy: removing while iterating skipped the next one
            if p["delivery_ts"] - now <= ROLL_DAYS * 86400 and await _close(p, "roll", now):
                mine.remove(p)
        held = sum(p["notional"] for p in mine)
        if held < TOP_UP_BELOW * target:
            await _open(sym, now, target - held)            # top up rather than close and reopen (fees twice)
    for p in open_positions():
        spot, fut = await _prices(p["symbol"], p["contract"])
        if spot and fut:
            _write("UPDATE carry_q SET spot_mark=?, fut_mark=? WHERE id=?", (spot, fut, p["id"]))


# ── Reporting ──────────────────────────────────────────────────────────────────

def rows() -> list[dict]:
    out, now = [], time.time()
    for p in _rows("SELECT * FROM carry_q"):
        is_open = p["status"] == "open"
        net = mark_pnl(p, p["spot_mark"] or p["spot_entry"], p["fut_mark"] or p["fut_entry"]) if is_open else (p["net"] or 0)
        end = now if is_open else p["closed_ts"]
        out.append({
            "id": p["id"], "system": BOOK, "symbol": p["symbol"], "direction": "short", "status": "open" if is_open else "closed",
            "opened_ts": p["opened_ts"], "closed_ts": None if is_open else p["closed_ts"],
            "hold_min": round((end - p["opened_ts"]) / 60, 1), "entry_price": p["fut_entry"],
            "exit_price": None if is_open else p["fut_exit"], "mark_price": p["fut_mark"] if is_open else None,
            "stop_loss": None, "take_profit": None, "notional": round(p["notional"], 2), "leverage": None, "margin": None,
            "risk_usd": None, "gross_pnl": round(net + (p["fees"] or 0), 2), "fees": round(p["fees"] or 0, 2), "funding": 0.0,
            "net_pnl": round(net, 2), "r": None, "exit_reason": None if is_open else p["close_reason"],
            "outcome": None if is_open else ("win" if net > 0 else "loss"), "timeframe": "quarterly",
            "note": f"long {p['symbol'][:-4]} spot / short {p['contract']} · basis locked {p['basis_ann'] * 100:+.2f}%/yr",
            "mfe_r": None, "quantity": round(p["qty"], 8), "liquidation": None, "tp_set": False,
        })
    return out


def status() -> dict:
    ps, now = open_positions(), time.time()
    return {"coins": COINS, "notional_x": NOTIONAL_X, "open": len(ps), "equity": round(equity(), 2),
            "positions": [{"symbol": p["symbol"], "contract": p["contract"], "notional": p["notional"],
                           "basis_locked_pct": round(p["basis_ann"] * 100, 2), "days_to_delivery": round((p["delivery_ts"] - now) / 86400, 1),
                           "pnl": round(mark_pnl(p, p["spot_mark"] or p["spot_entry"], p["fut_mark"] or p["fut_entry"]), 2),
                           "days": round((now - p["opened_ts"]) / 86400, 1)} for p in ps]}


def config() -> dict:
    return {"coins": COINS, "notional_x": NOTIONAL_X, "roll_days": ROLL_DAYS, "initial_balance": INITIAL_BALANCE}
