"""Redis-cached wrappers around every upstream data source.

Both the on-demand oracle pipeline and the background scanner read through here, so a
five-symbol sweep and a user clicking Refresh share the same cached upstream calls.

The important property: macro, fear & greed, global market and macro news are
symbol-independent. Caching them once collapses what would be ~80 upstream calls per
ten-symbol sweep down to ~4, and is what makes the 1-minute watchlist tier affordable —
at that cadence only the fast candles and the ticker are genuinely refetched.
"""

import asyncio
import logging


from data import cache
from data.binance_client import fetch_ohlcv, fetch_order_book, fetch_ticker_24h
from data import defillama_client as llama
from data.coingecko_client import fetch_global_market, get_for_symbol, resolve_id
from data.derivatives_client import fetch_positioning
from data.macro_client import fetch_fear_greed_index, fetch_macro_snapshot
from data.news_client import fetch_all_news
from data.social_client import fetch_twitter_sentiment

logger = logging.getLogger("data.feeds")

DEFAULT_INTERVALS = ("15m", "30m", "1h", "4h")


async def cached_ohlcv(symbol: str, interval: str, limit: int = 200) -> list[dict]:
    """Candles for one symbol/interval, sliced from a shared cached fetch.

    Callers ask for different depths — charts want 120, the TA pipeline wants 200 — so
    the fetch is rounded up to a bucket and the cache key includes it. Without this the
    chart's shallower request would evict the deeper one and silently starve the
    indicators of history.
    """
    bucket = 500 if limit > 200 else 200
    candles = await cache.cached(
        cache.key("ohlcv", symbol, f"{interval}:{bucket}"),
        cache.OHLCV_TTL.get(interval, 300),
        lambda: fetch_ohlcv(symbol, interval, bucket),
        # Candles hours old would feed the TA (and the autotrader) a market that's gone;
        # after ~10 missed refreshes it's better to fail the scan than to trade on them.
        stale_ttl=cache.OHLCV_TTL.get(interval, 300) * 10,
    )
    return candles[-limit:] if limit < len(candles) else candles


async def cached_ticker(symbol: str) -> dict:
    return await cache.cached(
        cache.key("ticker", symbol),
        cache.TTL["ticker"],
        lambda: fetch_ticker_24h(symbol),
        stale_ttl=120,          # a price from hours ago is worse than none
    )


async def cached_order_book(symbol: str) -> dict:
    return await cache.cached(
        cache.key("depth", symbol),
        cache.TTL["ticker"],
        lambda: fetch_order_book(symbol, 20),
        stale_ttl=120,
    )


async def cached_market_snapshot(
    symbol: str,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
) -> dict:
    """Same shape as binance_client.fetch_market_snapshot, cached per interval.

    Per-interval keys matter: the 1-minute watchlist tier passes only the fast
    timeframes, and still reuses the 4h candles cached by the 5-minute sweep.
    """
    results = await asyncio.gather(
        *(cached_ohlcv(symbol, iv) for iv in intervals),
        cached_ticker(symbol),
        cached_order_book(symbol),
        return_exceptions=True,
    )

    snapshot: dict = {"symbol": symbol, "ohlcv": {}, "ticker": {}, "order_book": {}}
    for i, interval in enumerate(intervals):
        snapshot["ohlcv"][interval] = [] if isinstance(results[i], Exception) else results[i]

    n = len(intervals)
    snapshot["ticker"] = {} if isinstance(results[n], Exception) else results[n]
    snapshot["order_book"] = {} if isinstance(results[n + 1], Exception) else results[n + 1]
    return snapshot


async def cached_news(symbol: str) -> dict:
    # Scraped news is read from news.db (recomputed once a minute); CryptoPanic is cached inside.
    return await fetch_all_news(symbol)


async def cached_macro() -> dict:
    return await cache.cached(
        cache.key("macro", "snapshot"),
        cache.TTL["macro"],
        fetch_macro_snapshot,
    )


async def cached_fear_greed() -> dict:
    return await cache.cached(
        cache.key("fear_greed", "current"),
        cache.TTL["fear_greed"],
        fetch_fear_greed_index,
    )


async def cached_global_market() -> dict:
    return await cache.cached(
        cache.key("global_market", "current"),
        cache.TTL["global_market"],
        fetch_global_market,
    )


async def cached_social(symbol: str) -> dict:
    return await cache.cached(
        cache.key("social", symbol),
        cache.TTL["social"],
        lambda: fetch_twitter_sentiment(symbol),
    )


async def cached_coingecko(symbol: str) -> dict:
    try:
        return await cache.cached(
            cache.key("coingecko", symbol),
            cache.TTL["coingecko"],
            lambda: get_for_symbol(symbol),
        )
    except Exception as e:                       # no cached copy either: the scan goes on without fundamentals
        return {"error": str(e)}


async def cached_futures_price(symbol: str) -> float | None:
    """Last traded perpetual price — what market entries and exits are priced at."""
    from data.binance_client import fetch_futures_price

    # Stops, targets and market entries are priced off this — never serve an old price
    # (the default stale mirror would keep one for 6 hours); callers fall back on None.
    return await cache.cached(cache.key("fprice", symbol), 5, lambda: fetch_futures_price(symbol), stale_ttl=30)


async def cached_perpetual_symbols() -> set[str]:
    from data.binance_client import fetch_perpetual_symbols

    return set(await cache.cached(cache.key("binance", "perps"), 86400, fetch_perpetual_symbols))


# ── Fundamentals sources: derivatives positioning and on-chain usage ──────────────

async def cached_positioning(symbol: str) -> dict:
    return await cache.cached(
        cache.key("positioning", symbol),
        cache.TTL["positioning"],
        lambda: fetch_positioning(symbol),
    )


async def _llama_listings() -> dict:
    protocols, chains, fees = await asyncio.gather(
        cache.cached(cache.key("defillama", "protocols"), cache.TTL["defillama"], llama.fetch_protocols),
        cache.cached(cache.key("defillama", "chains"), cache.TTL["defillama"], llama.fetch_chains),
        cache.cached(cache.key("defillama", "fees"), cache.TTL["defillama"], llama.fetch_fees),
    )
    return {"protocols": protocols, "chains": chains, "fees": fees}


async def cached_usage(symbol: str) -> dict:
    """TVL and fee trends for the token behind a Binance pair (see defillama_client)."""
    base = symbol.removesuffix("USDT")

    async def load() -> dict:
        listings = await _llama_listings()
        try:
            gecko_id = await resolve_id(symbol)
        except Exception:
            gecko_id = None
        usage = llama.usage_for(base, listings["protocols"], listings["chains"], listings["fees"], gecko_id)
        if usage.get("chain"):
            try:
                history = await cache.cached(cache.key("chain_tvl", usage["chain"]), cache.TTL["chain_tvl"],
                                             lambda: llama.fetch_chain_tvl_history(usage["chain"]))
                usage.update(llama.chain_tvl_changes(history))
            except Exception as e:
                logger.debug("Chain TVL history failed for %s: %s", usage["chain"], e)
        return usage

    return await cache.cached(cache.key("usage", symbol), cache.TTL["defillama"], load)


async def cached_stablecoins() -> dict:
    history = await cache.cached(cache.key("stablecoins", "history"), cache.TTL["stablecoins"],
                                 llama.fetch_stablecoin_history)
    return llama.stablecoin_growth(history)
