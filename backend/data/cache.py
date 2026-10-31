"""Redis-backed cache for the data layer and scanner.

Three behaviours the scanner depends on:
  * single-flight — ten concurrent symbol scans must not stampede FRED/news at once
  * stale-while-error — a transient 429 should serve the last good value, not flatten
    a score to zero via the pipeline's DEFAULTS
  * degrade, never break — a stopped Redis container falls through to direct fetches
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis
from redis.exceptions import RedisError

logger = logging.getLogger("data.cache")

NS = "oracle:v1"

# TTLs in seconds, by logical source. Symbol-independent sources (macro, fear_greed,
# global_market, macro_news) are fetched once per sweep and shared across all symbols.
TTL = {
    "cmc_listings": 900,   # 1 credit/call; 96/day fits the free 10k/month tier
    "macro": 21600,
    "fear_greed": 3600,
    "news": 1800,
    "macro_news": 7200,
    "global_market": 900,
    "coingecko": 21600,      # rank / 7d change / votes, shown to Claude only (FA retired as a signal): 6h is plenty and ends the 429s
    "social": 3600,
    "positioning": 900,      # Binance futures stats update hourly; funding every 8h
    "defillama": 3600,       # protocol / chain / fee listings — one call each for every project
    "chain_tvl": 21600,
    "stablecoins": 21600,
    "ticker": 20,
    "oracle": 60,
}

# OHLCV goes stale at roughly half a candle, floored so the 1-min watchlist tier
# still gets fresh fast-timeframe candles.
OHLCV_TTL = {"15m": 60, "30m": 120, "1h": 300, "4h": 900, "1d": 3600}

_client: aioredis.Redis | None = None
_down_until: float = 0.0
_warned = False


def key(*parts: str) -> str:
    return ":".join((NS, *(str(p) for p in parts)))


async def get_client() -> aioredis.Redis | None:
    """Shared client, or None while Redis is known-down (re-probed every 30s)."""
    global _client, _down_until, _warned

    if time.time() < _down_until:
        return None

    if _client is None:
        _client = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=3,
            health_check_interval=30,
        )
    try:
        await _client.ping()
        _warned = False
        return _client
    except (RedisError, OSError) as e:
        _down_until = time.time() + 30
        if not _warned:
            logger.warning("Redis unavailable (%s) — serving uncached for 30s", e)
            _warned = True
        return None


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def get(k: str) -> Any | None:
    client = await get_client()
    if client is None:
        return None
    try:
        raw = await client.get(k)
        return json.loads(raw) if raw else None
    except (RedisError, OSError, json.JSONDecodeError):
        return None


async def set(k: str, value: Any, ttl: int) -> None:
    client = await get_client()
    if client is None:
        return
    try:
        await client.set(k, json.dumps(value, default=str), ex=ttl)
    except (RedisError, OSError, TypeError) as e:
        logger.debug("Cache set failed for %s: %s", k, e)


async def delete(*keys: str) -> None:
    client = await get_client()
    if client is None or not keys:
        return
    try:
        await client.delete(*keys)
    except (RedisError, OSError):
        pass


async def hgetall(k: str) -> dict:
    """Read a hash whose values are JSON documents."""
    client = await get_client()
    if client is None:
        return {}
    try:
        raw = await client.hgetall(k)
        out = {}
        for field, value in (raw or {}).items():
            try:
                out[field] = json.loads(value)
            except json.JSONDecodeError:
                continue
        return out
    except (RedisError, OSError):
        return {}


async def hset(k: str, field: str, value: Any) -> None:
    client = await get_client()
    if client is None:
        return
    try:
        await client.hset(k, field, json.dumps(value, default=str))
    except (RedisError, OSError, TypeError) as e:
        logger.debug("Cache hset failed for %s/%s: %s", k, field, e)


async def hdel(k: str, *fields: str) -> None:
    client = await get_client()
    if client is None or not fields:
        return
    try:
        await client.hdel(k, *fields)
    except (RedisError, OSError):
        pass


async def acquire_once(k: str, ttl: int) -> bool:
    """SET NX EX — atomic claim. Used for LLM per-symbol cooldowns."""
    client = await get_client()
    if client is None:
        return True  # no Redis: don't block the caller, just lose dedup
    try:
        return bool(await client.set(k, str(int(time.time())), nx=True, ex=ttl))
    except (RedisError, OSError):
        return True


async def incr_with_expire(k: str, ttl: int) -> int:
    """INCR + EXPIRE on first write. Used for the daily LLM budget counter."""
    client = await get_client()
    if client is None:
        return 0
    try:
        pipe = client.pipeline()
        pipe.incr(k)
        pipe.expire(k, ttl)
        result = await pipe.execute()
        return int(result[0])
    except (RedisError, OSError):
        return 0


async def counter(k: str) -> int:
    client = await get_client()
    if client is None:
        return 0
    try:
        raw = await client.get(k)
        return int(raw) if raw else 0
    except (RedisError, OSError, ValueError):
        return 0


async def cached(
    k: str,
    ttl: int,
    loader: Callable[[], Awaitable[Any]],
    *,
    stale_ttl: int | None = None,
) -> Any:
    """Return cached value, else load it once (single-flight) and cache it.

    On loader failure the last good value is served from the stale mirror if one
    exists; only if there is no stale copy does the exception propagate.
    """
    client = await get_client()
    if client is None:
        return await loader()

    hit = await get(k)
    if hit is not None:
        return hit

    stale_key = f"{k}:stale"
    stale_ttl = stale_ttl or max(ttl * 10, 21600)
    lock_key = f"{k}:lock"

    got_lock = False
    try:
        got_lock = bool(await client.set(lock_key, "1", nx=True, ex=30))
    except (RedisError, OSError):
        pass

    if not got_lock:
        # Another task is loading this key — wait briefly for it to publish.
        for _ in range(50):
            await asyncio.sleep(0.1)
            hit = await get(k)
            if hit is not None:
                return hit
        logger.debug("Single-flight wait timed out for %s — loading directly", k)

    try:
        try:
            value = await loader()
        except Exception as e:
            stale = await get(stale_key)
            if stale is not None:
                from data.http import err_text
                logger.warning("Loader failed for %s (%s) — serving stale copy", k, err_text(e))
                return stale
            raise
        # Publish before releasing the lock, or a task arriving in between loads it again.
        await set(k, value, ttl)
        await set(stale_key, value, stale_ttl)
        return value
    finally:
        if got_lock:
            await delete(lock_key)
