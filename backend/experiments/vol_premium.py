"""Volatility premium — sells a defined-risk weekly strangle on BTC, ETH and SOL, on paper, off Deribit's public quotes.

Why (2026-10-28, research/vrp_lab.py on two years of hourly spot + 30-day implied vol): BTC's implied vol ran
3.1 points above the vol that then happened, on 69% of days; a 1σ strangle with 2σ wings made ≈ +1.8% of notional
per 30 days (78% of periods positive, worst −12.8%). ETH was thinner (+0.5 points; strangle +1.5%). The premium
shrank in the second year, so this is a paper test under the usual verdict, not a conviction. SOL joined on
2026-11-06 without a lab of its own (Deribit's SOL options are young), to bring the verdict forward: three
structures a week instead of two, each coin judged on its own row.

The trade (one structure per coin at a time, its own ledger — table `vol_premium` in swing_trading.db):
  when     no structure is open for the coin and a weekly expiry sits 5–9.5 days out (Fridays 08:00 UTC)
  what     sell the put and the call one implied σ from spot (σ scaled to the expiry), buy the put and call two σ
           out — an iron condor: the credit is the most it can make, the wing width less the credit the most it
           can lose. Sells are priced at the bid, buys at the ask, Deribit's taker fee on each leg.
  size     contracts so that the maximum loss is RISK_PCT of equity (Deribit's minimum lot respected)
  exit     held to expiry and settled at Deribit's delivery price — no management, so the result is the premium's
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from storage.db import SWING_DB, connect
from trading import book_switches

logger = logging.getLogger("experiments.vol_premium")

BOOK = "vol_premium"
LABEL = "Vol premium (weekly BTC/ETH/SOL condors)"
COINS = os.getenv("VOL_PREMIUM_COINS", "BTC,ETH,SOL").split(",")
RISK_PCT = float(os.getenv("VOL_PREMIUM_RISK_PCT", "15"))       # max loss per structure, % of equity (15% since 2026-10-29, was 5; one 0.1 BTC lot ≈ $400–500 worst case)
SHORT_SIGMA, WING_SIGMA = 1.0, 2.0
MIN_LOT = {"BTC": 0.1, "ETH": 1.0, "SOL": 10.0}       # Deribit's minimum trade, in units of the coin (one SOL contract is 10 SOL)
INITIAL_BALANCE = 10_000.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS vol_premium (
    id TEXT PRIMARY KEY, coin TEXT NOT NULL, expiry_ts REAL NOT NULL, underlying REAL, iv REAL, qty REAL,
    legs TEXT NOT NULL, credit REAL, max_loss REAL, fees REAL DEFAULT 0, opened_ts REAL, status TEXT NOT NULL,
    mark REAL DEFAULT 0, best REAL DEFAULT 0, worst REAL DEFAULT 0, closed_ts REAL, settle_price REAL, payout REAL,
    net REAL, close_reason TEXT, data TEXT
)"""

_lock = threading.Lock()
_con = None
_path = SWING_DB


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
    return _rows("SELECT * FROM vol_premium WHERE status='open' ORDER BY opened_ts")


def closed_positions() -> list[dict]:
    return _rows("SELECT * FROM vol_premium WHERE status='closed' ORDER BY closed_ts")


def balance() -> float:
    from trading import capital
    return capital.initial(BOOK, INITIAL_BALANCE) + sum(p["net"] or 0 for p in closed_positions())


def equity() -> float:
    return balance() + sum((p["mark"] or 0) for p in open_positions())


# ── Building the structure ─────────────────────────────────────────────────────

def _nearest(rows: list[dict], kind: str, target: float) -> dict | None:
    cands = [r for r in rows if r["type"] == kind and r.get("mark")]
    return min(cands, key=lambda r: abs(r["strike"] - target)) if cands else None


