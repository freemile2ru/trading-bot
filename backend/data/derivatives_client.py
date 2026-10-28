"""Perpetual-futures positioning from Binance's public endpoints (no key needed).

Price tells you where a coin went; positioning tells you who is holding it and how
crowded that side is. Crypto moves are often driven by leverage unwinding, which a
crowded long (high funding, open interest piling in while price stalls) sets up.

  funding       — what longs pay shorts every 8h. 0.01% is the neutral baseline.
  open interest — total open contracts; rising OI with rising price is new money,
                  rising OI with falling price is shorts piling in.
  taker ratio   — aggressive buy volume ÷ sell volume.
  long/short    — share of accounts (all, and top traders by position) that are net long.
  basis         — perpetual price vs spot index, in %.

The /futures/data statistics only go back 30 days on Binance, so they can be studied only
from what the scanner records going forward; funding history is available for years.
"""

import asyncio
import statistics
import time

from data.http import get_json

FAPI = "https://fapi.binance.com"


def _pct_change(new: float | None, old: float | None) -> float | None:
    return (new / old - 1) * 100 if new and old else None


async def _hist(path: str, symbol: str, period: str, limit: int) -> list[dict]:
    return await get_json(f"{FAPI}/futures/data/{path}", params={"symbol": symbol, "period": period, "limit": limit})


async def fetch_positioning(symbol: str) -> dict:
    """Current positioning snapshot for one USDT perpetual. Missing pieces are None."""
    now_ms = int(time.time() * 1000)
    premium, funding, oi, taker, accounts, top = await asyncio.gather(
        get_json(f"{FAPI}/fapi/v1/premiumIndex", params={"symbol": symbol}),
        get_json(f"{FAPI}/fapi/v1/fundingRate",
                 params={"symbol": symbol, "startTime": now_ms - 30 * 86_400_000, "limit": 1000}),
        _hist("openInterestHist", symbol, "1h", 169),
        _hist("takerlongshortRatio", symbol, "1h", 24),
        _hist("globalLongShortAccountRatio", symbol, "1h", 1),
        _hist("topLongShortPositionRatio", symbol, "1h", 1),
        return_exceptions=True,
    )
    out: dict = {"symbol": symbol, "fetched_at": time.time()}

    if isinstance(premium, dict) and premium.get("markPrice"):
        mark, index = float(premium["markPrice"]), float(premium["indexPrice"] or 0)
        out["funding_rate"] = float(premium["lastFundingRate"])
        out["basis_pct"] = (mark / index - 1) * 100 if index else None

    if isinstance(funding, list) and funding:
        rates = [float(r["fundingRate"]) for r in funding]
        last7 = [float(r["fundingRate"]) for r in funding if int(r["fundingTime"]) >= now_ms - 7 * 86_400_000]
        out["funding_7d_avg"] = statistics.fmean(last7) if last7 else None
        out["funding_30d_avg"] = statistics.fmean(rates)
        sd = statistics.pstdev(rates) if len(rates) > 5 else 0
        out["funding_z"] = (out["funding_7d_avg"] - out["funding_30d_avg"]) / sd if sd and last7 else None

    if isinstance(oi, list) and oi:
        vals = [float(r["sumOpenInterestValue"]) for r in oi]
        out["oi_usd"] = vals[-1]
        out["oi_change_24h_pct"] = _pct_change(vals[-1], vals[-25] if len(vals) >= 25 else None)
        out["oi_change_7d_pct"] = _pct_change(vals[-1], vals[0] if len(vals) >= 160 else None)

    if isinstance(taker, list) and taker:
        buy = sum(float(r["buyVol"]) for r in taker)
        sell = sum(float(r["sellVol"]) for r in taker)
        out["taker_buy_sell_24h"] = buy / sell if sell else None

    if isinstance(accounts, list) and accounts:
        out["long_account_pct"] = float(accounts[-1]["longAccount"]) * 100
    if isinstance(top, list) and top:
        out["top_trader_long_pct"] = float(top[-1]["longAccount"]) * 100

    return out


# ── History (research) ─────────────────────────────────────────────────────────
# Research downloads page through a lot of history. A 1500-row klines page costs 10 of
# Binance's 2400/min request weight per IP — shared with the live app — so pages are
# spaced to stay near half the limit however many downloads run concurrently.

_PAGE_GAP_S = 0.5
_page_lock = asyncio.Lock()
_last_page = 0.0


async def _paced():
    global _last_page
    async with _page_lock:
        wait = _last_page + _PAGE_GAP_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_page = time.monotonic()


async def _page(url: str, params: dict, attempts: int = 4) -> list:
    """One paced history page; retries Binance's occasional 408/5xx on long downloads."""
    import httpx

    for i in range(attempts):
        await _paced()
        try:
            return await get_json(url, params=params, timeout=20.0)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (408, 500, 502, 503, 504) or i == attempts - 1:
                raise
            await asyncio.sleep(2 ** (i + 1))
    return []

async def fetch_funding_range(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Every funding settlement in [start, end], paging 1000 at a time."""
    out, cursor = [], start_ms
    while cursor < end_ms:
        rows = await _page(f"{FAPI}/fapi/v1/fundingRate",
                           {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000})
        if not rows:
            break
        out += [{"time": int(r["fundingTime"]), "rate": float(r["fundingRate"])} for r in rows]
        cursor = int(rows[-1]["fundingTime"]) + 1
        if len(rows) < 1000:
            break
    return out


async def fetch_klines_range(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    """Perpetual klines [open_time, open, high, low, close, volume, quote_volume] over a range."""
    out, cursor = [], start_ms
    while cursor < end_ms:
        rows = await _page(f"{FAPI}/fapi/v1/klines",
                           {"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms,
                            "limit": 1500})
        if not rows:
            break
        out += [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7])]
                for r in rows]
        cursor = int(rows[-1][0]) + 1
        if len(rows) < 1500:
            break
    return out
