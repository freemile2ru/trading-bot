"""Event shorts — short what Binance announces (listings and delistings), hedged with BTC, on paper.

Why (2026-10-26, research/event_study_lab.py): on ~200 Binance announcements since late 2023, measured
from the first hourly close AFTER each one, the named coin fell against BTC over the next week in
every event type, in both halves of the sample:
  spot listing (perp already trading)   −2,230 bps over 7d, 84% of events, t −4.7 (a +1,324 bps run-up before)
  futures launch (the new perp's first week)   −1,456 bps, 65%, t −3.2
  delisting                              −2,159 bps, 81%, t −3.3 (−534 bps in the first hour alone)
Shorting these coins means paying funding (shorts crowd in): 2.6–3.7% per event on average, with tails to
96% — so the book kills a position once a funding settlement costs the short more than FUNDING_KILL.

The trade (one per coin, its own ledger — table `events` in swing_trading.db):
  source   the news collector's Binance announcement feeds (polled every 2 min); classified by the lab's
           own parser so the live rule is exactly the one that was tested
  entry    short the coin's perp + long BTC's perp, equal notional, LEG_PCT of equity a leg;
           spot listing / delisting: at once; futures launch: an hour after the perp starts trading
  exit     HOLD_DAYS later; a delisting before its delist date (title) minus a margin; a DISASTER_PCT pair
           stop; or the funding kill. Four taker fills; real funding on both legs settled at close.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from storage import state as kv
from storage.db import SWING_DB, connect
from trading import book_switches, futures, monitor

logger = logging.getLogger("experiments.events")

BOOK = "events"
LABEL = "Event shorts (listings & delistings)"
# Hold per event type (2026-10-27, event_study_lab with 30-day windows): the move keeps going — spot listings
# −22% vs BTC after 7 days but −46% after 30 (86% of events, t −8); new perps −15% → −29%; net of the funding a
# short pays that is ≈ +19% → +38% and +12% → +24% per event. Delistings are bounded by the delist date anyway.
HOLD_DAYS_BY_KIND = {"spot listing": float(os.getenv("EVENTS_HOLD_DAYS_LISTING", "30")),
                     "futures launch": float(os.getenv("EVENTS_HOLD_DAYS_LAUNCH", "30")),
                     "delisting": float(os.getenv("EVENTS_HOLD_DAYS_DELISTING", "7"))}
HOLD_DAYS = max(HOLD_DAYS_BY_KIND.values())
# 35% a leg since 2026-10-29 (was 10): ten pairs × two legs × 35% = 7× gross on the book, as asked. One event hitting its
# 25% pair stop costs ~9% of the book.
LEG_PCT = float(os.getenv("EVENTS_LEG_PCT", "35"))
MAX_OPEN = int(os.getenv("EVENTS_MAX_OPEN", "10"))        # month-long holds need more slots
DISASTER_PCT = float(os.getenv("EVENTS_DISASTER_PCT", "25"))
FUNDING_KILL = float(os.getenv("EVENTS_FUNDING_KILL", "0.005"))     # one settlement costing the short ≥ 0.5%
DELIST_MARGIN_H = 6
MAX_AGE_S = 3 * 3600                 # an announcement older than this when first seen is not traded
LAUNCH_WAIT_S = 3600
INITIAL_BALANCE = 10_000.0
HEDGE = "BTCUSDT"
SOURCES = ("Binance listings", "Binance delistings")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL, title TEXT, announced_ts REAL,
    opened_ts REAL, deadline_ts REAL, coin_entry REAL, btc_entry REAL, notional REAL,
    status TEXT NOT NULL, closed_ts REAL, coin_exit REAL, btc_exit REAL, close_reason TEXT,
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
    return _rows("SELECT * FROM events WHERE status='open' ORDER BY opened_ts")


def pending() -> list[dict]:
    return _rows("SELECT * FROM events WHERE status='pending' ORDER BY announced_ts")


def closed_positions() -> list[dict]:
    return _rows("SELECT * FROM events WHERE status='closed' ORDER BY closed_ts")


def spread_pct(p: dict, coin_px: float, btc_px: float) -> float:
    """The pair's move in its favour (short coin, long BTC), % of notional."""
    return (-(coin_px / p["coin_entry"] - 1) + (btc_px / p["btc_entry"] - 1)) * 100


def fill(price: float, buying: bool) -> float:
    return price * (1 + futures.SLIPPAGE) if buying else price * (1 - futures.SLIPPAGE)