def _px(r: dict, selling: bool) -> float:
    """Execution price in coin: sells hit the bid, buys lift the ask; a one-sided book falls back to the mark ± 5%."""
    if selling:
        return r["bid"] if r.get("bid") else r["mark"] * 0.95
    return r["ask"] if r.get("ask") else r["mark"] * 1.05


def design(chain: list[dict], coin: str, expiry: float, now: float, equity_usd: float) -> dict | None:
    """The condor for one expiry: legs, credit, max loss and size — or None when the chain can't support it."""
    from data.deribit_client import fee_usd
    rows = [r for r in chain if r["coin"] == coin and abs(r["expiry_ts"] - expiry) < 60 and r.get("mark") and r.get("underlying")]
    if len(rows) < 8:
        return None
    s = rows[0]["underlying"]
    atm = sorted(rows, key=lambda r: abs(r["strike"] - s))[:2]
    iv = sum(r["iv"] for r in atm if r.get("iv")) / max(sum(1 for r in atm if r.get("iv")), 1)
    if not iv:
        return None
    t = max(expiry - now, 3600) / (365 * 86400)
    sig = iv / 100 * math.sqrt(t)
    want = [("put", s * (1 - SHORT_SIGMA * sig), "sell"), ("call", s * (1 + SHORT_SIGMA * sig), "sell"),
            ("put", s * (1 - WING_SIGMA * sig), "buy"), ("call", s * (1 + WING_SIGMA * sig), "buy")]
    legs = []
    for kind, k, side in want:
        r = _nearest(rows, kind, k)
        if not r:
            return None
        legs.append({"name": r["name"], "type": kind, "strike": r["strike"], "side": side, "px_coin": _px(r, side == "sell"),
                     "px_usd": round(_px(r, side == "sell") * s, 2), "iv": r.get("iv")})
    strikes = {(l["type"], l["side"]): l["strike"] for l in legs}
    if not (strikes[("put", "buy")] < strikes[("put", "sell")] < s < strikes[("call", "sell")] < strikes[("call", "buy")]):
        return None
    credit_1 = sum(l["px_usd"] * (1 if l["side"] == "sell" else -1) for l in legs)
    width = max(strikes[("put", "sell")] - strikes[("put", "buy")], strikes[("call", "buy")] - strikes[("call", "sell")])
    max_loss_1 = width - credit_1
    if credit_1 <= 0 or max_loss_1 <= 0:
        return None
    lot = MIN_LOT.get(coin, 1.0)
    qty = math.floor(equity_usd * RISK_PCT / 100 / max_loss_1 / lot) * lot
    if qty < lot:
        return None
    fees = round(sum(fee_usd(l["px_coin"], s, qty) for l in legs), 4)
    return {"coin": coin, "expiry_ts": expiry, "underlying": s, "iv": round(iv, 2), "qty": round(qty, 4), "legs": legs,
            "credit": round(credit_1 * qty, 4), "max_loss": round(max_loss_1 * qty, 4), "fees": fees, "sigma_pct": round(sig * 100, 2)}


def structure_value(legs: list[dict], chain_by_name: dict[str, dict], s: float) -> float | None:
    """What closing the structure would cost now (USD per contract): Σ side × mark × S, each leg at its own
    expiry's underlying (Deribit quotes every expiry against its own future; `s` is the fallback)."""
    total = 0.0
    for l in legs:
        r = chain_by_name.get(l["name"])
        if not r or r.get("mark") is None:
            return None
        total += (1 if l["side"] == "sell" else -1) * r["mark"] * (r.get("underlying") or s)
    return total


def payout_at(legs: list[dict], settle: float) -> float:
    """Intrinsic value owed on the short legs less what the long legs pay, per contract."""
    out = 0.0
    for l in legs:
        intrinsic = max(settle - l["strike"], 0) if l["type"] == "call" else max(l["strike"] - settle, 0)
        out += intrinsic * (1 if l["side"] == "sell" else -1)
    return out


