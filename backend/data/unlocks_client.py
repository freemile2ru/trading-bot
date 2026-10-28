"""Token unlock calendar — CoinMarketCap's public unlock page, read from the data it ships with the HTML.

There is no free unlock API (DefiLlama's is Pro-only, CryptoRank/Tokenomist/DropsTab are paid), but CMC renders
https://coinmarketcap.com/token-unlocks/ server-side with a `tokenUnlockList` (100 coins a page, ~14 pages) in its
Next.js payload: each coin's NEXT unlock — timestamp, token amount, USD value, % of supply — and the allocation
lines that make it up (name + cliff/linear). That's enough to find the cliff unlocks the research is about.

Refreshed a few times a day and cached in the kv store; a failed refresh keeps the last good list.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from storage import state as kv

logger = logging.getLogger("data.unlocks")

URL = "https://coinmarketcap.com/token-unlocks/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml"}
CACHE_S = 6 * 3600
MAX_PAGES = 20
INSIDER = ("team", "investor", "seed", "private", "strategic", "advisor", "backer", "core contributor", "contributor",
           "angel", "founder", "vc", "series", "pre-seed", "presale", "pre-sale", "early", "partner")


def _key() -> str:
    return kv.key("unlocks", "cmc")


def parse_page(html: str) -> tuple[list[dict], int]:
    """(tokenUnlockList rows, totalCount) from a rendered page, or ([], 0)."""
    i = html.find("__NEXT_DATA__")
    if i < 0:
        return [], 0
    j, k = html.find(">", i), html.find("</script>", i)
    try:
        d = json.loads(html[j + 1:k])
    except ValueError:
        return [], 0

    def walk(o):
        if isinstance(o, dict):
            if "tokenUnlockList" in o:
                return o
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r
        return None

    found = walk(d)
    if not found:
        return [], 0
    return list(found.get("tokenUnlockList") or []), int(found.get("totalCount") or 0)


def normalise(row: dict) -> dict | None:
    """One coin's next unlock as a flat record, or None when it has no dated unlock."""
    nx = row.get("nextUnlocked") or {}
    if not nx.get("date"):
        return None
    det = row.get("nextUnlockedDetail") or []
    total = sum(float(x.get("tokenAmount") or 0) for x in det) or float(nx.get("tokenAmount") or 0) or 1.0
    cliff = sum(float(x.get("tokenAmount") or 0) for x in det if (x.get("vestingType") or "").lower() == "cliff")
    insider = sum(float(x.get("tokenAmount") or 0) for x in det
                  if any(w in (x.get("allocationName") or "").lower() for w in INSIDER))
    q = (row.get("quotes") or [{}])[0]
    return {"symbol": row.get("symbol"), "slug": row.get("slug"), "name": row.get("name"), "ts": nx["date"] / 1000,
            "pct": float(nx.get("tokenAmountPercentage") or 0), "usd": float(nx.get("tokenAmountUsd") or 0),
            "tokens": float(nx.get("tokenAmount") or 0), "cliff_share": round(cliff / total, 3),
            "insider_share": round(insider / total, 3),
            "allocations": [{"name": x.get("allocationName"), "type": x.get("vestingType"), "tokens": x.get("tokenAmount")} for x in det][:12],
            "market_cap": q.get("marketCap"), "volume_24h": q.get("volume24h"), "price": q.get("price"),
            "circulating": row.get("circulatingSupply"), "cmc_rank": row.get("cmcRank")}


async def _fetch_page(n: int) -> str:
    from data.http import get_text
    return await get_text(URL + (f"?page={n}" if n > 1 else ""), headers=HEADERS)


async def refresh(now: float | None = None, force: bool = False) -> list[dict]:
    now = now or time.time()
    cached = kv.get_sync(_key())
    if cached and not force and now - cached.get("at", 0) < CACHE_S:
        return cached["rows"]
    rows: list[dict] = []
    try:
        first, total = parse_page(await _fetch_page(1))
        rows += first
        pages = min(MAX_PAGES, (total + 99) // 100)
        for n in range(2, pages + 1):
            page_rows, _ = parse_page(await _fetch_page(n))
            if not page_rows:
                break
            rows += page_rows
            await asyncio.sleep(0.3)
    except Exception as e:
        logger.warning("Unlock calendar refresh failed: %s", e)
        return (cached or {}).get("rows") or []
    out = [r for r in (normalise(x) for x in rows) if r]
    if len(out) < 50 and cached:                               # a broken page shouldn't wipe the last good list
        logger.warning("Unlock calendar looks truncated (%d rows) — keeping the cached one", len(out))
        return cached["rows"]
    kv.set_sync(_key(), {"at": now, "rows": out}, 3 * 86400)
    try:
        snapshot(now, out)
    except Exception as e:
        logger.debug("unlock snapshot failed: %s", e)
    logger.info("Unlock calendar refreshed: %d coins with a dated unlock", len(out))
    return out


# ── The record (data/unlocks.db): the calendar as it stood each day, so unlocks can be studied on our own history ──
SNAP_DB = None      # resolved lazily from storage.db.DATA_DIR
SNAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS unlock_calendar (
    day TEXT NOT NULL, symbol TEXT NOT NULL, unlock_ts REAL NOT NULL, pct REAL, usd REAL, tokens REAL, cliff_share REAL,
    insider_share REAL, price REAL, market_cap REAL, volume_24h REAL, allocations TEXT, PRIMARY KEY (day, symbol, unlock_ts))"""


def _snap_con():
    import os
    import sqlite3
    from storage.db import DATA_DIR
    con = sqlite3.connect(os.path.join(DATA_DIR, "unlocks.db"), timeout=10)
    con.execute(SNAP_SCHEMA)
    return con


def snapshot(now: float, rows: list[dict]) -> int:
    """One row per (day, coin, unlock) — a refresh later the same day changes nothing."""
    import datetime as dt
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d")
    con = _snap_con()
    with con:
        con.executemany("INSERT OR IGNORE INTO unlock_calendar VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(day, r["symbol"], r["ts"], r["pct"], r["usd"], r["tokens"], r["cliff_share"], r["insider_share"], r.get("price"),
                          r.get("market_cap"), r.get("volume_24h"), json.dumps(r.get("allocations") or [])) for r in rows])
    con.close()
    return len(rows)


def snapshot_status() -> dict:
    try:
        con = _snap_con()
        n, days, first, last = con.execute("SELECT COUNT(*), COUNT(DISTINCT day), MIN(day), MAX(day) FROM unlock_calendar").fetchone()
        con.close()
        return {"rows": n, "days": days, "since": first, "through": last}
    except Exception:
        return {"rows": 0, "days": 0, "since": None, "through": None}


def cached() -> list[dict]:
    return (kv.get_sync(_key()) or {}).get("rows") or []


def refreshed_at() -> float | None:
    return (kv.get_sync(_key()) or {}).get("at")


_NUM = re.compile(r"^\d+")


def perp_for(symbol: str, perps: set[str]) -> str | None:
    """The Binance USDT perp for a CMC symbol (1000-multiplied listings included), or None."""
    s = (symbol or "").upper().replace(" ", "")
    if not s or "(" in s:
        return None
    for cand in (f"{s}USDT", f"1000{s}USDT"):
        if cand in perps:
            return cand
    return None
