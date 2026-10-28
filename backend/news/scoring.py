"""Per-coin news score from the scraped, classified headlines.

  item weight   importance weight × recency decay (half-life NEWS_HALF_LIFE_H, from the
                publish time — never later than first seen); nothing older than 48 h counts
  coin score    tanh(Σ direction × weight over the coin's own stories / 1.5)
                — one fresh importance-4 story, fully bullish, reads ≈ +0.83
  market score  the same over crypto-wide and macro stories, / 3 (there are many more of them)
  news score    0.75 × coin + 0.25 × market when the coin has its own stories, else 0.4 × market
  burst         the coin's stories in the last 6 h vs its 6-hourly average over the previous
                week — attention arriving before (or without) a clear direction

Unclassified headlines count at importance 1 with VADER's reading, halved, until Claude
has read them.
"""
from __future__ import annotations

import json
import math
import os
import re
import time

from news import store
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

HALF_LIFE_H = float(os.getenv("NEWS_HALF_LIFE_H", "6"))
IMPORTANCE_W = {1: 0.15, 2: 0.4, 3: 1.0, 4: 1.8, 5: 3.0}
BURST_WINDOW = 6 * 3600
BURST_BASELINE = 7 * 86400
BURST_MIN_HISTORY = 3 * 86400        # collection must have run this long before bursts are reported
SNAPSHOT_TTL = 60

_vader = SentimentIntensityAnalyzer()
_snap: dict = {"at": 0.0, "version": -1, "coins": {}, "market": None, "items": []}


def vader(text: str) -> float:
    return _vader.polarity_scores(text)["compound"]


# ── Coin mentions (before Claude, and for per-coin searches) ────────────────────

def mention_matcher(universe: dict[str, str]):
    """Returns f(text) -> [symbols]. Names match case-insensitively as whole words; tickers
    only in capitals, and only when 3+ letters (so "OP" or "S" in a sentence don't match)."""
    pats = []
    for sym, name in universe.items():
        parts = []
        # A name that is just the ticker ("Dash", "Near") is an ordinary word too — ticker rule only.
        if name and len(name) >= 4 and name.lower() != sym.lower():
            parts.append(re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE))
        if len(sym) >= 3:
            parts.append(re.compile(rf"(?<![A-Za-z$]){re.escape(sym)}\b|\${re.escape(sym)}\b"))
        pats.append((sym, parts))

    def match(text: str) -> list[str]:
        return [sym for sym, ps in pats if any(p.search(text) for p in ps)]
    return match


# ── Scoring ────────────────────────────────────────────────────────────────────

def _age_h(it: dict, now: float) -> float:
    t = min(it.get("published") or it["seen"], it["seen"])
    return max(0.0, (now - t) / 3600)


def reading(it: dict) -> dict:
    """(coins, scope, direction, importance) — Claude's labels, or the provisional reading."""
    if it.get("classified_at"):
        return {"coins": it.get("coins") or [], "scope": it.get("scope") or "irrelevant",
                "direction": it.get("direction") or 0.0, "importance": it.get("importance") or 1, "provisional": False}
    mentions = it.get("mentions") or []
    return {"coins": mentions, "scope": "coin" if mentions else "market",
            "direction": round((it.get("vader") or 0.0) * 0.5, 3), "importance": 1, "provisional": True}


def weight(it: dict, now: float) -> float:
    age = _age_h(it, now)
    if age * 3600 > store.MAX_AGE:
        return 0.0
    return IMPORTANCE_W.get(reading(it)["importance"], 0.15) * 0.5 ** (age / HALF_LIFE_H)


def _brief(it: dict, now: float, w: float) -> dict:
    rd = reading(it)
    return {"id": it["id"], "title": it["title"], "url": it.get("url"), "source": it.get("source"),
            "published": it.get("published"), "seen": it["seen"], "age_h": round(_age_h(it, now), 1),
            "event_type": it.get("event_type"), "direction": rd["direction"], "importance": rd["importance"],
            "note": it.get("note"), "provisional": rd["provisional"], "weight": round(w, 3),
            "outlets": len(it.get("sources") or []), "coins": rd["coins"], "scope": rd["scope"],
            "story": it.get("story") or it["id"], "similar": 0}


CRYPTO_WORDS = re.compile(r"\b(crypto\w*|tokens?|coins?|blockchain|defi|altcoins?|bitcoin|btc|ethereum|stablecoins?|"
                          r"memecoins?|airdrop|mainnet|testnet|staking|onchain|on-chain|web3|binance|coinbase|etf|whales?)\b",
                          re.IGNORECASE)


def distinctive(sym: str, name: str | None) -> bool:
    """A name nobody uses outside crypto ("Hedera", "Chainlink") — unlike "Dash", "Sui" or "Aave"."""
    return bool(name) and len(name) >= 6 and name.lower() != sym.lower()


def crypto_context(text: str, sym: str, name: str | None) -> bool:
    """Is a per-coin search result really about the coin? A search for "Dash" returns TV spoilers,
    dash-cams and DoorDash (stock ticker DASH); keep those out before they cost a Claude call."""
    return distinctive(sym, name) or bool(CRYPTO_WORDS.search(text)) or f"${sym}" in text