# ── The loop ──────────────────────────────────────────────────────────────────

async def _open(coin: str, chain: list[dict], now: float) -> dict | None:
    from data.deribit_client import weekly_expiries
    from trading import capital
    exps = weekly_expiries([r for r in chain if r["coin"] == coin], now)
    if not exps:
        return None
    d = design(chain, coin, exps[0], now, capital.sizing_equity(BOOK, equity()))
    if not d:
        return None
    row = {"id": uuid.uuid4().hex[:12], **d, "legs": json.dumps(d["legs"]), "opened_ts": now, "status": "open", "mark": 0.0,
           "data": json.dumps({"sigma_pct": d["sigma_pct"]})}
    _write("INSERT INTO vol_premium (id, coin, expiry_ts, underlying, iv, qty, legs, credit, max_loss, fees, opened_ts, status, mark, data) "
           "VALUES (:id,:coin,:expiry_ts,:underlying,:iv,:qty,:legs,:credit,:max_loss,:fees,:opened_ts,:status,:mark,:data)", row)
    logger.info("Vol premium opened: %s condor %s×, IV %.1f%%, credit $%.2f, max loss $%.2f, expiry %s", coin, d["qty"], d["iv"],
                d["credit"], d["max_loss"], datetime.fromtimestamp(exps[0], timezone.utc).strftime("%m-%d"))
    return row


async def _settle(p: dict, now: float) -> dict | None:
    from data.deribit_client import SETTLE_FEE, delivery_price
    settle = await delivery_price(p["coin"], p["expiry_ts"])
    if settle is None:
        return None
    legs = json.loads(p["legs"])
    payout = round(payout_at(legs, settle) * p["qty"], 4)
    fees = round((p["fees"] or 0) + 4 * SETTLE_FEE * settle * p["qty"], 4)
    net = round(p["credit"] - payout - fees, 4)
    _write("UPDATE vol_premium SET status='closed', closed_ts=?, settle_price=?, payout=?, fees=?, net=?, close_reason='expired', mark=? WHERE id=?",
           (now, settle, payout, fees, net, net, p["id"]))
    logger.info("Vol premium settled: %s at %.0f — payout $%.2f, net $%.2f", p["coin"], settle, payout, net)
    return {**p, "status": "closed", "net": net, "payout": payout, "settle_price": settle}


def close_at_mark(p: dict, reason: str, now: float | None = None) -> dict:
    """Close a structure at its last mark (credit less the structure's current cost and fees) — a condor has no exit
    of its own before expiry; used when the book cannot run on this instance (scripts/close_unpromoted.py)."""
    now = now or time.time()
    net = round(p.get("mark") or 0.0, 4)
    _write("UPDATE vol_premium SET status='closed', closed_ts=?, net=?, close_reason=? WHERE id=?", (now, net, reason, p["id"]))
    logger.info("Vol premium closed at mark: %s — %s, net $%.2f", p["coin"], reason, net)
    return {**p, "status": "closed", "closed_ts": now, "net": net, "close_reason": reason}


def erase_closed(reason: str) -> int:
    """Remove closed rows with this close reason — for structures closed because the book cannot run on this instance,
    never for trades the book actually took."""
    with _lock:
        con = _db()
        n = con.execute("DELETE FROM vol_premium WHERE status='closed' AND close_reason=?", (reason,)).rowcount
        con.commit()
    logger.warning("Vol premium: erased %d closed rows with reason %r", n, reason)
    return n


