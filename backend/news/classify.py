"""Claude reads each new headline and says what it means for the coins we can trade.

VADER — the dictionary sentiment the old news feed used — reads "short squeeze" and
"exploit funds recovered" as negative and "record liquidations" as positive; for market
news it's close to noise. Claude labels coins, event type, likely direction and importance.

Batched (BATCH headlines per call) on its own daily budget. Unclassified items still count,
at the lowest weight and VADER's reading, until Claude gets to them (scoring.py).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from news import store
from storage import state as kv

logger = logging.getLogger("news.classify")

# Halved on 2026-10-08 (was 150): over its first ~2 weeks the news score didn't predict returns
# (signal scoring IC −0.06 … −0.10), so it gets a smaller budget until it shows value.
DAILY_CALLS = int(os.getenv("NEWS_CLAUDE_DAILY_CALLS", "75"))
BATCH = 25
# A partial batch is classified once its oldest headline has waited this long. At ~50 headlines an
# hour, 5 minutes meant calls of 4–5 headlines (~290 calls a day); 30 minutes fills a batch of ~25
# (~50 calls a day, inside the budget). Binance listings/delistings never wait (URGENT_SOURCES).
MAX_WAIT = int(os.getenv("NEWS_CLASSIFY_MAX_WAIT", "1800"))
URGENT_SOURCES = ("Binance listings", "Binance delistings")    # classified at once, never batched up

EVENT_TYPES = [
    "listing", "delisting", "hack_exploit", "regulation", "enforcement_legal", "etf_institutional",
    "partnership_adoption", "product_upgrade", "supply_unlock_burn", "funding_investment", "stablecoin",
    "exchange_ops", "macro_policy", "macro_data", "market_flows", "price_commentary", "other",
]
SCOPES = ["coin", "market", "macro", "irrelevant"]

SYSTEM_PROMPT = f"""You classify crypto and macro news headlines for a short-term (hours to days) crypto futures trader.

For each item return an object with:
- "id": the item's id, unchanged
- "coins": symbols from the TRADEABLE COINS list the story is specifically about (empty if none). Only use listed symbols. A story about "altcoins" or "crypto" in general names no coins.
- "scope": "coin" (about specific listed coins), "market" (crypto-wide: exchanges, stablecoins, ETF flows, industry regulation), "macro" (rates, inflation, jobs, geopolitics, TradFi), or "irrelevant" (nothing a crypto price would react to: listicles, promos, unrelated companies)
- "event_type": one of {json.dumps(EVENT_TYPES)}
- "direction": expected short-term price impact from -1.0 (clearly bearish) to +1.0 (clearly bullish), 0 if neutral or unclear. For "coin" scope it's the impact on those coins; for "market"/"macro" on crypto overall.
- "importance": 1–5 for a trader over the next day
    5 = market-moving at once (major exchange hack, surprise Fed decision, top-10 coin delisted or approved ETF)
    4 = strong catalyst for the named coin (Binance listing/delisting, protocol exploit, major enforcement action, large unlock)
    3 = notable (real partnership or adoption, upgrade shipping, sizeable fund flows, regulatory progress)
    2 = routine news with little price relevance
    1 = opinion, price predictions, "should you buy" pieces, recaps of moves that already happened
