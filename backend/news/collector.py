"""The news loop: poll what's due, keep what's dated and fresh, classify, alert, label reactions.

Runs every TICK seconds from the scheduler. Each source has its own interval (sources.py):
Binance announcements 2 min, news sites 5–10 min, SEC / Fed 15 min, and a Google News search
per scanned coin every 15 min (a few coins per tick, so the searches are spread out).

Date rules (`accept`):
  * no publish date        → kept, dated by when we first saw it
  * dated in the future    → more than 10 min ahead is a bad clock or a scheduled post: re-dated to first seen
  * older than 48 h        → dropped — it's in the price, and old items resurface in feeds
    (DL News's feed still carries articles from months ago)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from news import classify, scoring, sources, store
from storage import state as kv

logger = logging.getLogger("news.collector")

ENABLED = os.getenv("NEWS_ENABLED", "true").lower() != "false"
TICK = 60
GOOGLE_PER_TICK = 3
FUTURE_TOLERANCE = 600
REACTION_EVERY = 1800
ALERT_IMPORTANCE = int(os.getenv("NEWS_ALERT_IMPORTANCE", "4"))
HORIZONS = {"r_1h": 3600, "r_4h": 4 * 3600, "r_24h": 24 * 3600}

_last: dict[str, float] = {}         # source (or "Google News:SOL") → last poll
_state = {"last_reactions": 0.0, "last_tick": 0.0, "last_new": 0}


def accept(item: dict, now: float) -> dict | None:
    """Apply the date rules. Returns the item with `seen` (and a trusted `published`), or None."""
    pub = item.get("published")
    if pub and pub > now + FUTURE_TOLERANCE:
        pub = None
    if pub and now - pub > store.MAX_AGE:
        return None
    if not item.get("title"):
        return None
    return {**item, "published": pub, "seen": now}


async def universe() -> dict[str, str]:
    """{BASE: name} — the top-N pool plus anything held, so any coin we could trade is recognised."""
    from scanner import scheduler
    pool = scheduler._state.get("pool") or []
    if not pool:
        try:
            from data.coinmarketcap_client import get_tradeable_pool
            pool = await get_tradeable_pool(scheduler.POOL_SIZE)
        except Exception as e:
            logger.debug("No coin pool for news matching: %s", e)
    out = {c["symbol"]: c.get("name") or c["symbol"] for c in pool if c.get("symbol")}
    for sym in _held():
        out.setdefault(sym, sym)
    return out


def _held() -> set[str]:
    """Base symbols with an open position or resting order in the main or auto system (paper or live)."""
    try:
        from trading import coin_lock
        return {s.removesuffix("USDT") for s in coin_lock.holders()}
    except Exception:
        return set()


def _scanned() -> list[str]:
    from scanner import scheduler
    return [c["symbol"] for c in (scheduler._state.get("universe") or []) if c.get("symbol")]


async def poll(now: float | None = None) -> int:
    """One pass over whatever sources are due. Returns the number of new items."""
    now = now or time.time()
    if not _last:
        # After a restart, carry on from the recorded poll times — re-polling everything at once
        # got CryptoSlate to answer 429.
        _last.update({r["name"]: r["last_poll"] or 0 for r in store.sources()})
    uni = await universe()
    match = scoring.mention_matcher(uni)
    jobs: list[tuple[str, sources.Source, str | None, str | None]] = []
    for src in sources.SOURCES:
        if src.kind == "google":
            due = [c for c in _scanned() if now - _last.get(f"{src.name}:{c}", 0) >= src.interval]
            for c in due[:GOOGLE_PER_TICK]:
                jobs.append((f"{src.name}:{c}", src, src.url.format(q=sources.google_query(uni.get(c, c), c)), c))
        elif now - _last.get(src.name, 0) >= src.interval:
            jobs.append((src.name, src, None, None))

    new_total = 0
    async with sources.client() as client:
        results = await asyncio.gather(*(sources.fetch(client, src, url) for _, src, url, _ in jobs),
                                       return_exceptions=True)
    for (key, src, _, coin), res in zip(jobs, results):
        _last[key] = now
        health = src.name                             # Google's per-coin searches report as one source
        if isinstance(res, Exception):
            logger.debug("News source %s failed: %s", key, res)
            store.source_polled(health, ok=False, error=f"{key}: {res}")
            continue
        kept, stale, off_topic = [], 0, 0
        for raw in res:
            it = accept(raw, now)
            if it is None:
                stale += 1
                continue
            text = f"{it['title']} {it.get('summary') or ''}"
            if coin and not scoring.crypto_context(text, coin, uni.get(coin)):
                off_topic += 1
                continue
            mentions = set(match(text))
            if coin:
                mentions.add(coin)                    # found by searching for this coin
            it["mentions"] = sorted(mentions)
            it["vader"] = scoring.vader(it["title"])
            kept.append(it)
        if off_topic:
            logger.debug("%s: %d off-topic search results skipped", key, off_topic)
        new = store.add(kept)
        new_total += len(new)
        store.source_polled(health, ok=True, new=len(new), stale=stale,
                            newest=max((r.get("published") or 0 for r in res), default=None) or None)
    return new_total


async def _alerts(since: float) -> None:
    """Important news on a coin we hold: publish it so the app can raise it at once."""
    from scanner import bus
    held = _held()
    if not held:
        return
    for it in store.items(since):
        if not it.get("classified_at") or (it.get("importance") or 0) < ALERT_IMPORTANCE:
            continue
        hit = sorted(set(it.get("coins") or []) & held)
        if not hit or not await kv.acquire_once(kv.key("news", "alerted", it["id"]), 7 * 86400):
            continue
        logger.warning("NEWS on held coin %s — %s (importance %s, direction %+.1f)", ",".join(hit), it["title"],
                       it["importance"], it.get("direction") or 0)
        bus.publish("news_alert", {"id": it["id"], "coins": hit, "title": it["title"], "url": it.get("url"),
                                   "importance": it["importance"], "direction": it.get("direction"),
                                   "event_type": it.get("event_type"), "note": it.get("note")})


async def label_reactions(now: float | None = None) -> int:
    """What price did after each classified headline: 1 h / 4 h / 24 h from first seen, on the
    perpetual (BTC for market-wide and macro news). Returns rows written."""
    from data.derivatives_client import fetch_klines_range
    now = now or time.time()
    todo = store.reactions_pending(ready_before=now - HORIZONS["r_1h"] - 600)
    by_symbol: dict[str, list[dict]] = {}
    for it in todo:
        coins = it.get("coins") or []
        targets = [f"{c}USDT" for c in coins] if it.get("scope") == "coin" and coins else ["BTCUSDT"]
        for sym in targets:
            by_symbol.setdefault(sym, []).append(it)
    rows = []
    for sym, its in by_symbol.items():
        start = min(i["seen"] for i in its) - 900
        try:
            bars = await fetch_klines_range(sym, "5m", int(start * 1000), int(now * 1000))
        except Exception as e:
            logger.debug("Reaction bars for %s unavailable: %s", sym, e)
            continue
        closes = {b[0] // 1000: b[4] for b in bars}

        def close_at(t: float) -> float | None:
            """The last 5-minute close completed by time t."""
            bar = int(t // 300) * 300 - 300
            return closes.get(bar) or closes.get(bar - 300)
        for it in its:
            p0 = close_at(it["seen"])
            if not p0:
                continue
            vals = [round((close_at(it["seen"] + h) / p0 - 1) * 100, 3) if it["seen"] + h + 300 <= now
                    and close_at(it["seen"] + h) else None for h in HORIZONS.values()]
            rows.append((it["id"], sym, p0, *vals))
    store.save_reactions(rows)
    return len(rows)


async def tick() -> None:
    if not ENABLED:
        return
    now = time.time()
    new = await poll(now)
    _state.update(last_tick=now, last_new=new)
    # A few calls per tick at most, so a backlog never holds up the next poll.
    classified = await classify.run(await universe(), max_calls=3)
    if classified:
        await _alerts(now - 3600)
    elif not store.pending_count():
        # Idle: link stories for headlines classified before story linking existed, then merge
        # important stories that are one event linked apart.
        if not await classify.link_backlog(max_calls=1):
            await classify.consolidate()
    try:
        from news import brief
        await brief.maybe(now)
    except Exception as e:
        logger.warning("App headlines check failed: %s", e)
    if new or classified:
        logger.info("News: %d new headlines, %d classified by Claude (%d calls today)", new, classified,
                    classify.calls_today())
    if now - _state["last_reactions"] >= REACTION_EVERY:
        _state["last_reactions"] = now
        try:
            n = await label_reactions(now)
            if n:
                logger.info("News reactions: labelled %d item/coin pairs", n)
        except Exception as e:
            logger.warning("News reaction labelling failed: %s", e)


def status() -> dict:
    return {"enabled": ENABLED, "tick_s": TICK, "last_tick": _state["last_tick"], "last_new": _state["last_new"],
            "pending": store.pending_count(), "claude_calls_today": classify.calls_today(),
            "claude_daily_calls": classify.DAILY_CALLS, "half_life_h": scoring.HALF_LIFE_H,
            "max_age_h": store.MAX_AGE / 3600,
            "intervals": [{"source": s.name, "every_s": s.interval,
                           "note": "per scanned coin" if s.kind == "google" else None} for s in sources.SOURCES]}