async def tick(now: float | None = None) -> list[dict]:
    """Every few minutes: settle expired structures, mark open ones, open the next weekly where there is none."""
    from data.deribit_client import chain as fetch_chain
    from experiments import books
    if not books.ENABLED:
        return []
    now = now or time.time()
    closed = []
    for p in open_positions():
        if now >= p["expiry_ts"] + 300:
            done = await _settle(p, now)
            if done:
                closed.append(done)
    chains: dict[str, list[dict]] = {}
    for coin in COINS:
        try:
            chains[coin] = await fetch_chain(coin)
        except Exception as e:
            logger.debug("Deribit chain %s unavailable: %s", coin, e)
    for p in open_positions():
        rows = chains.get(p["coin"])
        if not rows:
            continue
        by_name = {r["name"]: r for r in rows}
        s = rows[0]["underlying"]
        cost = structure_value(json.loads(p["legs"]), by_name, s)
        if cost is None:
            continue
        mark = round(p["credit"] - cost * p["qty"] - (p["fees"] or 0), 4)
        _write("UPDATE vol_premium SET mark=?, best=MAX(best, ?), worst=MIN(worst, ?) WHERE id=?", (mark, mark, mark, p["id"]))
    if book_switches.enabled(BOOK):
        have = {p["coin"] for p in open_positions()}
        for coin in COINS:
            if coin not in have and chains.get(coin):
                await _open(coin, chains[coin], now)
    return closed


# ── Reporting ──────────────────────────────────────────────────────────────────

def rows() -> list[dict]:
    out, now = [], time.time()
    for p in _rows("SELECT * FROM vol_premium WHERE status IN ('open','closed')"):
        is_open = p["status"] == "open"
        net = (p["mark"] or 0) if is_open else (p["net"] or 0)
        end = now if is_open else p["closed_ts"]
        risk = p["max_loss"] or 0
        legs = json.loads(p["legs"])
        shorts = [l["strike"] for l in legs if l["side"] == "sell"]
        out.append({
            "id": p["id"], "system": BOOK, "symbol": f"{p['coin']}USDT", "direction": "short",
            "status": "open" if is_open else "closed", "opened_ts": p["opened_ts"],
            "closed_ts": None if is_open else p["closed_ts"], "hold_min": round((end - p["opened_ts"]) / 60, 1),
            "entry_price": p["underlying"], "exit_price": None if is_open else p["settle_price"], "mark_price": None,
            "stop_loss": None, "take_profit": None, "notional": round(p["underlying"] * p["qty"], 2), "leverage": None,
            "margin": round(risk, 2), "risk_usd": round(risk, 2),
            "gross_pnl": round(net + (p["fees"] or 0), 2), "fees": round(p["fees"] or 0, 2), "funding": 0.0, "net_pnl": round(net, 2),
            "r": round(net / risk, 2) if risk else None, "exit_reason": None if is_open else p["close_reason"],
            "outcome": None if is_open else ("win" if net > 0 else "loss"), "timeframe": "weekly",
            "note": f"iron condor {min(shorts):.0f}/{max(shorts):.0f}, IV {p['iv']:.0f}%, credit ${p['credit']:.0f}, "
                    f"expiry {datetime.fromtimestamp(p['expiry_ts'], timezone.utc).strftime('%m-%d')}",
            "mfe_r": round((p["best"] or 0) / risk, 2) if risk else None,
            "quantity": p["qty"], "liquidation": None, "tp_set": False,
            "opened_at": datetime.fromtimestamp(p["opened_ts"], timezone.utc).isoformat(),
        })
    return out


def status() -> dict:
    now = time.time()
    return {"open": [{"coin": p["coin"], "qty": p["qty"], "iv": p["iv"], "credit": p["credit"], "max_loss": p["max_loss"], "mark": p["mark"],
                      "days_to_expiry": round((p["expiry_ts"] - now) / 86400, 1),
                      "strikes": [l["strike"] for l in json.loads(p["legs"])]} for p in open_positions()],
            "closed": len(closed_positions()), "equity": round(equity(), 2), "config": config()}


def config() -> dict:
    return {"coins": COINS, "risk_pct": RISK_PCT, "short_sigma": SHORT_SIGMA, "wing_sigma": WING_SIGMA, "initial_balance": INITIAL_BALANCE}
