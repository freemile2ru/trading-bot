"""Who may trade live, the checks every live order passes, the audit log, and the kill switch.

Mode per system ("paper" | "live") lives in state.db so it survives restarts. A system is live
only while ALL of these hold — checked on every entry, not just when the toggle is flipped:
  * LIVE_TRADING_ALLOWED=true in .env
  * its mode is "live"
  * it has API keys for the configured environment

data/live.db keeps:
  audit   every request to Binance and its response (keys and signatures never stored)
  orders  every order we sent, keyed by our client id, with its latest known state
  events  mode switches, guard refusals, kill-switch runs, reconciliation notes
"""

import json
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from live import config as LC
from live.binance import BinanceError, BinanceFutures
from storage import state as kv
from storage.db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (ts REAL, system TEXT, method TEXT, path TEXT, params TEXT, status INTEGER,
                                  response TEXT, ms INTEGER);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts);
CREATE TABLE IF NOT EXISTS orders (cid TEXT PRIMARY KEY, system TEXT, symbol TEXT, purpose TEXT, side TEXT,
                                   type TEXT, qty REAL, price REAL, status TEXT, exchange_id TEXT, algo INTEGER,
                                   ref TEXT, created REAL, updated REAL, data TEXT);
CREATE INDEX IF NOT EXISTS ix_orders_sys ON orders(system, status);
CREATE TABLE IF NOT EXISTS events (ts REAL, system TEXT, kind TEXT, data TEXT);
"""

_lock = threading.Lock()
_con = None
_clients: dict[str, BinanceFutures] = {}
# Callbacks registered by the systems: today's realized live P&L, and "am I flat?" (scalper).
_daily_pnl: dict[str, Callable[[], float]] = {}
_is_flat: dict[str, Callable[[], bool]] = {}
_flatten: dict[str, Callable[[str], list[str]]] = {}


class LiveBlocked(Exception):
    """A live action was refused before anything was sent to the exchange."""


def _db():
    global _con
    if _con is None:
        _con = connect(LC.DB)
        _con.executescript(SCHEMA)
        _con.commit()
    return _con


def _write(sql: str, params) -> None:
    with _lock:
        _db().execute(sql, params)
        _db().commit()


AUDIT_READS_DAYS = 30
_audit_writes = [0]


def audit(row: dict) -> None:
    """Every request is logged. Orders, cancels and errors keep their full response forever; a successful
    read (GET 200 — the account poll alone runs ~6,000 times a day at ~4 KB) keeps only its timing, and
    read rows are pruned after AUDIT_READS_DAYS. Until 2026-10-19 every read stored its body: 540 MB."""
    read_ok = row["method"] == "GET" and row["status"] == 200
    response = "" if read_ok else json.dumps(row["response"], default=str)[:4000]
    _write("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)",
           (row["ts"], row["system"], row["method"], row["path"], json.dumps(row["params"], default=str),
            row["status"], response, row["ms"]))
    _audit_writes[0] += 1
    if _audit_writes[0] % 2000 == 1:
        _write("DELETE FROM audit WHERE method='GET' AND status=200 AND ts < ?", (time.time() - AUDIT_READS_DAYS * 86400,))


def event(system: str, kind: str, data: dict) -> None:
    _write("INSERT INTO events VALUES (?,?,?,?)", (time.time(), system, kind, json.dumps(data, default=str)))


def entry_owner(symbol: str) -> str | None:
    """The system that placed the most recent entry order on this symbol (from our own order log)."""
    with _lock:
        row = _db().execute("SELECT system FROM orders WHERE symbol=? AND purpose='entry' ORDER BY created DESC LIMIT 1",
                            (symbol,)).fetchone()
    return row[0] if row else None


def record_order(cid: str, system: str, symbol: str, purpose: str, side: str, type_: str, qty: float,
                 price: float | None, status: str, exchange_id=None, algo: bool = False, ref: str | None = None,
                 data: dict | None = None) -> None:
    now = time.time()
    _write("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cid) DO UPDATE SET "
           "status=excluded.status, exchange_id=COALESCE(excluded.exchange_id, orders.exchange_id), "
           "updated=excluded.updated, data=excluded.data",
           (cid, system, symbol, purpose, side, type_, qty, price, status,
            None if exchange_id is None else str(exchange_id), int(algo), ref, now, now, json.dumps(data or {}, default=str)))


def rows(sql: str, params=()) -> list[dict]:
    with _lock:
        cur = _db().execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── Modes ──────────────────────────────────────────────────────────────────────

def mode(system: str) -> str:
    return (kv.get_sync(kv.key("live", "mode", system)) or {}).get("mode", "paper")


def is_live(system: str) -> bool:
    return LC.ALLOWED and mode(system) == "live" and LC.keys(system) is not None


def client(system: str) -> BinanceFutures:
    k = LC.keys(system)
    if not k:
        raise LiveBlocked(f"no Binance {LC.ENV} API key configured for {system}")
    c = _clients.get(system)
    # A new key in .env (or a different environment) replaces the cached client.
    if c is None or c.rest != LC.REST.rstrip("/") or c._key != k[0]:
        c = _clients[system] = BinanceFutures(*k, rest=LC.REST, audit=audit, system=system)
    return c


def confirmation_phrase(system: str) -> str:
    return f"LIVE {system.upper()} {LC.ENV.upper()}"


def set_mode(system: str, new_mode: str, confirm: str = "") -> dict:
    if system not in LC.SYSTEMS or new_mode not in ("paper", "live"):
        raise LiveBlocked("unknown system or mode")
    if new_mode == "live":
        if not LC.ALLOWED:
            raise LiveBlocked("live trading is disabled — set LIVE_TRADING_ALLOWED=true in .env first")
        if confirm.strip() != confirmation_phrase(system):
            raise LiveBlocked(f'type "{confirmation_phrase(system)}" to confirm')
        c = client(system)
        try:
            if c.hedge_mode():
                raise LiveBlocked("the Binance account is in hedge mode — switch it to one-way mode")
            eq = c.equity()
        except BinanceError as e:
            raise LiveBlocked(f"Binance rejected the keys or request: {e}") from e
    if system in _is_flat and not _is_flat[system]():
        raise LiveBlocked(f"{system} has open positions or orders — it can only switch mode when flat")
    kv.set_sync(kv.key("live", "mode", system), {"mode": new_mode, "at": time.time()})
    event(system, "mode", {"mode": new_mode, "env": LC.ENV})
    return {"system": system, "mode": new_mode, **({"account": eq} if new_mode == "live" else {})}


def register(system: str, daily_pnl: Callable[[], float] | None = None,
             is_flat: Callable[[], bool] | None = None, flatten: Callable[[str], list[str]] | None = None) -> None:
    if daily_pnl:
        _daily_pnl[system] = daily_pnl
    if is_flat:
        _is_flat[system] = is_flat
    if flatten:
        _flatten[system] = flatten


# ── Pre-trade guard ────────────────────────────────────────────────────────────

def _today_start() -> float:
    d = datetime.now(timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def guard_entry(system: str, symbol: str, leverage: int, others_holding: Callable[[str, str], bool] | None = None) -> None:
    """Raise LiveBlocked if a new live entry must not be sent. Exits are never blocked."""
    if not LC.ALLOWED:
        raise LiveBlocked("LIVE_TRADING_ALLOWED is off")
    if mode(system) != "live":
        raise LiveBlocked(f"{system} is in paper mode")
    if leverage > LC.MAX_LEVERAGE:
        raise LiveBlocked(f"leverage {leverage}x above the live cap of {LC.MAX_LEVERAGE}x")
    halted = kv.get_sync(kv.key("live", "halt", system))
    if halted:
        raise LiveBlocked(f"halted: {halted.get('reason')}")
    if system in _daily_pnl:
        try:
            equity = client(system).equity()["equity"]
        except BinanceError as e:
            raise LiveBlocked(f"can't read the account before trading: {e}") from e
        pnl = _daily_pnl[system]()
        if equity > 0 and pnl <= -equity * LC.DAILY_LOSS_PCT / 100:
            until = _today_start() + 86400
            kv.set_sync(kv.key("live", "halt", system),
                        {"reason": f"daily loss limit {LC.DAILY_LOSS_PCT}% hit (${pnl:,.2f}) — resumes at 00:00 UTC"},
                        ttl=until - time.time())
            event(system, "halt", {"pnl": pnl, "equity": equity})
            raise LiveBlocked(f"daily loss limit {LC.DAILY_LOSS_PCT}% hit")
    # Two systems on one Binance account share a single one-way position per symbol.
    acct = LC.account_id(system)
    for other in LC.SYSTEMS:
        if other != system and LC.account_id(other) == acct and others_holding and others_holding(other, symbol):
            raise LiveBlocked(f"{symbol} is already held live by {other} on the same Binance account "
                              f"(give each system its own sub-account key to trade the same coin)")


# ── Kill switch ────────────────────────────────────────────────────────────────

def kill_switch(reason: str = "manual kill switch") -> dict:
    """Every system back to paper, then each flattens its live positions and orders.
    Runs even when LIVE_TRADING_ALLOWED is off — closing risk is never blocked."""
    out = {}
    for system in LC.SYSTEMS:
        kv.set_sync(kv.key("live", "mode", system), {"mode": "paper", "at": time.time(), "by": "kill switch"})
        closed: list[str] = []
        if system in _flatten and LC.keys(system):
            try:
                closed = _flatten[system](reason)
            except Exception as e:
                closed = [f"error: {e}"]
        out[system] = closed
    event("all", "kill_switch", {"reason": reason, "result": out})
    return out


def status() -> dict:
    out = {"allowed": LC.ALLOWED, "env": LC.ENV, "rest": LC.REST, "daily_loss_pct": LC.DAILY_LOSS_PCT,
           "max_leverage": LC.MAX_LEVERAGE, "systems": {}}
    for s in LC.SYSTEMS:
        halt = kv.get_sync(kv.key("live", "halt", s))
        out["systems"][s] = {"mode": mode(s), "live": is_live(s), "has_keys": LC.keys(s) is not None,
                             "account": LC.account_id(s), "confirm": confirmation_phrase(s),
                             "halted": halt.get("reason") if halt else None,
                             "flat": _is_flat[s]() if s in _is_flat else None}
    ids = [out["systems"][s]["account"] for s in LC.SYSTEMS if out["systems"][s]["account"]]
    out["shared_account"] = len(ids) != len(set(ids))
    return out
