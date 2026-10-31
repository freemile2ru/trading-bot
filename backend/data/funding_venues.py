"""Funding rates on Binance and Hyperliquid for the same coins, logged hourly for the cross-venue funding lab.

Carry is the one family that passed its holdout. The next step in that family is long the perp on the venue paying
less and short it where funding is higher — delta-neutral, like carry. Hyperliquid settles hourly and its rates
often sit well apart from Binance's on mid-caps; whether the gap clears the round-trip cost is a data question, so
this records both venues' rates (both expressed per 8 hours) and the gap (data/funding_venues.db). Nothing trades on it.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

from storage.db import DATA_DIR

logger = logging.getLogger("data.funding_venues")

DB = os.path.join(DATA_DIR, "funding_venues.db")
SCHEMA = ("CREATE TABLE IF NOT EXISTS funding_venues (ts INTEGER NOT NULL, coin TEXT NOT NULL, binance_8h REAL, hl_8h REAL, "
          "gap_8h REAL, binance_mark REAL, hl_mark REAL, PRIMARY KEY (ts, coin))")


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    con.execute(SCHEMA)
    return con


def binance_rates(premium_rows: list[dict]) -> dict[str, tuple[float, float]]:
    """{coin: (funding per 8h, mark)} from /fapi/v1/premiumIndex (USDT perps only; 1000X contracts keep their prefix)."""
    out = {}
    for r in premium_rows:
        sym = r.get("symbol") or ""
        if not sym.endswith("USDT") or "_" in sym:
            continue
        try:
            out[sym[:-4]] = (float(r.get("lastFundingRate") or 0), float(r.get("markPrice") or 0))
        except (TypeError, ValueError):
            continue
    return out


def hyperliquid_rates(meta_and_ctxs: list) -> dict[str, tuple[float, float]]:
    """{coin: (funding per 8h, mark)} from the metaAndAssetCtxs reply: [meta, [ctx…]]; ctx.funding is per hour."""
    out = {}
    try:
        universe = meta_and_ctxs[0]["universe"]
        ctxs = meta_and_ctxs[1]
    except (IndexError, KeyError, TypeError):
        return out
    for asset, ctx in zip(universe, ctxs):
        try:
            name = str(asset["name"])
            coin = name[1:] if name.startswith("k") and name[1:].isupper() else name     # kPEPE → PEPE
            out[coin] = (float(ctx.get("funding") or 0) * 8, float(ctx.get("markPx") or 0))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def rows_for(ts: int, binance: dict, hl: dict) -> list[tuple]:
    """One row per coin both venues list; Binance's 1000X contracts are matched to the plain coin."""
    out = []
    for b_coin, (b_rate, b_mark) in binance.items():
        coin = b_coin[4:] if b_coin.startswith("1000") else b_coin
        if coin not in hl:
            continue
        h_rate, h_mark = hl[coin]
        out.append((ts, coin, b_rate, h_rate, round(b_rate - h_rate, 8), b_mark, h_mark))
    return out


async def tick() -> int:
    """Hourly: fetch both venues, store the overlap. Returns rows written."""
    from data.http import get_json
    from data.hyperliquid_client import _post
    ts = int(time.time() // 3600 * 3600)
    try:
        premium = await get_json("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=20)
        ctxs = await _post({"type": "metaAndAssetCtxs"})
    except Exception as e:
        logger.warning("Funding venues: fetch failed (%s)", e)
        return 0
    rows = rows_for(ts, binance_rates(premium if isinstance(premium, list) else []), hyperliquid_rates(ctxs if isinstance(ctxs, list) else []))
    if not rows:
        return 0
    con = _con()
    with con:
        con.executemany("INSERT OR REPLACE INTO funding_venues VALUES (?,?,?,?,?,?,?)", rows)
    con.close()
    return len(rows)


def status() -> dict:
    try:
        con = _con()
        n, coins, first, last = con.execute("SELECT COUNT(*), COUNT(DISTINCT coin), MIN(ts), MAX(ts) FROM funding_venues").fetchone()
        wide = con.execute("SELECT coin, AVG(ABS(gap_8h)) AS g FROM funding_venues WHERE ts >= ? GROUP BY coin ORDER BY g DESC LIMIT 5",
                           (int(time.time()) - 7 * 86400,)).fetchall()
        con.close()
    except sqlite3.Error:
        return {"rows": 0}
    return {"rows": n, "coins": coins, "since": first, "through": last,
            "widest_gaps_7d": [{"coin": c, "avg_abs_gap_8h_pct": round(g * 100, 4)} for c, g in wide]}
