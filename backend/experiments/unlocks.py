"""Unlock shorts — short a coin into a large cliff unlock, hedged with BTC, on paper.

Why (2026-10-28, research sweep): Keyrock's study of 16,000+ unlock events found ~90% put pressure on price and
that the slide happens BEFORE the date — median −14.7% over the month before, −9.1% in the final two weeks —
with team/investor cliffs the worst (≈ −25%) and ecosystem/community unlocks flat. Known future supply gets
front-run, the same mechanism as the delisting shorts. We have no history of our own to replay (unlock calendars
are paid), so this is a forward test under the usual 40-trade verdict.

The trade (one per coin, its own ledger — table `unlocks` in swing_trading.db):
  source   CoinMarketCap's unlock calendar (data/unlocks_client.py), refreshed every few hours
  pick     the NEXT unlock is a cliff (≥ CLIFF_SHARE of it), worth ≥ MIN_PCT of supply, and either mostly insider
           allocations (≥ INSIDER_SHARE) or very large (≥ BIG_PCT); the coin has a Binance USDT perp
  entry    ENTRY_DAYS before the unlock (or at once if nearer, but ≥ MIN_LEAD_DAYS away): short the coin's perp +
           long BTC's perp, equal notional, LEG_PCT of equity a leg
  exit     at the unlock time; a DISASTER_PCT pair stop; or the funding kill (shorts crowd in before unlocks)
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

logger = logging.getLogger("experiments.unlocks")

BOOK = "unlocks"
LABEL = "Unlock shorts (cliff unlocks vs BTC)"
MIN_PCT = float(os.getenv("UNLOCKS_MIN_PCT", "1.0"))            # of supply, CMC's figure
BIG_PCT = float(os.getenv("UNLOCKS_BIG_PCT", "5.0"))
CLIFF_SHARE = 0.5
INSIDER_SHARE = float(os.getenv("UNLOCKS_INSIDER_SHARE", "0.5"))
ENTRY_DAYS = float(os.getenv("UNLOCKS_ENTRY_DAYS", "14"))
MIN_LEAD_DAYS = 2.0
LEG_PCT = float(os.getenv("UNLOCKS_LEG_PCT", "10"))
MAX_OPEN = int(os.getenv("UNLOCKS_MAX_OPEN", "8"))
DISASTER_PCT = float(os.getenv("UNLOCKS_DISASTER_PCT", "25"))
FUNDING_KILL = float(os.getenv("UNLOCKS_FUNDING_KILL", "0.005"))
MIN_VOLUME_USD = 2_000_000           # a day's spot volume below this isn't worth the fills
INITIAL_BALANCE = 10_000.0
HEDGE = "BTCUSDT"

SCHEMA = """
CREATE TABLE IF NOT EXISTS unlocks (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, coin TEXT, unlock_ts REAL NOT NULL, pct REAL, usd REAL,
    insider_share REAL, cliff_share REAL, opened_ts REAL, deadline_ts REAL, coin_entry REAL, btc_entry REAL,
    notional REAL, status TEXT NOT NULL, closed_ts REAL, coin_exit REAL, btc_exit REAL, close_reason TEXT,
    fees REAL DEFAULT 0, funding REAL DEFAULT 0, gross REAL, net REAL,
    best_pct REAL DEFAULT 0, worst_pct REAL DEFAULT 0, mark_pct REAL DEFAULT 0, data TEXT
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
    return _rows("SELECT * FROM unlocks WHERE status='open' ORDER BY opened_ts")


def pending() -> list[dict]:
    return _rows("SELECT * FROM unlocks WHERE status='pending' ORDER BY unlock_ts")


def closed_positions() -> list[dict]:
    return _rows("SELECT * FROM unlocks WHERE status='closed' ORDER BY closed_ts")


def spread_pct(p: dict, coin_px: float, btc_px: float) -> float:
    return (-(coin_px / p["coin_entry"] - 1) + (btc_px / p["btc_entry"] - 1)) * 100


def fill(price: float, buying: bool) -> float:
    return price * (1 + futures.SLIPPAGE) if buying else price * (1 - futures.SLIPPAGE)


def balance() -> float:
    from trading import capital
    return capital.initial(BOOK, INITIAL_BALANCE) + sum(p["net"] or 0 for p in closed_positions())


def equity() -> float:
    return balance() + sum((p["mark_pct"] or 0) / 100 * (p["notional"] or 0) for p in open_positions())


# ── Candidates ─────────────────────────────────────────────────────────────────

def qualifies(u: dict) -> bool:
    """The research's shape: a sizeable cliff, insider-heavy or very large."""
    if u["cliff_share"] < CLIFF_SHARE or u["pct"] < MIN_PCT:
        return False
    if (u.get("volume_24h") or 0) < MIN_VOLUME_USD:
        return False
    return u["insider_share"] >= INSIDER_SHARE or u["pct"] >= BIG_PCT


def candidates(calendar: list[dict], perps: set[str], now: float) -> list[dict]:
    """Qualifying unlocks on Binance perps, inside the entry window, not already on the book."""
    from data.unlocks_client import perp_for
    known = {(p["symbol"], round(p["unlock_ts"])) for p in _rows("SELECT symbol, unlock_ts FROM unlocks")}
    out = []
    for u in calendar:
        perp = perp_for(u["symbol"], perps)
        if not perp or perp == HEDGE or not qualifies(u):
            continue
        lead = (u["ts"] - now) / 86400
        if not (MIN_LEAD_DAYS <= lead <= ENTRY_DAYS) or (perp, round(u["ts"])) in known:
            continue
        out.append({**u, "perp": perp, "lead_days": round(lead, 1)})
    out.sort(key=lambda u: -u["pct"])
    return out


async def queue(now: float | None = None) -> list[dict]:
    """Add qualifying unlocks that are inside the entry window as pending rows."""
    from data.unlocks_client import cached
    from experiments import books
    now = now or time.time()
    if not books.ENABLED or not book_switches.enabled(BOOK):
        return []
    perps = await _perps(now)
    queued = []
    for u in candidates(cached(), perps, now):
        row = {"id": uuid.uuid4().hex[:12], "symbol": u["perp"], "coin": u["symbol"], "unlock_ts": u["ts"], "pct": u["pct"],
               "usd": u["usd"], "insider_share": u["insider_share"], "cliff_share": u["cliff_share"], "deadline_ts": u["ts"],
               "status": "pending", "data": json.dumps({"allocations": u.get("allocations"), "name": u.get("name"), "rank": u.get("cmc_rank")})}
        _write("INSERT INTO unlocks (id, symbol, coin, unlock_ts, pct, usd, insider_share, cliff_share, deadline_ts, status, data) "
               "VALUES (:id,:symbol,:coin,:unlock_ts,:pct,:usd,:insider_share,:cliff_share,:deadline_ts,:status,:data)", row)
        queued.append(row)
        logger.info("Unlock queued: %s %.1f%% of supply ($%.1fM, insider %.0f%%) on %s", u["perp"], u["pct"], u["usd"] / 1e6,
                    u["insider_share"] * 100, datetime.fromtimestamp(u["ts"], timezone.utc).strftime("%m-%d %H:%M"))
    return queued


async def _open(p: dict, now: float) -> dict | None:
    if len(open_positions()) >= MAX_OPEN:
        return None
    coin, btc = await monitor.live_price(p["symbol"]), await monitor.live_price(HEDGE)
    if not coin or not btc:
        return None
    from trading import capital
    notional = round(capital.sizing_equity(BOOK, equity()) * LEG_PCT / 100, 2)
    _write("UPDATE unlocks SET status='open', opened_ts=?, coin_entry=?, btc_entry=?, notional=?, fees=? WHERE id=?",
           (now, fill(coin, buying=False), fill(btc, buying=True), notional, round(2 * notional * futures.TAKER_FEE, 4), p["id"]))
    logger.info("Unlock short opened: %s vs BTC ($%.0f a leg) until the unlock on %s", p["symbol"], notional,
                datetime.fromtimestamp(p["unlock_ts"], timezone.utc).strftime("%m-%d %H:%M"))
    return _rows("SELECT * FROM unlocks WHERE id=?", (p["id"],))[0]


async def _funding(p: dict, until: float) -> tuple[float, float]:
    from data.binance_client import fetch_funding_history
    start, paid, worst = int(p["opened_ts"] * 1000), 0.0, 0.0
    for symbol, side in ((p["symbol"], -1), (HEDGE, 1)):
        try:
            for f in await fetch_funding_history(symbol, start):
                if f["time"] <= until * 1000:
                    paid += f["rate"] * p["notional"] * side
                    if side < 0:
                        worst = max(worst, -f["rate"])
        except Exception as e:
            logger.debug("Funding for %s unavailable: %s", symbol, e)
    return round(paid, 4), worst


async def close(p: dict, coin_px: float, btc_px: float, reason: str, now: float | None = None) -> dict:
    now = now or time.time()
    coin_exit, btc_exit = fill(coin_px, buying=True), fill(btc_px, buying=False)
    gross = round(spread_pct(p, coin_exit, btc_exit) / 100 * p["notional"], 4)
    fees = round((p["fees"] or 0) + 2 * p["notional"] * futures.TAKER_FEE, 4)
    funding, _ = await _funding(p, now)
    net = round(gross - fees - funding, 4)
    _write("UPDATE unlocks SET status='closed', closed_ts=?, coin_exit=?, btc_exit=?, close_reason=?, fees=?, funding=?, "
           "gross=?, net=? WHERE id=?", (now, coin_exit, btc_exit, reason, fees, funding, gross, net, p["id"]))
    logger.info("Unlock short closed: %s — %s, net $%.2f", p["symbol"], reason, net)
    return {**p, "status": "closed", "closed_ts": now, "close_reason": reason, "gross": gross, "fees": fees, "funding": funding, "net": net}


_last_funding_check = {"at": 0.0}
_last_refresh = {"at": 0.0}
_perp_cache: dict = {"at": 0.0, "symbols": set()}


async def _perps(now: float) -> set[str]:
    if now - _perp_cache["at"] > 3600 or not _perp_cache["symbols"]:
        from data.binance_client import fetch_perpetual_symbols
        try:
            _perp_cache.update(at=now, symbols=set(await fetch_perpetual_symbols()))
        except Exception as e:
            logger.debug("perp list unavailable: %s", e)
    return _perp_cache["symbols"]


async def tick(now: float | None = None) -> list[dict]:
    """Every minute: refresh the calendar now and then, queue and open what's in the window, manage what's open."""
    from data import unlocks_client
    from experiments import books
    if not books.ENABLED:
        return []
    now = now or time.time()
    if now - _last_refresh["at"] >= 3600:
        _last_refresh["at"] = now
        await unlocks_client.refresh(now)
    await queue(now)
    if book_switches.enabled(BOOK):
        for p in pending():
            if p["unlock_ts"] - now < MIN_LEAD_DAYS * 86400 / 2:
                _write("UPDATE unlocks SET status='closed', closed_ts=?, close_reason='missed', net=0 WHERE id=?", (now, p["id"]))
                continue
            await _open(p, now)
    closed = []
    check_funding = now - _last_funding_check["at"] >= 900
    btc = await monitor.live_price(HEDGE)
    for p in open_positions():
        px = await monitor.live_price(p["symbol"])
        if not px or not btc:
            continue
        mark = spread_pct(p, px, btc)
        _write("UPDATE unlocks SET mark_pct=?, best_pct=MAX(best_pct, ?), worst_pct=MIN(worst_pct, ?) WHERE id=?",
               (round(mark, 4), round(mark, 4), round(mark, 4), p["id"]))
        if mark <= -DISASTER_PCT:
            closed.append(await close(p, px, btc, "disaster_stop", now))
        elif now >= p["deadline_ts"]:
            closed.append(await close(p, px, btc, "unlock_reached", now))
        elif check_funding:
            _, worst = await _funding(p, now)
            if worst >= FUNDING_KILL:
                closed.append(await close(p, px, btc, "funding_kill", now))
    if check_funding:
        _last_funding_check["at"] = now
    return closed


# ── Reporting ──────────────────────────────────────────────────────────────────

def rows() -> list[dict]:
    out, now = [], time.time()
    for p in _rows("SELECT * FROM unlocks WHERE status IN ('open','closed') AND opened_ts IS NOT NULL"):
        is_open = p["status"] == "open"
        risk = (p["notional"] or 0) * DISASTER_PCT / 100
        net = (p["mark_pct"] or 0) / 100 * (p["notional"] or 0) - (p["fees"] or 0) if is_open else (p["net"] or 0)
        end = now if is_open else p["closed_ts"]
        out.append({
            "id": p["id"], "system": BOOK, "symbol": p["symbol"], "direction": "short",
            "status": "open" if is_open else "closed", "opened_ts": p["opened_ts"],
            "closed_ts": None if is_open else p["closed_ts"], "hold_min": round((end - p["opened_ts"]) / 60, 1),
            "entry_price": p["coin_entry"], "exit_price": None if is_open else p["coin_exit"], "mark_price": None,
            "stop_loss": None, "take_profit": None, "notional": round(p["notional"] or 0, 2), "leverage": None,
            "margin": None, "risk_usd": round(risk, 2),
            "gross_pnl": round(p["gross"] if not is_open else net + (p["fees"] or 0), 2),
            "fees": round(p["fees"] or 0, 2), "funding": round(p["funding"] or 0, 2), "net_pnl": round(net, 2),
            "r": round(net / risk, 2) if risk else None, "exit_reason": None if is_open else p["close_reason"],
            "outcome": None if is_open else ("win" if net > 0 else "loss"), "timeframe": "into unlock",
            "note": f"unlock {p['pct']:.1f}% of supply (${(p['usd'] or 0) / 1e6:.1f}M, insider {(p['insider_share'] or 0) * 100:.0f}%) "
                    f"on {datetime.fromtimestamp(p['unlock_ts'], timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
            "mfe_r": round((p["best_pct"] or 0) / DISASTER_PCT, 2),
            "quantity": None, "liquidation": None, "tp_set": False,
            "opened_at": datetime.fromtimestamp(p["opened_ts"], timezone.utc).isoformat(),
        })
    return out


def status() -> dict:
    from data import unlocks_client
    now = time.time()
    cal = unlocks_client.cached()
    watch = sorted([u for u in cal if qualifies(u) and 0 <= u["ts"] - now <= 45 * 86400], key=lambda u: u["ts"])
    return {"open": [{"symbol": p["symbol"], "pct": p["pct"], "mark_pct": p["mark_pct"], "unlock_ts": p["unlock_ts"]} for p in open_positions()],
            "pending": [{"symbol": p["symbol"], "pct": p["pct"], "unlock_ts": p["unlock_ts"]} for p in pending()],
            "watch": [{"symbol": u["symbol"], "pct": u["pct"], "usd": u["usd"], "insider_share": u["insider_share"], "ts": u["ts"]} for u in watch[:12]],
            "closed": len(closed_positions()), "equity": round(equity(), 2), "calendar_coins": len(cal),
            "calendar_refreshed_at": unlocks_client.refreshed_at(), "calendar_record": unlocks_client.snapshot_status(), "config": config()}


def config() -> dict:
    return {"min_pct": MIN_PCT, "big_pct": BIG_PCT, "insider_share": INSIDER_SHARE, "entry_days": ENTRY_DAYS, "leg_pct": LEG_PCT,
            "max_open": MAX_OPEN, "disaster_pct": DISASTER_PCT, "funding_kill": FUNDING_KILL, "hedge": HEDGE, "initial_balance": INITIAL_BALANCE}
