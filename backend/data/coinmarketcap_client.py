"""CoinMarketCap listings — defines the scanner universe and feeds fundamental analysis."""

import logging
import os
import time

import httpx

from data import cache
from data.http import get_json, get_with_client

logger = logging.getLogger("data.coinmarketcap")

BASE = "https://pro-api.coinmarketcap.com/v1"
BINANCE_EXCHANGE_INFO = "https://api.binance.com/api/v3/exchangeInfo"

# Pegged to USD — price action carries no directional signal worth trading.
STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDE", "USDS",
    "PYUSD", "USDD", "FRAX", "LUSD", "GUSD", "USD1",
    "RLUSD", "USDG", "USD0", "USDTB", "BFUSD", "EURC", "USDF",
}

# Pegged to a commodity — same problem as stables: no crypto-native signal to trade.
COMMODITY_BACKED = {"XAUT", "PAXG"}

# Wrapped / liquid-staking derivatives track their underlying, so they duplicate
# whichever base asset is already in the universe.
DERIVATIVES = {
    "WBTC", "WETH", "STETH", "WSTETH", "WBETH", "RETH", "CBETH",
    "WEETH", "SOLVBTC", "BSC-USD", "WBNB", "JITOSOL", "MSOL",
}

EXCLUDED = STABLECOINS | DERIVATIVES | COMMODITY_BACKED

# Stables, wrapped assets and coins without a Binance USDT pair take roughly a third of
# the top slots, so over-fetch ~1.8× the pool. CMC bills 1 credit per 200 results, so
# anything up to 200 costs the same as 50.
def _fetch_limit(pool_size: int) -> int:
    return min(5000, max(100, int(pool_size * 1.8)))


LAST_POOL_KEY = "cmc:last_pool"          # the last pool that resolved — the fallback when CMC is unreachable


def _listings_key(limit: int) -> str:
    return cache.key("cmc", "listings", str(limit))


_last_listings_key = _listings_key(_fetch_limit(30))


async def _fetch_listings(limit: int) -> list[dict]:
    api_key = os.getenv("COINMARKETCAP_API_KEY")
    if not api_key:
        raise RuntimeError("COINMARKETCAP_API_KEY not set")

    payload = await get_json(
        f"{BASE}/cryptocurrency/listings/latest",
        params={"start": 1, "limit": limit, "convert": "USD", "sort": "market_cap"},
        headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"},
        timeout=15.0,
    )

    coins = []
    for c in payload.get("data", []):
        usd = c.get("quote", {}).get("USD", {})
        coins.append({
            "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "cmc_rank": c.get("cmc_rank"),
            "price": usd.get("price"),
            "market_cap": usd.get("market_cap"),
            "market_cap_dominance": usd.get("market_cap_dominance"),
            "volume_24h": usd.get("volume_24h"),
            "volume_change_24h": usd.get("volume_change_24h"),
            "percent_change_1h": usd.get("percent_change_1h"),
            "percent_change_24h": usd.get("percent_change_24h"),
            "percent_change_7d": usd.get("percent_change_7d"),
            "percent_change_30d": usd.get("percent_change_30d"),
            "circulating_supply": c.get("circulating_supply"),
            "total_supply": c.get("total_supply"),
            "max_supply": c.get("max_supply"),
        })
    return coins


async def _fetch_binance_usdt_pairs() -> list[str]:
    """Every actively trading ...USDT spot pair, so we never scan an unlistable symbol."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await get_with_client(client, BINANCE_EXCHANGE_INFO)
    return [
        s["symbol"]
        for s in r.json().get("symbols", [])
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
    ]


async def get_tradeable_pool(pool_size: int = 30) -> list[dict]:
    """Top `pool_size` coins by market cap that are actually tradeable on Binance.

    Over-fetches because stablecoins and wrapped assets occupy several of the real
    top slots and must be filtered out before taking the head of the list.
    """
    global _last_listings_key
    limit = _fetch_limit(pool_size)
    _last_listings_key = _listings_key(limit)
    try:
        listings = await cache.cached(
            _last_listings_key,
            cache.TTL["cmc_listings"],
            lambda: _fetch_listings(limit),
        )
    except Exception as e:
        # The cache's stale copy lives six hours; after an overnight stop there is none, and one CMC timeout then
        # aborted the whole sweep (2026-11-03). The last pool that resolved is kept durably and serves until CMC is back.
        from data.http import err_text
        from storage import state as kv
        last = kv.get_sync(LAST_POOL_KEY) or {}
        if last.get("pool"):
            logger.warning("CMC listings unavailable (%s) — using the last tradeable pool (%d coins, from %s)",
                           err_text(e), len(last["pool"]), time.strftime("%Y-%m-%d %H:%M", time.gmtime(last.get("at") or 0)))
            return last["pool"][:pool_size]
        raise
    pairs = set(await cache.cached(
        cache.key("binance", "usdt_pairs"),
        86400,
        _fetch_binance_usdt_pairs,
    ))
    # Trades are simulated on USDⓈ-M perpetuals, so the coin needs one. (Analysis still
    # reads spot candles, which is why the spot pair is required too.) Coins listed as
    # 1000x contracts (1000SHIBUSDT…) don't match and are skipped.
    from data.feeds import cached_perpetual_symbols
    try:
        pairs &= await cached_perpetual_symbols()
    except Exception as e:
        logger.warning("Perpetual list unavailable (%s) — universe not filtered for futures", e)

    pool = []
    for coin in listings:
        sym = coin["symbol"]
        if sym in EXCLUDED or not (sym.isascii() and sym.isalnum()):
            continue
        pair = f"{sym}USDT"
        if pair not in pairs:
            logger.debug("Skipping %s — no Binance %s spot pair and perpetual", sym, pair)
            continue
        pool.append({**coin, "pair": pair})
        if len(pool) >= pool_size:
            break

    if not pool:
        raise RuntimeError("CMC universe resolved to zero tradeable symbols")
    if len(pool) < pool_size:
        logger.warning("Pool has %d tradeable coins, fewer than the %d requested", len(pool), pool_size)
    try:
        from storage import state as kv
        kv.set_sync(LAST_POOL_KEY, {"at": time.time(), "pool": pool})
    except Exception as e:                                            # bookkeeping only — never fails a sweep
        logger.debug("Could not remember the pool: %s", e)
    return pool


async def get_for_pair(pair: str) -> dict:
    """CMC fundamentals for one Binance pair, from the cached universe listing."""
    base = pair.replace("USDT", "").upper()
    listings = await cache.get(_last_listings_key) or []
    for coin in listings:
        if coin.get("symbol") == base:
            return coin
    return {}