def balance() -> float:
    from trading import capital
    return capital.initial("events", INITIAL_BALANCE) + sum(p["net"] or 0 for p in closed_positions())


def equity() -> float:
    return balance() + sum((p["mark_pct"] or 0) / 100 * (p["notional"] or 0) for p in open_positions())


def delist_deadline(title: str) -> float | None:
    m = re.search(r"\((\d{4}-\d{2}-\d{2})\)", title)
    if not m:
        return None
    day = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    return day - DELIST_MARGIN_H * 3600


# ── Finding events ─────────────────────────────────────────────────────────────

def _seen_key() -> str:
    return kv.key("events", "seen_through")


async def scan_announcements(now: float | None = None) -> list[dict]:
    """New Binance announcements since the last scan → pending/open rows. Returns what was queued."""
    from experiments import books
    from news import store
    from research.event_study_lab import STABLE, classify
    now = now or time.time()
    if not books.ENABLED or not book_switches.enabled(BOOK):
        return []
    since = kv.get_sync(_seen_key())
    if since is None:
        kv.set_sync(_seen_key(), now)            # first run: only announcements from now on
        return []
    known = {p["symbol"] for p in open_positions() + pending()}
    queued = []
    for it in store.items(since - 60):
        if it.get("source") not in SOURCES or not it.get("published"):
            continue
        if now - it["published"] > MAX_AGE_S:
            continue
        cl = classify(it["title"])
        if not cl:
            continue
        kind, tickers = cl
        for tk in tickers:
            sym = f"{tk}USDT"
            if tk in STABLE or sym == HEDGE or sym in known:
                continue
            deadline = it["published"] + HOLD_DAYS_BY_KIND[kind] * 86400
            if kind == "delisting":
                d = delist_deadline(it["title"])
                if d is not None:
                    deadline = min(deadline, d)
                if deadline - now < 2 * 3600:
                    continue
            row = {"id": uuid.uuid4().hex[:12], "symbol": sym, "kind": kind, "title": it["title"][:200],
                   "announced_ts": it["published"], "deadline_ts": deadline, "status": "pending",
                   "data": json.dumps({"news_id": it.get("id")})}
            _write("INSERT INTO events (id, symbol, kind, title, announced_ts, deadline_ts, status, data) "
                   "VALUES (:id,:symbol,:kind,:title,:announced_ts,:deadline_ts,:status,:data)", row)
            known.add(sym)
            queued.append(row)
            logger.info("Event queued: %s %s — %s", kind, sym, it["title"][:80])
    kv.set_sync(_seen_key(), now)                # advanced only once the window was fully processed
    return queued


async def _open(p: dict, now: float) -> dict | None:
    if len(open_positions()) >= MAX_OPEN:
        return None
    coin, btc = await monitor.live_price(p["symbol"]), await monitor.live_price(HEDGE)
    if not coin or not btc:
        return None
    if p["kind"] == "futures launch":
        data = json.loads(p.get("data") or "{}")
        first = data.get("price_seen_ts")
        if not first:
            data["price_seen_ts"] = now
            _write("UPDATE events SET data=? WHERE id=?", (json.dumps(data), p["id"]))
            return None
        if now - first < LAUNCH_WAIT_S:
            return None
        deadline = now + HOLD_DAYS_BY_KIND["futures launch"] * 86400
    else:
        deadline = p["deadline_ts"]
    from trading import capital
    notional = round(capital.sizing_equity("events", equity()) * LEG_PCT / 100, 2)
    _write("UPDATE events SET status='open', opened_ts=?, deadline_ts=?, coin_entry=?, btc_entry=?, notional=?, fees=? WHERE id=?",
           (now, deadline, fill(coin, buying=False), fill(btc, buying=True), notional,
            round(2 * notional * futures.TAKER_FEE, 4), p["id"]))
    logger.info("Event short opened: %s %s vs BTC ($%.0f a leg, until %s)", p["kind"], p["symbol"], notional,
                datetime.fromtimestamp(deadline, timezone.utc).strftime("%m-%d %H:%M"))
    return _rows("SELECT * FROM events WHERE id=?", (p["id"],))[0]