- "note": at most 20 words — why it matters (or doesn't)
- "same_story_as": if this item reports the SAME underlying event as an entry in RECENT STORIES or an earlier item in this batch, that story/item id; otherwise null. Same event = same announcement, hack, filing, deal or price move covered again (a rewording, a follow-up recap, another outlet, "X jumps 24%" and "why is X up 22% today"). NOT the same: a new development in a continuing saga (charges filed after an investigation), or a separate move on a later day.

Rules: judge the event, not the headline's tone. A recap of yesterday's move is importance 1 (it's already in the price). Exchange notices about margin tiers or maintenance are "exchange_ops", usually importance 1–2. Binance "will delist" is bearish for that coin (importance 4–5); "will list" / new perpetual is bullish (importance 3–4). Delisting of a spot PAIR (e.g. X/BNB) while other pairs stay is importance 2.
Reply with ONLY a JSON array, one object per item, no prose."""


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _budget_key() -> str:
    return kv.key("news", "claude_calls", _day())


def calls_today() -> int:
    return int(kv.get_sync(_budget_key()) or 0)


def due(now: float | None = None) -> bool:
    """A full batch waiting, an urgent source waiting, or a partial batch that has waited long enough."""
    now = now or time.time()
    n = store.pending_count()
    if not n:
        return False
    if n >= BATCH:
        return True
    oldest = store.oldest_pending_seen()
    if oldest and now - oldest >= MAX_WAIT:
        return True
    return any(p["source"] in URGENT_SOURCES or any(s.get("via") in URGENT_SOURCES for s in p["sources"])
               for p in store.pending(BATCH))


STORY_CONTEXT = 60           # recent stories shown to Claude for linking


def _story_context(items: list[dict]) -> list[dict]:
    coins = {c for it in items for c in (it.get("mentions") or [])}
    return store.recent_stories(time.time() - store.STORY_WINDOW, coins or None, STORY_CONTEXT)


def _brief(items: list[dict], universe: dict[str, str], stories: list[dict] | None = None) -> str:
    coins = ", ".join(f"{s} ({n})" for s, n in sorted(universe.items()))
    lines = [f"TRADEABLE COINS: {coins}", ""]
    if stories:
        lines.append("RECENT STORIES (id: lead headline):")
        lines += [f"{st['story']}: {st['title']}" for st in stories]
        lines.append("")
    lines.append("ITEMS:")
    for it in items:
        when = datetime.fromtimestamp(it["published"] or it["seen"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        outlets = ", ".join(sorted({s["source"] for s in it["sources"]}))
        lines.append(json.dumps({"id": it["id"], "published": when, "outlets": outlets, "title": it["title"],
                                 "summary": (it.get("summary") or "")[:400]}, ensure_ascii=False))
    return "\n".join(lines)


def validate(a: dict, universe: dict[str, str]) -> dict:
    """Coerce one answer into the stored shape; unknown values fall back to safe defaults."""
    def num(v, lo, hi, default):
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return default
    scope = a.get("scope") if a.get("scope") in SCOPES else "irrelevant"
    return {
        "coins": [c for c in (a.get("coins") or []) if isinstance(c, str) and c.upper() in universe][:6],
        "scope": scope,
        "event_type": a.get("event_type") if a.get("event_type") in EVENT_TYPES else "other",
        "direction": round(num(a.get("direction"), -1.0, 1.0, 0.0), 2),
        "importance": int(num(a.get("importance"), 1, 5, 1)),
        "note": str(a.get("note") or "")[:200],
    }


def resolve_story(item: dict, answer: dict, known: dict[str, str]) -> str:
    """The story an item belongs to. `known` maps ids Claude may cite (recent stories, this batch)
    to their story id; anything else — or no link — keeps the story assigned at intake."""
    target = answer.get("same_story_as")
    if isinstance(target, str) and target in known and target != item["id"]:
        return known[target]
    return item.get("story") or item["id"]


async def run(universe: dict[str, str], max_calls: int | None = None) -> int:
    """Classify what's due, newest first, within today's budget. Returns items classified."""
    from oracle.claude_oracle import MODEL, get_client, parse_json, response_text
    done, calls = 0, 0
    while due() and calls_today() < DAILY_CALLS and (max_calls is None or calls < max_calls):
        calls += 1
        batch = store.pending(BATCH)
        stories = _story_context(batch)
        await kv.incr_with_expire(_budget_key(), 172800)
        try:
            resp = await get_client().messages.create(
                model=MODEL, max_tokens=8000 + 140 * len(batch),
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": _brief(batch, universe, stories)}])
            answers = parse_json(response_text(resp))
        except Exception as e:
            logger.warning("News classification failed: %s", e)
            return done
        by_id = {a.get("id"): a for a in answers if isinstance(a, dict)}
        known = {st["story"]: st["story"] for st in stories}
        labels = {}
        for it in batch:                       # batch order: an item may link to one earlier in the batch
            if it["id"] not in by_id:
                continue
            labels[it["id"]] = {**validate(by_id[it["id"]], universe), "story": resolve_story(it, by_id[it["id"]], known)}
            known[it["id"]] = labels[it["id"]]["story"]
        # Anything Claude skipped is marked irrelevant rather than retried forever.
        for it in batch:
            labels.setdefault(it["id"], {"coins": [], "scope": "irrelevant", "event_type": "other",
                                         "direction": 0.0, "importance": 1, "note": "not classified (skipped)"})
        store.save_labels(labels)
        done += len(labels)
    return done


LINK_BATCH = 80
LINK_PROMPT = """You group crypto news headlines into stories. Two headlines are the same story when they report the
SAME underlying event (same announcement, hack, filing, deal, or the same price move covered again by other outlets or
as a recap). A new development in a continuing saga, or a separate move on a later day, is a different story.
For each ITEM reply with {"id": ..., "same_story_as": <an id from RECENT STORIES or an EARLIER item in ITEMS, or null>}.
Reply with ONLY a JSON array."""


async def link_backlog(max_calls: int = 2) -> int:
    """Link stories for headlines classified before story linking existed (titles only — cheap).
    Shares the classification budget. Returns items checked."""
    from oracle.claude_oracle import MODEL, get_client, parse_json, response_text
    done = 0
    for _ in range(max_calls):
        if calls_today() >= DAILY_CALLS:
            break
        items = store.unchecked_stories(time.time() - store.MAX_AGE, LINK_BATCH)
        if not items:
            break
        first_seen = min(i["seen"] for i in items)
        stories = [st for st in store.recent_stories(first_seen - store.STORY_WINDOW, None, 200)
                   if st["story"] not in {i["id"] for i in items}][:STORY_CONTEXT]
        body = ["RECENT STORIES:"] + [f"{st['story']}: {st['title']}" for st in stories] + ["", "ITEMS (oldest first):"]
        body += [json.dumps({"id": i["id"], "coins": i.get("coins") or [], "title": i["title"]}, ensure_ascii=False) for i in items]
        await kv.incr_with_expire(_budget_key(), 172800)
        try:
            resp = await get_client().messages.create(
                model=MODEL, max_tokens=8000 + 40 * len(items), system=LINK_PROMPT,
                messages=[{"role": "user", "content": "\n".join(body)}])
            answers = {a.get("id"): a for a in parse_json(response_text(resp)) if isinstance(a, dict)}
        except Exception as e:
            logger.warning("Story linking failed: %s", e)
            break
        known = {st["story"]: st["story"] for st in stories}
        links = {}
        for it in items:
            links[it["id"]] = resolve_story(it, answers.get(it["id"]) or {}, known)
            known[it["id"]] = links[it["id"]]
        store.set_stories(links)
        done += len(items)
    return done


CONSOLIDATE_EVERY = 1800
CONSOLIDATE_PROMPT = """You are given the lead headline of each important crypto news story from the last two days.
Some entries may be the SAME underlying event, split because different outlets worded it differently
(e.g. "Bitwise NEAR ETF launches in the U.S." and "First spot NEAR ETF in the US: Bitwise launches NRR").
Group the ids that report the same event. A new development in a continuing saga is NOT the same event.
Reply with ONLY a JSON array of groups, each an array of 2+ ids; [] if every entry is distinct."""


async def consolidate(force: bool = False) -> int:
    """Merge important stories that are one event but were linked apart (different batches, wording).
    Runs at most every CONSOLIDATE_EVERY, and only when the set of important stories changed.
    Returns the number of headlines moved."""
    import hashlib
    from news import scoring
    from oracle.claude_oracle import MODEL, get_client, parse_json, response_text
    last = kv.get_sync(kv.key("news", "consolidated")) or {}
    if not force and time.time() - last.get("at", 0) < CONSOLIDATE_EVERY:
        return 0
    leads = scoring.important(hours=48, limit=80, min_importance=3)
    key = hashlib.sha1(",".join(sorted(b["story"] for b in leads)).encode()).hexdigest()[:16]
    if len(leads) < 2 or (key == last.get("key") and not force) or calls_today() >= DAILY_CALLS:
        return 0
    lines = [f"{b['story']} | {','.join(b['coins']) or b['scope']} | {b['title']}" for b in leads]
    await kv.incr_with_expire(_budget_key(), 172800)
    try:
        resp = await get_client().messages.create(
            model=MODEL, max_tokens=8000, system=CONSOLIDATE_PROMPT,
            messages=[{"role": "user", "content": "id | coins | lead headline\n" + "\n".join(lines)}])
        groups = parse_json(response_text(resp))
    except Exception as e:
        logger.warning("Story consolidation failed: %s", e)
        return 0
    age = {b["story"]: b["age_h"] for b in leads}
    moved = 0
    for g in groups if isinstance(groups, list) else []:
        ids = [i for i in (g if isinstance(g, list) else []) if i in age]
        if len(set(ids)) < 2:
            continue
        target = max(set(ids), key=lambda i: age[i])            # the first reported keeps its id
        moved += store.merge_stories(target, [i for i in set(ids) if i != target])
    kv.set_sync(kv.key("news", "consolidated"), {"at": time.time(), "key": key}, 3 * 86400)
    if moved:
        logger.info("News: merged %d headlines into existing stories", moved)
    return moved
