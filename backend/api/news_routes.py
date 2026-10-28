"""Scraped news: the headline feed, per-coin scores, important stories, the mood verdict, the
App headline editions, source health and price-reaction research.

Read endpoints are cached in-process and keyed on the news store's version, so a poll that
changes nothing serves the last answer without recomputing — and a new headline invalidates it.
"""

import asyncio
import time

from fastapi import APIRouter, HTTPException, Query

from news import brief, classify, collector, mood, scoring, store

router = APIRouter(prefix="/news", tags=["news"])

MIN_DIRECTIONAL = 0.2          # |direction| below this is "neutral" — no side to judge a reaction by
SORTS = ("newest", "importance", "impact", "weight", "coverage", "reaction")

_cache: dict[tuple, tuple[float, int, object]] = {}


async def _cached(key: tuple, ttl: float, fn):
    """Serve (key)'s last answer while the store hasn't changed and it's younger than ttl."""
    hit = _cache.get(key)
    v = store.version()
    if hit and hit[1] == v and time.time() - hit[0] < ttl:
        return hit[2]
    value = await asyncio.to_thread(fn)
    _cache[key] = (time.time(), v, value)
    if len(_cache) > 200:                                  # filters are many; keep the newest answers
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
            _cache.pop(k, None)
    return value


def _published(it: dict) -> float:
    return min(it.get("published") or it["seen"], it["seen"])


def _max_move(it: dict) -> float:
    return max((abs(r["r_4h"] if r.get("r_4h") is not None else (r.get("r_1h") or 0)) for r in it["reactions"]),
               default=0.0)


SORT_KEYS = {
    "newest": lambda it: -_published(it),
    "importance": lambda it: (-it["importance"], -_published(it)),
    "impact": lambda it: (-(it["importance"] + abs(it["direction"])), -_published(it)),
    "weight": lambda it: (-it["weight"], -_published(it)),
    "coverage": lambda it: (-(len(it.get("similar") or []) + len(it["sources"])), -_published(it)),
    "reaction": lambda it: (-_max_move(it), -_published(it)),
}


@router.get("")
async def feed(coin: str | None = Query(None, pattern=r"^[A-Z0-9]{2,15}$"),
               scope: str | None = Query(None, pattern=r"^(coin|market|macro|irrelevant|pending)$"),
               min_importance: int = Query(1, ge=1, le=5), q: str | None = Query(None, max_length=80),
               hours: float = Query(48, gt=0, le=24 * 14), limit: int = Query(300, ge=1, le=1000),
               include_irrelevant: bool = False, sort: str = Query("newest", pattern="^(" + "|".join(SORTS) + ")$"),
               group: bool = True):
    """Headlines first seen in the last `hours`, with Claude's reading, current weight in the score,
    every outlet that ran it, and what price did afterwards. With `group`, one row per story — the
    lead headline, with the other headlines about the same event under `similar`."""
    def load():
        now = time.time()
        out = []
        for it in store.items(now - hours * 3600, limit=5000):
            rd = scoring.reading(it)
            if scope == "pending":
                if not rd["provisional"]:
                    continue
            elif scope and rd["scope"] != scope:
                continue
            if rd["scope"] == "irrelevant" and not include_irrelevant and scope != "irrelevant":
                continue
            if coin and coin not in rd["coins"] and coin not in (it.get("mentions") or []):
                continue
            if q and q.lower() not in it["title"].lower():
                continue
            out.append({**{k: it.get(k) for k in ("id", "title", "url", "source", "summary", "published", "seen",
                                                   "event_type", "note", "sources", "mentions", "story")},
                        **rd, "weight": round(scoring.weight(it, now), 3), "age_h": round(scoring._age_h(it, now), 2),
                        "similar": []})
        reacts: dict[str, list] = {}
        for r in store.reactions([o["id"] for o in out]):
            reacts.setdefault(r["item_id"], []).append({k: r[k] for k in ("symbol", "r_1h", "r_4h", "r_24h")})
        for o in out:
            o["reactions"] = reacts.get(o["id"], [])
        if group:
            stories: dict[str, list] = {}
            for o in out:
                stories.setdefault(o["story"], []).append(o)
            out = []
            for members in stories.values():
                # Lead: Claude-read over provisional, then the most important, then the first reported.
                members.sort(key=lambda m: (m["provisional"], -m["importance"], _published(m)))
                lead = {**members[0], "similar": [{k: m[k] for k in ("id", "title", "url", "source", "published", "seen",
                                                                      "importance", "direction")} for m in members[1:]]}
                lead["reactions"] = lead["reactions"] or next((m["reactions"] for m in members if m["reactions"]), [])
                out.append(lead)
        out = [o for o in out if o["importance"] >= min_importance]
        out.sort(key=SORT_KEYS[sort])
        return out[:limit]
    items = await _cached(("feed", coin, scope, min_importance, q, hours, limit, include_irrelevant, sort, group), 20, load)
    return {"items": items, "generated_at": time.time()}


@router.get("/item/{item_id}")
async def item(item_id: str):
    it = await asyncio.to_thread(store.one, item_id)
    if not it:
        raise HTTPException(404, "no such headline")
    return {**it, **scoring.reading(it), "reactions": store.reactions([item_id])}