async def _funding(p: dict, until: float) -> tuple[float, float]:
    """(funding paid in $ on both legs, worst single settlement cost to the short as a fraction)."""
    from data.binance_client import fetch_funding_history
    start, paid, worst = int(p["opened_ts"] * 1000), 0.0, 0.0
    for symbol, side in ((p["symbol"], -1), (HEDGE, 1)):          # short coin: pays when the rate is negative
        try:
            for f in await fetch_funding_history(symbol, start):
                if f["time"] <= until * 1000:
                    paid += f["rate"] * p["notional"] * side
                    if side < 0:
                        worst = max(worst, -f["rate"])
        except Exception as e:
            logger.debug("Funding for %s unavailable: %s", symbol, e)
    return round(paid, 4), worst


def erase_closed(reason: str) -> int:
    """Remove closed rows with this close reason — for positions closed because the book cannot run on this instance
    (scripts/close_unpromoted.py), never for trades the book actually took."""
    with _lock:
        con = _db()
        n = con.execute("DELETE FROM events WHERE status='closed' AND close_reason=?", (reason,)).rowcount
        con.commit()
    logger.warning("Events: erased %d closed rows with reason %r", n, reason)
    return n


async def close(p: dict, coin_px: float, btc_px: float, reason: str, now: float | None = None) -> dict:
    now = now or time.time()
    coin_exit, btc_exit = fill(coin_px, buying=True), fill(btc_px, buying=False)
    gross = round(spread_pct(p, coin_exit, btc_exit) / 100 * p["notional"], 4)
    fees = round((p["fees"] or 0) + 2 * p["notional"] * futures.TAKER_FEE, 4)
    funding, _ = await _funding(p, now)
    net = round(gross - fees - funding, 4)
    _write("UPDATE events SET status='closed', closed_ts=?, coin_exit=?, btc_exit=?, close_reason=?, fees=?, funding=?, "
           "gross=?, net=? WHERE id=?", (now, coin_exit, btc_exit, reason, fees, funding, gross, net, p["id"]))
    logger.info("Event short closed: %s %s — %s, net $%.2f", p["kind"], p["symbol"], reason, net)
    return {**p, "status": "closed", "closed_ts": now, "close_reason": reason, "gross": gross, "fees": fees,
            "funding": funding, "net": net}


_last_funding_check = {"at": 0.0}


async def tick(now: float | None = None) -> list[dict]:
    """Every minute: queue new announcements, open what's ready, mark and manage what's open."""
    from experiments import books
    if not books.ENABLED:
        return []
    now = now or time.time()
    await scan_announcements(now)
    if book_switches.enabled(BOOK):
        for p in pending():
            too_late = (p["kind"] != "futures launch" and p["deadline_ts"] - now < 2 * 3600) or \
                       (p["kind"] == "futures launch" and now - p["announced_ts"] > 7 * 86400)     # never started trading
            if too_late:
                _write("UPDATE events SET status='closed', closed_ts=?, close_reason='missed', net=0 WHERE id=?", (now, p["id"]))
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
        _write("UPDATE events SET mark_pct=?, best_pct=MAX(best_pct, ?), worst_pct=MIN(worst_pct, ?) WHERE id=?",
               (round(mark, 4), round(mark, 4), round(mark, 4), p["id"]))
        if mark <= -DISASTER_PCT:
            closed.append(await close(p, px, btc, "disaster_stop", now))
        elif now >= p["deadline_ts"]:
            closed.append(await close(p, px, btc, "hold_complete", now))
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
    for p in _rows("SELECT * FROM events WHERE status IN ('open','closed') AND opened_ts IS NOT NULL"):
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
            "outcome": None if is_open else ("win" if net > 0 else "loss"), "timeframe": f"{HOLD_DAYS_BY_KIND.get(p['kind'], HOLD_DAYS):g}d event",
            "note": f"{p['kind']}: {p['title'] or ''}"[:160], "mfe_r": round((p["best_pct"] or 0) / DISASTER_PCT, 2),
            "quantity": None, "liquidation": None, "tp_set": False,
            "opened_at": datetime.fromtimestamp(p["opened_ts"], timezone.utc).isoformat(),
        })
    return out


def status() -> dict:
    return {"open": len(open_positions()), "pending": [{"symbol": p["symbol"], "kind": p["kind"], "title": p["title"]} for p in pending()],
            "closed": len(closed_positions()), "equity": round(equity(), 2), "config": config()}


def config() -> dict:
    return {"hold_days": HOLD_DAYS_BY_KIND, "leg_pct": LEG_PCT, "max_open": MAX_OPEN, "disaster_pct": DISASTER_PCT,
            "funding_kill": FUNDING_KILL, "hedge": HEDGE, "initial_balance": INITIAL_BALANCE}
