"""Binance public API — OHLCV, order book, funding rates, ticker."""

import asyncio
import httpx
from data.http import get_with_client

BASE = "https://api.binance.com/api/v3"
FAPI = "https://fapi.binance.com/fapi/v1"


async def fetch_ohlcv(
    symbol: str,
    interval: str = "1h",
    limit: int = 200,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Return OHLCV candles. interval: 1m 5m 15m 1h 4h 1d 1w."""
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=12)
    try:
        r = await get_with_client(
            client, f"{BASE}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        return [
            {
                "open_time": c[0],
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "close_time": c[6],
                "quote_volume": float(c[7]),
                "trades": int(c[8]),
            }
            for c in r.json()
        ]
    finally:
        if own:
            await client.aclose()


async def fetch_ticker_24h(
    symbol: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=12)
    try:
        r = await get_with_client(client, f"{BASE}/ticker/24hr", params={"symbol": symbol})
        d = r.json()
        return {
            "symbol": d["symbol"],
            "price": float(d["lastPrice"]),
            "price_change_pct": float(d["priceChangePercent"]),
            "high": float(d["highPrice"]),
            "low": float(d["lowPrice"]),
            "volume": float(d["volume"]),
            "quote_volume": float(d["quoteVolume"]),
        }
    finally:
        if own:
            await client.aclose()


async def fetch_order_book(
    symbol: str,
    limit: int = 20,
    client: httpx.AsyncClient | None = None,
) -> dict:
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=12)
    try:
        r = await get_with_client(client, f"{BASE}/depth", params={"symbol": symbol, "limit": limit})
        d = r.json()
        bids = [[float(p), float(q)] for p, q in d["bids"]]
        asks = [[float(p), float(q)] for p, q in d["asks"]]
        total_bid = sum(p * q for p, q in bids)
        total_ask = sum(p * q for p, q in asks)
        return {
            "bids": bids,
            "asks": asks,
            "bid_ask_ratio": round(total_bid / total_ask, 4) if total_ask else 1.0,
            "spread_pct": round((asks[0][0] - bids[0][0]) / bids[0][0] * 100, 4) if bids and asks else 0,
        }
    finally:
        if own:
            await client.aclose()


async def fetch_funding_rate(
    symbol: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Futures funding rate — positive means longs pay shorts (bearish bias)."""
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=12)
    try:
        r = await get_with_client(client, f"{FAPI}/premiumIndex", params={"symbol": symbol})
        d = r.json()
        return {
            "symbol": d["symbol"],
            "mark_price": float(d["markPrice"]),
            "index_price": float(d["indexPrice"]),
            "funding_rate": float(d["lastFundingRate"]),
            "next_funding_time": d["nextFundingTime"],
        }
    except Exception:
        return {}
    finally:
        if own:
            await client.aclose()


async def fetch_market_snapshot(symbol: str) -> dict:
    """Gather all Binance data for a symbol in parallel."""
    async with httpx.AsyncClient(timeout=12) as client:
        ohlcv_tasks = {
            interval: fetch_ohlcv(symbol, interval, 200, client)
            for interval in ["15m", "30m", "1h", "4h"]
        }
        results = await asyncio.gather(
            *ohlcv_tasks.values(),
            fetch_ticker_24h(symbol, client),
            fetch_order_book(symbol, 20, client),
            return_exceptions=True,
        )

    intervals = list(ohlcv_tasks.keys())
    snapshot: dict = {"symbol": symbol, "ohlcv": {}, "ticker": {}, "order_book": {}}
    for i, interval in enumerate(intervals):
        snapshot["ohlcv"][interval] = results[i] if not isinstance(results[i], Exception) else []
    n = len(intervals)
    snapshot["ticker"] = results[n] if not isinstance(results[n], Exception) else {}
    snapshot["order_book"] = results[n + 1] if not isinstance(results[n + 1], Exception) else {}
    return snapshot
