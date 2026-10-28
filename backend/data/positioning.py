"""Binance Futures positioning data, logged so it can be tested later.

Binance publishes, per perp and 5-minute bucket: the top accounts' long/short *position* ratio, the top accounts'
long/short *account* ratio, all accounts' long/short ratio, and the taker buy/sell volume ratio — but keeps only
30 days of it. The published evidence for these as signals is weak, so nothing trades on them; this just records
them hourly (data/positioning.db) so that in a few months there is a history to run a lab on.
"""
from __future__ import annotations

import logging
import os
import sqlite3

from storage.db import DATA_DIR

logger = logging.getLogger("data.positioning")

DB = os.path.join(DATA_DIR, "positioning.db")
FAPI = "https://fapi.binance.com/futures/data"
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT", "SUIUSDT", "LINKUSDT", "AVAXUSDT", "HYPEUSDT", "1000PEPEUSDT"]
METRICS = {"top_position": ("topLongShortPositionRatio", "longShortRatio"), "top_account": ("topLongShortAccountRatio", "longShortRatio"),
           "global_account": ("globalLongShortAccountRatio", "longShortRatio"), "taker": ("takerlongshortRatio", "buySellRatio")}
SCHEMA = "CREATE TABLE IF NOT EXISTS positioning (symbol TEXT NOT NULL, metric TEXT NOT NULL, ts INTEGER NOT NULL, value REAL NOT NULL, PRIMARY KEY (symbol, metric, ts))"


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    con.execute(SCHEMA)
    return con


async def tick(now: float | None = None) -> int:
    """Fetch the last ~2 hours of 5-minute rows for every coin and metric; returns rows written."""
    from data.http import get_json
    rows = []
    for sym in COINS:
        for metric, (path, field) in METRICS.items():
            try:
                data = await get_json(f"{FAPI}/{path}", params={"symbol": sym, "period": "5m", "limit": 24}, timeout=15.0)
            except Exception as e:
                logger.debug("positioning %s %s unavailable: %s", sym, metric, e)
                continue
            for r in data or []:
                try:
                    rows.append((sym, metric, int(r["timestamp"]) // 1000, float(r[field])))
                except (KeyError, TypeError, ValueError):
                    continue
    if not rows:
        return 0
    con = _con()
    with con:
        cur = con.executemany("INSERT OR IGNORE INTO positioning VALUES (?,?,?,?)", rows)
        n = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)
    con.close()
    return n


def status() -> dict:
    try:
        con = _con()
        n, first, last = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM positioning").fetchone()
        con.close()
    except sqlite3.Error:
        n, first, last = 0, None, None
    return {"rows": n, "since": first, "through": last, "coins": COINS, "metrics": list(METRICS), "db": DB}


def series(symbol: str, metric: str, since: float | None = None) -> list[tuple[int, float]]:
    con = _con()
    rows = con.execute("SELECT ts, value FROM positioning WHERE symbol=? AND metric=? AND ts>=? ORDER BY ts",
                       (symbol, metric, int(since or 0))).fetchall()
    con.close()
    return rows