@router.get("/scores")
async def scores():
    """Every coin with news in the window, strongest reading first, plus the market reading."""
    snap = await asyncio.to_thread(scoring.snapshot)
    coins = [{"coin": c, **{k: v for k, v in d.items() if k != "top"}, "top": d["top"][:3]}
             for c, d in snap["coins"].items()]
    coins.sort(key=lambda c: (-abs(c["score"]) if c["stories"] else 1, -c["stories_6h"]))
    return {"market": snap["market"], "coins": coins, "computed_at": snap["at"]}


@router.get("/important")
async def important(hours: float = Query(24, gt=0, le=72), limit: int = Query(8, ge=1, le=30),
                    min_importance: int = Query(3, ge=1, le=5)):
    """The biggest distinct stories — one per event, however many outlets ran it."""
    stories = await _cached(("important", hours, limit, min_importance), 30,
                            lambda: scoring.important(hours, limit, min_importance))
    return {"stories": stories, "held": sorted(await asyncio.to_thread(collector._held))}


@router.get("/mood")
async def mood_now():
    """How we're feeling: the weighted gauge (cached a minute) and the latest edition's verdict."""
    m = await mood.current()
    latest = await asyncio.to_thread(store.brief)
    return {**m, "verdict": latest["verdict"] if latest else None, "verdict_at": latest["ts"] if latest else None}


@router.get("/brief")
async def get_brief(id: int | None = Query(None, ge=1)):
    """An App headline edition — the latest, or `id` — with prev/next ids for paging back."""
    b = await asyncio.to_thread(store.brief, id)
    if not b:
        if id:
            raise HTTPException(404, "no such edition")
        return {"id": None, "headlines": [], "verdict": None, "prev_id": None, "next_id": None, "latest": True}
    return b


@router.get("/briefs")
async def list_briefs(limit: int = Query(48, ge=1, le=500)):
    return {"editions": await asyncio.to_thread(store.briefs, limit), "calls_today": brief.calls_today(),
            "daily_calls": brief.DAILY_CALLS, "every_s": brief.EVERY}


@router.post("/brief")
async def new_brief():
    """Write a fresh edition now (at most every couple of minutes)."""
    inp = await brief.gather()
    trigger = brief.due(inp, time.time(), manual=True)
    if not trigger:
        raise HTTPException(429, f"an edition was written under {brief.MANUAL_GAP // 60} minutes ago")
    b = await brief.generate(trigger, inp)
    if not b:
        raise HTTPException(503, "Claude couldn't write an edition (daily budget spent, or the call failed)")
    return b


@router.get("/status")
async def status():
    from news import sources as news_sources
    current = {s.name: s for s in news_sources.SOURCES}
    rows = [{**r, "every_s": current[r["name"]].interval, "kind": current[r["name"]].kind}
            for r in await asyncio.to_thread(store.sources) if r["name"] in current]     # retired feeds drop off
    held = await asyncio.to_thread(collector._held)
    return {**await asyncio.to_thread(collector.status), "sources": rows, "held": sorted(held),
            "brief_calls_today": brief.calls_today(), "brief_daily_calls": brief.DAILY_CALLS}


@router.post("/refresh")
async def refresh():
    """Poll whatever is due now and classify (the loop does this every minute anyway)."""
    await collector.tick()
    return await status()


def _stats(rows: list[dict]) -> dict:
    out = {"n": len(rows)}
    for h in ("r_1h", "r_4h", "r_24h"):
        signed = [r[h] * (1 if r["direction"] > 0 else -1) for r in rows if r.get(h) is not None]
        moves = [abs(r[h]) for r in rows if r.get(h) is not None]
        out[h] = {"n": len(signed),
                  "avg_signed_pct": round(sum(signed) / len(signed), 3) if signed else None,
                  "hit_pct": round(sum(1 for x in signed if x > 0) / len(signed) * 100, 1) if signed else None,
                  "avg_abs_move_pct": round(sum(moves) / len(moves), 3) if moves else None}
    return out


def _research() -> dict:
    rows = [r for r in store.reactions() if r.get("direction") is not None and abs(r["direction"]) >= MIN_DIRECTIONAL]
    # One reaction per story and coin: the first headline's. Fourteen headlines about one rally
    # are one observation, not fourteen agreeing ones.
    first: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda r: r["seen"]):
        first.setdefault((r["story"], r["symbol"]), r)
    rows = list(first.values())
    by_type: dict[str, list] = {}
    by_imp: dict[int, list] = {}
    for r in rows:
        by_type.setdefault(r["event_type"] or "other", []).append(r)
        by_imp.setdefault(r["importance"] or 1, []).append(r)
    return {
        "overall": _stats(rows),
        "by_event_type": sorted(({"event_type": k, **_stats(v)} for k, v in by_type.items()), key=lambda x: -x["n"]),
        "by_importance": [{"importance": k, **_stats(by_imp[k])} for k in sorted(by_imp, reverse=True)],
        "min_directional": MIN_DIRECTIONAL,
        "note": "One observation per story, measured from the first time the app saw it — not its publish time.",
    }


@router.get("/research")
async def research():
    """Did price move the way Claude said it would? Per event type and per importance, the
    average move in the called direction (positive = right) after 1 h, 4 h and 24 h."""
    return await _cached(("research",), 300, _research)


@router.get("/budget")
async def budget():
    return {"calls_today": classify.calls_today(), "daily_calls": classify.DAILY_CALLS, "pending": store.pending_count()}