def story_groups(items: list[dict], now: float) -> list[dict]:
    """Collapse headlines into stories. A story counts ONCE: its weight is its strongest headline's
    weight and its direction the weight-averaged direction of its headlines — ten outlets (or ten
    rewordings) of one event add coverage, not points."""
    groups: dict[str, dict] = {}
    for it in items:
        w = weight(it, now)
        rd = reading(it)
        if w <= 0 or rd["scope"] == "irrelevant":
            continue
        g = groups.setdefault(it.get("story") or it["id"], {"story": it.get("story") or it["id"], "items": []})
        g["items"].append((it, rd, w))
    out = []
    for g in groups.values():
        lead_it, lead_rd, lead_w = max(g["items"], key=lambda x: (x[2], not x[1]["provisional"]))
        total_w = sum(w for _, _, w in g["items"])
        direction = sum(rd["direction"] * w for _, rd, w in g["items"]) / total_w
        coins = sorted({c for _, rd, _ in g["items"] for c in rd["coins"] if rd["scope"] == "coin"})
        scope = lead_rd["scope"] if lead_rd["scope"] != "coin" or coins else "market"
        brief = _brief(lead_it, now, lead_w)
        brief.update(direction=round(direction, 3), story=g["story"], similar=len(g["items"]) - 1,
                     outlets=len({s["source"] for it, _, _ in g["items"] for s in (it.get("sources") or [])}) or 1,
                     coins=coins, scope=scope)
        out.append({"story": g["story"], "weight": lead_w, "direction": direction, "coins": coins, "scope": scope,
                    "brief": brief})
    return out


def compute(items: list[dict], history: list[dict], now: float, history_since: float | None = None) -> dict:
    """Scores for every coin mentioned, plus the market reading. Pure — tests call this directly."""
    coin_raw: dict[str, float] = {}
    coin_items: dict[str, list] = {}
    mkt_raw, mkt_items = 0.0, []
    for st in story_groups(items, now):
        contrib = st["direction"] * st["weight"]
        if st["scope"] == "coin":
            for c in st["coins"]:
                coin_raw[c] = coin_raw.get(c, 0.0) + contrib
                coin_items.setdefault(c, []).append(st["brief"])
        else:
            mkt_raw += contrib
            mkt_items.append(st["brief"])
    market = math.tanh(mkt_raw / 3)

    # Burst: distinct stories per coin in the last 6 h against the week before.
    recent: dict[str, set] = {}
    base: dict[str, set] = {}
    for h in history:
        cs = h.get("coins") or h.get("mentions") or []
        bucket = recent if now - h["seen"] <= BURST_WINDOW else base
        for c in cs:
            bucket.setdefault(c, set()).add(h.get("story") or id(h))
    per_window = BURST_BASELINE / BURST_WINDOW
    # Until there's a few days of history the baseline is ~0 and every coin reads as a huge burst.
    burst_ready = history_since is not None and now - history_since >= BURST_MIN_HISTORY

    coins = {}
    for c in set(coin_raw) | set(recent):
        cs = math.tanh(coin_raw.get(c, 0.0) / 1.5)
        has = c in coin_raw
        top = sorted(coin_items.get(c, []), key=lambda x: -x["weight"])[:8]
        n_recent, n_base = len(recent.get(c, ())), len(base.get(c, ()))
        coins[c] = {
            "score": round(0.75 * cs + 0.25 * market if has else 0.4 * market, 4),
            "coin_score": round(cs, 4), "stories": len(coin_items.get(c, [])),
            "burst": round((n_recent + 1) / (n_base / per_window + 1), 2) if burst_ready else None,
            "stories_6h": n_recent, "top": top,
        }
    return {"market": {"score": round(market, 4), "stories": len(mkt_items),
                       "top": sorted(mkt_items, key=lambda x: -x["weight"])[:8]},
            "coins": coins}


def snapshot(now: float | None = None) -> dict:
    """All coins' scores, recomputed at most once a minute (the scanner asks per coin)."""
    now = now or time.time()
    # Cached: recomputed when the store changed, or a minute later (weights decay with time).
    if now - _snap["at"] < SNAPSHOT_TTL and _snap["market"] is not None and _snap["version"] == store.version():
        return _snap
    items = store.items(now - store.MAX_AGE)
    history = [{"seen": h["seen"], "coins": _json(h["coins"]), "mentions": _json(h["mentions"]), "story": h["story"]}
               for h in store.seen_counts(now - BURST_BASELINE - BURST_WINDOW)]
    out = compute(items, history, now, store.first_seen())
    _snap.update(at=now, version=store.version(), coins=out["coins"], market=out["market"])
    return _snap


def _json(v):
    return json.loads(v) if v else None


def for_coin(base: str) -> dict:
    s = snapshot()
    c = s["coins"].get(base)
    market = s["market"] or {"score": 0.0, "stories": 0, "top": []}
    if not c:
        return {"score": round(0.4 * market["score"], 4), "coin_score": 0.0, "stories": 0, "burst": None,
                "stories_6h": 0, "top": [], "market": market}
    return {**c, "market": market}


def impact(b: dict) -> float:
    """How much a story should matter to a trader: importance first, then how clearly it points."""
    return b["importance"] + abs(b["direction"])


def important(hours: float = 24, limit: int = 12, min_importance: int = 3, now: float | None = None) -> list[dict]:
    """The biggest distinct stories of the last `hours` — one entry per story, never per headline."""
    now = now or time.time()
    items = [it for it in store.items(now - hours * 3600) if it.get("classified_at")]
    stories = [st["brief"] for st in story_groups(items, now)]
    top = [b for b in stories if b["importance"] >= min_importance and not b["provisional"]]
    return sorted(top, key=lambda b: (-impact(b), b["age_h"]))[:limit]
