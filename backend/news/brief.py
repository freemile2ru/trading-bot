"""App headlines — Claude's trading-desk briefing, written from what the app knows right now.

Each edition is stored (news.db news_briefs), so earlier ones can be paged back to: the
picture changes almost in real time and "what did it say an hour ago" is worth keeping.

When a new edition is written (checked every collector tick):
  first      there is none yet
  important  a new story of importance ≥ NEWS_ALERT_IMPORTANCE arrived since the last one
             (at most every MIN_GAP)
  scheduled  EVERY has passed and the inputs changed — if nothing changed, no call is made
             (up to MAX_SAME, when the same picture is re-dated with a fresh edition anyway)
  manual     the Refresh button (at most every MANUAL_GAP)
Claude calls come from their own daily budget (NEWS_BRIEF_DAILY_CALLS).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

from news import mood, scoring, store
from storage import state as kv

logger = logging.getLogger("news.brief")

# Halved on 2026-10-08 (was 60 a day, every 30 min, re-dated after 3 h): it wrote ~20 editions a
# day while the news score it leans on hadn't shown predictive value. Important news still
# triggers an edition after MIN_GAP.
DAILY_CALLS = int(os.getenv("NEWS_BRIEF_DAILY_CALLS", "30"))
EVERY = int(os.getenv("NEWS_BRIEF_EVERY", "3600"))
MIN_GAP = 600
MANUAL_GAP = 120
MAX_SAME = 6 * 3600
ALERT_IMPORTANCE = int(os.getenv("NEWS_ALERT_IMPORTANCE", "4"))
STANCES = ["bullish", "bearish", "neutral", "caution"]
RELEVANCE = ["position", "signal", "watchlist", "market", "macro"]

SYSTEM_PROMPT = f"""You are the head of a small crypto futures trading desk writing the desk's live headlines.
The desk trades USDT perpetuals over hours to days: a manual book ("main"), a rules-based autotrader that asks
Claude before entering ("auto"), and scanner signals. You get the desk's current state: its mood reading, the
biggest distinct news stories, per-coin news scores, open positions and recent signals.

Write headlines RELATIVE TO TRADING — what matters for positions we hold, signals we're acting on, coins on the
scanner, and the market backdrop — not a general news digest. Lead with anything touching an open position.
Every headline must be grounded in the inputs; cite story ids you used. Don't invent prices or events.

Reply with ONLY JSON:
{{
  "verdict": {{
    "label": "Risk-on" | "Leaning bullish" | "Neutral / mixed" | "Leaning bearish" | "Risk-off",
    "summary": "<2–3 sentences: how we're feeling right now and why — weigh the news, Claude's signals and the regime; say where they disagree. FA is shown for context only: it has pointed the wrong way, so don't lean on it>",
    "watch": "<one sentence: what would change this view>"
  }},
  "headlines": [
    {{"title": "<≤ 12 words>", "body": "<≤ 35 words: what it means for the desk>",
      "stance": one of {json.dumps(STANCES)}, "relevance": one of {json.dumps(RELEVANCE)},
      "coins": ["<symbols>"], "stories": ["<story ids>"]}}
  ]
}}
3 to 6 headlines, most trading-relevant first. You may disagree with the mood gauge's label — say why in the summary."""


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _budget_key() -> str:
    return kv.key("news", "brief_calls", _day())


def calls_today() -> int:
    return int(kv.get_sync(_budget_key()) or 0)


def _positions() -> list[dict]:
    try:
        from trading import coin_lock
        return [{"symbol": sym, "system": h["system"], "mode": h["mode"], "direction": h["direction"],
                 "pending_order": bool(h.get("pending")), "pnl_usd": h.get("pnl_usd")}
                for sym, hs in coin_lock.holders().items() for h in hs]
    except Exception:
        return []


async def gather() -> dict:
    """Everything the edition is written from (also what decides whether a new one is needed)."""
    m = await mood.current()
    snap = scoring.snapshot()
    stories = scoring.important(hours=24, limit=12)
    coins = sorted(({"coin": c, "score": d["score"], "stories": d["stories"]} for c, d in snap["coins"].items() if d["stories"]),
                   key=lambda x: -abs(x["score"]))[:10]
    return {"mood": m, "stories": stories, "coin_news": coins, "positions": _positions(),
            "signals": (m["components"].get("claude") or {}).get("latest") or []}


