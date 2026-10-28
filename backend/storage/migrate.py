"""One-time migrations into the SQLite databases. Safe to run on every start.

  * Redis → state.db / signals.db: durable coordination state that used to live in Redis
    (watchlist, cooldowns, budgets, Claude approval keys, autotrader states, scan snapshot,
    fired-signal feed). Keys keep their remaining TTL. Redis keys are left in place.
  * data/signal_history.json → signals.db (the file is kept as *.migrated.json).
  * The wallets' JSON files are imported by PaperWallet itself on first load.
"""

import json
import logging
import os

from data import cache
from storage import signals, state

logger = logging.getLogger("storage.migrate")

HISTORY_JSON = os.path.join(os.path.dirname(__file__), "..", "data", "signal_history.json")
DONE_KEY = state.key("meta", "migrated_from_redis")

# What counted as state (not cache) in Redis, by key pattern under the oracle:v1 namespace.
STATE_PATTERNS = ("watchlist", "watchlist:left:*", "llm_budget:*", "llm_cd:*", "auto:*", "scan:snapshot")


def migrate_signal_history() -> int:
    if not os.path.exists(HISTORY_JSON) or signals.oracle_count() > 0:
        return 0
    with open(HISTORY_JSON) as f:
        entries = json.load(f)
    for e in entries:
        signals.add_oracle_signal(e)
    os.replace(HISTORY_JSON, HISTORY_JSON[:-5] + ".migrated.json")
    logger.info("Imported %d oracle signals from signal_history.json into signals.db", len(entries))
    return len(entries)


async def migrate_redis() -> dict:
    if await state.get(DONE_KEY):
        return {}
    client = await cache.get_client()
    if client is None:
        logger.info("Redis unavailable — state migration will be retried next start")
        return {}

    prefix = cache.key("")                      # "oracle:v1:"
    moved = {"keys": 0, "hash_fields": 0, "fired": 0}
    for pattern in STATE_PATTERNS:
        async for raw in client.scan_iter(match=prefix + pattern):
            k = raw.decode() if isinstance(raw, bytes) else raw
            new_key = state.key(*k[len(prefix):].split(":"))
            kind = await client.type(k)
            kind = kind.decode() if isinstance(kind, bytes) else kind
            if kind == "hash":
                for field, value in (await client.hgetall(k)).items():
                    field = field.decode() if isinstance(field, bytes) else field
                    await state.hset(new_key, field, json.loads(value))
                    moved["hash_fields"] += 1
            elif kind == "string":
                value = await client.get(k)
                ttl_ms = await client.pttl(k)
                if value is None or ttl_ms == -2:
                    continue
                await state.set(new_key, json.loads(value), ttl_ms / 1000 if ttl_ms > 0 else None)
                moved["keys"] += 1

    feed = await client.get(cache.key("fired_feed"))
    for sig in json.loads(feed) if feed else []:
        signals.add_fired(sig)
        moved["fired"] += 1

    await state.set(DONE_KEY, True)
    logger.info("Migrated durable state from Redis into SQLite: %s", moved)
    return moved


async def run() -> None:
    try:
        migrate_signal_history()
    except Exception as e:
        logger.error("signal_history.json import failed: %s", e)
    try:
        await migrate_redis()
    except Exception as e:
        logger.error("Redis state migration failed: %s", e)
