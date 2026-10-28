"""How are we feeling right now? One reading from everything the app currently believes.

Components (each −1 … +1), weighted, re-normalised over whichever are available:
  news    0.35  scraped news: half the market-wide score, half the average of the scanned coins'
  claude  0.30  Claude's opinions — scanner signals fired in the last 24 h (BUY + / SELL −, by
                confidence), plus the Claude swing trader's latest market view (text, shown)
  regime  0.20  BTC trend vs its 20-day average and its 7-day move
  fa      0     average FA score of the scanned coins — shown as context, NOT scored. It carried
                0.15 until 2026-10-08, when signal scoring showed it pointing the wrong way
                (IC −0.25 against the next 24 h over ~10 days; no predictive power in the
                year-long test either).
Fear & Greed is shown alongside but not scored (the macro score already reads it contrarian).

Deterministic and cached for a minute; Claude's written verdict comes with each App headline
edition (brief.py), which reads this.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("news.mood")

WEIGHTS = {"news": 0.35, "claude": 0.30, "regime": 0.20, "fa": 0.0}
TTL = 60
SIGNAL_WINDOW = 24 * 3600
LABELS = ((0.40, "Risk-on"), (0.15, "Leaning bullish"), (-0.15, "Neutral / mixed"), (-0.40, "Leaning bearish"))
_cache: dict = {"at": 0.0, "value": None}


def label(score: float) -> str:
    for cut, name in LABELS:
        if score >= cut:
            return name
    return "Risk-off"


def combine(parts: dict[str, dict]) -> dict:
    """Weighted score over the components that have a value. Pure — tests call it directly."""
    # Zero-weight components (FA) are shown, but neither scored nor counted in the agreement.
    have = {k: v for k, v in parts.items() if v.get("score") is not None and WEIGHTS.get(k, 0) > 0}
    total_w = sum(WEIGHTS[k] for k in have)
    score = sum(WEIGHTS[k] * max(-1.0, min(1.0, v["score"])) for k, v in have.items()) / total_w if total_w else 0.0
    signs = [1 if v["score"] > 0.05 else -1 if v["score"] < -0.05 else 0 for v in have.values()]
    agree = max(signs.count(1), signs.count(-1)) if signs else 0
    return {"score": round(score, 3), "label": label(score), "agreement": f"{agree} of {len(have)} point the same way",
            "components": {k: {**v, "weight": WEIGHTS[k]} for k, v in parts.items()}}


async def _news_part() -> dict:
    from news import scoring
    from scanner import scheduler
    snap = scoring.snapshot()
    scanned = [c["symbol"] for c in (scheduler._state.get("universe") or []) if c.get("symbol")]
    coin_scores = [snap["coins"][c]["score"] for c in scanned if c in snap["coins"] and snap["coins"][c]["stories"]]
    market = snap["market"]["score"] if snap["market"] else 0.0
    avg = sum(coin_scores) / len(coin_scores) if coin_scores else 0.0
    return {"score": round(0.5 * market + 0.5 * avg, 3), "market": market, "coins_avg": round(avg, 3),
            "coins_with_news": len(coin_scores), "detail": f"market news {market:+.2f}, scanned coins {avg:+.2f}"}


async def _claude_part() -> dict:
    from scanner import escalation
    now = time.time()
    fired = [f for f in await escalation.recent_fired() if now - f.get("fired_at", 0) <= SIGNAL_WINDOW]
    votes = [(1 if f.get("action") == "BUY" else -1) * (f.get("confidence") or 50) / 100 for f in fired
             if f.get("action") in ("BUY", "SELL")]
    view = None
    try:
        from experiments import claude_swing
        r = claude_swing.reviews()
        if r:
            view = {"text": r[0].get("market_view"), "ts": r[0].get("ts")}
    except Exception:
        pass
    buys = sum(1 for f in fired if f.get("action") == "BUY")
    return {"score": round(sum(votes) / len(votes), 3) if votes else None,
            "signals": len(votes), "buys": buys, "sells": len(votes) - buys,
            "latest": [{"symbol": f["symbol"], "action": f["action"], "confidence": f.get("confidence"),
                        "fired_at": f["fired_at"]} for f in fired[:5]],
            "swing_view": view,
            "detail": f"{buys} BUY / {len(votes) - buys} SELL signals in 24 h" if votes else "no Claude signals in 24 h"}


async def _regime_part() -> dict:
    from trading import regime
    r = await regime.current()
    if not r:
        return {"score": None, "detail": "BTC data unavailable"}
    trend = 0.4 if r["btc_trend"] == "up" else -0.4
    move = max(-0.6, min(0.6, (r.get("btc_7d_pct") or 0) / 10))
    return {"score": round(trend + move, 3), **r,
            "detail": f"BTC {'above' if r['btc_trend'] == 'up' else 'below'} its 20-day average, "
                      f"{(r.get('btc_7d_pct') or 0):+.1f}% in 7 d, 30-day vol {r.get('btc_vol_30d')}%"}


async def _fa_part() -> dict:
    from scanner import scheduler
    rows = (await scheduler.get_snapshot()).get("rows") or []
    scores = [r["fa"]["score"] for r in rows if (r.get("fa") or {}).get("score") is not None and not r.get("held_only")]
    if not scores:
        return {"score": None, "detail": "no scan yet"}
    avg = sum(scores) / len(scores)
    return {"score": round(avg, 3), "coins": len(scores),
            "detail": f"average FA {avg:+.2f} over {len(scores)} scanned coins — context only, not scored "
                      f"(it pointed the wrong way in signal scoring)"}


async def current(force: bool = False) -> dict:
    if not force and _cache["value"] and time.time() - _cache["at"] < TTL:
        return _cache["value"]
    parts = {}
    for name, fn in (("news", _news_part), ("claude", _claude_part), ("regime", _regime_part), ("fa", _fa_part)):
        try:
            parts[name] = await fn()
        except Exception as e:
            logger.debug("Mood component %s unavailable: %s", name, e)
            parts[name] = {"score": None, "detail": "unavailable"}
    out = combine(parts)
    try:
        from data import feeds
        fg = await feeds.cached_fear_greed()
        out["fear_greed"] = {"value": fg.get("value"), "classification": fg.get("classification")}
    except Exception:
        out["fear_greed"] = None
    out["computed_at"] = time.time()
    _cache.update(at=time.time(), value=out)
    return out
