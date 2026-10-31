"""Binance public API — OHLCV, order book, funding rates, ticker."""

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


async def fetch_tickers_24h(symbols: list[str]) -> dict[str, dict]:
    """24h stats for many symbols (Binance weight 40 per 21–100 symbols). Batched, and anything that isn't a plain
    ASCII symbol is dropped first — a listing named in Chinese characters once 400'd the whole request."""
    import json
    from data.http import get_json

    clean = [s for s in symbols if s.isascii() and s.isalnum()]
    data: list = []
    for i in range(0, len(clean), 60):
        data += await get_json(
            f"{BASE}/ticker/24hr",
            params={"symbols": json.dumps(clean[i:i + 60], separators=(",", ":"))},
        )
    out = {}
    for d in data:
        last = float(d["lastPrice"])
        out[d["symbol"]] = {
            "price": last,
            "price_change_pct": float(d["priceChangePercent"]),
            "high": float(d["highPrice"]),
            "low": float(d["lowPrice"]),
            "quote_volume": float(d["quoteVolume"]),
            "trades": int(d.get("count") or 0),
            "range_pct": (float(d["highPrice"]) - float(d["lowPrice"])) / last * 100 if last else 0.0,
        }
    return out


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


# ── USDⓈ-M perpetuals: the venue trades are simulated on ─────────────────────────

async def fetch_futures_minute_bars(symbol: str, start_ms: int, limit: int = 1000) -> list[dict]:
    """Perpetual 1m klines from `start_ms` — fills, stops and liquidations are walked on these."""
    from data.http import get_json

    rows = await get_json(
        f"{FAPI}/klines",
        params={"symbol": symbol, "interval": "1m", "startTime": start_ms, "limit": limit},
    )
    return [
        {"open_time": r[0], "open": float(r[1]), "high": float(r[2]),
         "low": float(r[3]), "close": float(r[4]), "close_time": r[6]}
        for r in rows
    ]


async def fetch_futures_klines(symbol: str, interval: str = "4h", limit: int = 100) -> list[dict]:
    """The latest perpetual klines (most recent last)."""
    from data.http import get_json

    rows = await get_json(f"{FAPI}/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
    return [{"open_time": r[0], "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]),
             "close_time": r[6]} for r in rows]


async def fetch_futures_price(symbol: str) -> float | None:
    from data.http import get_json

    d = await get_json(f"{FAPI}/ticker/price", params={"symbol": symbol})
    return float(d["price"]) if d.get("price") else None


async def fetch_funding_history(symbol: str, start_ms: int) -> list[dict]:
    """Every funding settlement since `start_ms` (8h on most perpetuals, hourly on some — paged, so a
    week of hourly settlements isn't cut off at the old limit of 100)."""
    from data.http import get_json

    out, t = [], start_ms
    while True:
        rows = await get_json(f"{FAPI}/fundingRate", params={"symbol": symbol, "startTime": t, "limit": 1000})
        out += [{"time": int(r["fundingTime"]), "rate": float(r["fundingRate"]),
                 "mark": float(r.get("markPrice") or 0) or None} for r in rows]
        if len(rows) < 1000:
            return out
        t = int(rows[-1]["fundingTime"]) + 1


async def fetch_perpetual_info() -> dict[str, str]:
    """Every trading USDT-margined perpetual, crypto and TradFi alike, with Binance's own asset class: symbol →
    "crypto" (underlyingType COIN), "stocks" (EQUITY, KR_EQUITY, HK_EQUITY…) or "futures" (COMMODITY). Index, FX and
    other TradFi types count as futures. The copy books route each leader position by this."""
    from data.http import get_json

    info = await get_json(f"{FAPI}/exchangeInfo", timeout=20.0)
    out = {}
    for s in info.get("symbols", []):
        if s.get("contractType") not in ("PERPETUAL", "TRADIFI_PERPETUAL") or s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
            continue
        ut = (s.get("underlyingType") or "COIN").upper()
        out[s["symbol"]] = "crypto" if ut == "COIN" else "stocks" if "EQUITY" in ut else "futures"
    return out


async def fetch_perpetual_symbols() -> list[str]:
    """Every trading USDT-margined crypto perpetual (the scanner's universe; TradFi perps are not coins)."""
    from data.http import get_json

    info = await get_json(f"{FAPI}/exchangeInfo", timeout=20.0)
    return [
        s["symbol"] for s in info.get("symbols", [])
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    ]