def inputs_key(inp: dict) -> str:
    """Changes when something an edition would say changes — not on every decimal of drift."""
    m = inp["mood"]
    key = {"mood": [m["label"], round(m["score"], 1)],
           "stories": sorted((s["story"], s["importance"]) for s in inp["stories"][:8]),
           "positions": sorted((p["symbol"], p["direction"]) for p in inp["positions"]),
           "signals": sorted((s["symbol"], s["action"]) for s in inp["signals"])}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def due(inp: dict, now: float, manual: bool = False) -> str | None:
    last = store.last_brief_meta()
    if last is None:
        return "first"
    age = now - last["ts"]
    if manual:
        return "manual" if age >= MANUAL_GAP else None
    changed = inputs_key(inp) != last["inputs_key"]
    # An important story newer than the last edition.
    if age >= MIN_GAP and any(s["importance"] >= ALERT_IMPORTANCE and s["age_h"] * 3600 < age for s in inp["stories"]):
        return "important"
    if age >= EVERY and changed:
        return "scheduled"
    if age >= MAX_SAME:
        return "scheduled"
    return None


def _prompt(inp: dict) -> str:
    m = inp["mood"]
    comps = {k: {kk: vv for kk, vv in v.items() if kk in ("score", "detail", "weight", "swing_view")}
             for k, v in m["components"].items()}
    body = {
        "now_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "mood_gauge": {"score": m["score"], "label": m["label"], "agreement": m["agreement"], "components": comps,
                       "fear_greed": m.get("fear_greed")},
        "top_stories_24h": [{k: s.get(k) for k in ("story", "title", "event_type", "direction", "importance", "coins",
                                                   "scope", "age_h", "note", "outlets")} for s in inp["stories"]],
        "coin_news_scores": inp["coin_news"],
        "open_positions": inp["positions"],
        "claude_signals_24h": inp["signals"],
    }
    return json.dumps(body, ensure_ascii=False, default=str)


def validate(ans: dict, inp: dict) -> tuple[dict, list[dict]]:
    stories = {s["story"] for s in inp["stories"]}
    v = ans.get("verdict") or {}
    labels = [lb for _, lb in mood.LABELS] + ["Risk-off"]
    verdict = {"label": v.get("label") if v.get("label") in labels else inp["mood"]["label"],
               "summary": str(v.get("summary") or "")[:800], "watch": str(v.get("watch") or "")[:300]}
    heads = []
    for h in (ans.get("headlines") or [])[:6]:
        if not isinstance(h, dict) or not h.get("title"):
            continue
        heads.append({"title": str(h["title"])[:140], "body": str(h.get("body") or "")[:400],
                      "stance": h.get("stance") if h.get("stance") in STANCES else "neutral",
                      "relevance": h.get("relevance") if h.get("relevance") in RELEVANCE else "market",
                      "coins": [str(c).upper() for c in (h.get("coins") or [])][:6],
                      "stories": [s for s in (h.get("stories") or []) if s in stories]})
    return verdict, heads


async def generate(trigger: str, inp: dict | None = None) -> dict | None:
    """Write and store one edition. Returns it, or None (budget spent or Claude failed)."""
    from oracle.claude_oracle import MODEL, get_client, parse_json, response_text
    if calls_today() >= DAILY_CALLS:
        return None
    inp = inp or await gather()
    await kv.incr_with_expire(_budget_key(), 172800)
    try:
        resp = await get_client().messages.create(
            model=MODEL, max_tokens=10000,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": _prompt(inp)}])
        verdict, heads = validate(parse_json(response_text(resp)), inp)
    except Exception as e:
        logger.warning("App headlines failed: %s", e)
        return None
    if not heads:
        return None
    # The stories each headline cites travel with the edition, so an old edition still shows them.
    by_id = {s["story"]: s for s in inp["stories"]}
    for h in heads:
        h["story_briefs"] = [{k: by_id[s].get(k) for k in ("story", "id", "title", "url", "importance", "direction", "event_type")}
                             for s in h["stories"]]
    m = {k: inp["mood"][k] for k in ("score", "label", "agreement", "components", "fear_greed")}
    bid = store.save_brief(trigger, m, verdict, heads, inputs_key(inp))
    logger.info("App headlines #%d (%s): %s — %s", bid, trigger, verdict["label"], heads[0]["title"])
    return store.brief(bid)


async def maybe(now: float | None = None) -> dict | None:
    now = now or time.time()
    inp = await gather()
    trigger = due(inp, now)
    return await generate(trigger, inp) if trigger else None
