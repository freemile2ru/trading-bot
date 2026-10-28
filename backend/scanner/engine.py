"""Deterministic per-symbol scan — the free tier that runs on every sweep.

Reuses the existing strategy unchanged (compute_multi_timeframe_ta → build_signal_package)
and layers the new FA score and named setups on top to produce a single `conviction`
number the All Signals table ranks on. No LLM call happens here; escalation to Claude is
gated separately in escalation.py.
"""

import asyncio
import logging
import time

from analysis.scoring import build_signal_package
from analysis.technical import compute_multi_timeframe_ta
from data import feeds
from scanner.fundamentals import compute_fundamentals, research_features
from scanner.strategies import detect_setups

logger = logging.getLogger("scanner.engine")

FULL_INTERVALS = ("15m", "30m", "1h", "4h")
# The 1-minute tier uses the same timeframes — 4h carries the most weight in the blend and
# sets the regime, so dropping it made watchlist conviction disagree with the full sweep.
# Slow candles are cached (1h 5 min, 4h 15 min), so only the fast ones are really refetched.
LIGHT_INTERVALS = FULL_INTERVALS


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def compute_conviction(composite: float, setups: list[dict], fa_score: float | None = None) -> dict:
    """Blend the existing composite with setup confirmation. (FA agreement used to scale this
    too; retired 2026-09-29 — no predictive power. `fa_score` is accepted and ignored.)

    Setups pointing against the composite damp conviction rather than boosting it — a
    strong weighted average that every recognisable pattern disagrees with is exactly
    the signal that should not reach Claude, let alone the user.
    """
    if composite > 0:
        sign = 1
    elif composite < 0:
        sign = -1
    else:
        return {
            "conviction": 0.0,
            "setup_multiplier": 1.0,
            "aligned_setups": [],
            "opposing_setups": [],
        }

    wanted = "long" if sign > 0 else "short"
    aligned = [s for s in setups if s["direction"] == wanted]
    opposing = [s for s in setups if s["direction"] != wanted]

    setup_mult = min(1.0 + 0.15 * sum(s["strength"] for s in aligned), 1.5)
    if opposing:
        setup_mult *= max(0.5, 1.0 - 0.20 * sum(s["strength"] for s in opposing))

    return {
        "conviction": round(_clamp(composite * setup_mult), 4),
        "setup_multiplier": round(setup_mult, 3),
        "aligned_setups": [s["name"] for s in aligned],
        "opposing_setups": [s["name"] for s in opposing],
    }


def _extract_triggers(tf_signals: dict, best_tf: str, atr_pct: float) -> dict:
    """The volatility/proximity flags the watchlist promotion rules read."""
    s = tf_signals.get(best_tf) or next((v for v in tf_signals.values() if v), {}) or {}

    # `or 99` would turn a distance of exactly 0 (price on the level) into "far away".
    dist_r1 = abs(99 if s.get("distance_to_r1_pct") is None else s["distance_to_r1_pct"])
    dist_s1 = abs(99 if s.get("distance_to_s1_pct") is None else s["distance_to_s1_pct"])

    return {
        "bb_squeeze": bool(s.get("bb_squeeze")),
        "divergence": s.get("rsi_divergence", "none"),
        "volume_ratio": float(s.get("volume_ratio") or 1.0),
        "near_level": min(dist_r1, dist_s1) < 1.0,
        "distance_to_level_pct": round(min(dist_r1, dist_s1), 3),
        "atr_pct": round(atr_pct, 4),
        "tf_labels": {tf: (d or {}).get("signal") for tf, d in tf_signals.items()},
    }


async def scan_symbol(
    pair: str,
    coin: dict,
    btc: dict,
    shared: dict,
    *,
    light: bool = False,
) -> dict:
    """One symbol's deterministic scan.

    `shared` carries the symbol-independent feeds (macro, fear & greed, global market)
    fetched once per sweep, so ten symbols don't each refetch them.
    """
    t0 = time.perf_counter()
    intervals = LIGHT_INTERVALS if light else FULL_INTERVALS

    snapshot, news, social, cg, positioning, usage = await asyncio.gather(
        feeds.cached_market_snapshot(pair, intervals),
        feeds.cached_news(pair),
        feeds.cached_social(pair),
        feeds.cached_coingecko(pair),
        feeds.cached_positioning(pair),
        feeds.cached_usage(pair),
        return_exceptions=True,
    )

    def _ok(v, fallback):
        return fallback if isinstance(v, Exception) or v is None else v

    snapshot = _ok(snapshot, {"ohlcv": {}, "ticker": {}, "order_book": {}})
    news = _ok(news, {"aggregate_sentiment": 0, "aggregate_label": "neutral", "total_articles": 0})
    social = _ok(social, {"sentiment": 0.0, "label": "neutral", "tweets_analyzed": 0})
    cg = _ok(cg, {})
    positioning = _ok(positioning, {})
    usage = _ok(usage, {})

    ta_result = compute_multi_timeframe_ta(snapshot.get("ohlcv", {}))

    signal_package = build_signal_package(
        symbol=pair,
        ta_result=ta_result,
        news=news,
        macro=shared.get("macro", {}),
        fear_greed=shared.get("fear_greed", {"value": 50, "classification": "Neutral"}),
        social=social,
        coingecko=cg,
    )

    ticker = snapshot.get("ticker") or {}
    price = ticker.get("price") or signal_package.get("price") or coin.get("price") or 0
    signal_package["price"] = price

    fa = compute_fundamentals(coin, btc, shared.get("global_market", {}))
    # Raw positioning and usage data travel with the FA block so Claude sees them; they
    # don't change fa["score"] (see research_features).
    fa["positioning"] = positioning
    fa["usage"] = usage
    fa["features"] = research_features(coin, btc, positioning, usage, shared)
    signal_package["fundamentals"] = fa   # so Claude weighs FA directly, not just its side effects
    tf_signals = signal_package.get("ta_summary", {}).get("timeframe_signals", {})
    setups = detect_setups(tf_signals, signal_package.get("tf_alignment", {}))

    conv = compute_conviction(
        signal_package.get("composite_score", 0.0), setups, fa["score"]
    )

    confidence = min(95, signal_package.get("confidence_pct", 0) + 3 * len(conv["aligned_setups"]))
    conviction = conv["conviction"]
    direction = "long" if conviction > 0.1 else "short" if conviction < -0.1 else "neutral"

    return {
        "symbol": pair,
        "base": pair.replace("USDT", ""),
        "name": coin.get("name"),
        "rank": coin.get("cmc_rank"),
        "price": price,
        "change_1h": coin.get("percent_change_1h"),
        "change_24h": coin.get("percent_change_24h") or ticker.get("price_change_pct"),
        "change_7d": coin.get("percent_change_7d"),
        "conviction": conviction,
        "direction": direction,
        "confidence": confidence,
        "composite_score": signal_package.get("composite_score", 0.0),
        "ta_score": signal_package.get("component_scores", {}).get("technical", 0.0),
        "fa": fa,
        "setups": setups,
        "conviction_detail": conv,
        "regime": signal_package.get("market_regime", {}).get("regime", "unknown"),
        "news": {k: (signal_package.get("news_summary") or {}).get(k) for k in ("score", "burst", "stories_6h")},
        "best_timeframe": signal_package.get("best_timeframe"),
        "tf_alignment": signal_package.get("tf_alignment", {}),
        "hold_duration": signal_package.get("hold_duration"),
        "levels": {
            "stop_loss": signal_package.get("stop_loss"),
            "take_profit_1": signal_package.get("take_profit_1"),
            "take_profit_2": signal_package.get("take_profit_2"),
        },
        # stop_loss_pct is ATR-derived (atr/price*100*1.5), so undo the multiplier
        # to recover ATR as a percentage of price for the volatility triggers.
        "triggers": _extract_triggers(
            tf_signals,
            signal_package.get("best_timeframe", "1h"),
            (signal_package.get("stop_loss_pct") or 0.0) / 1.5,
        ),
        "signal_package": signal_package,
        "scanned_at": time.time(),
        "tier": "light" if light else "full",
        "elapsed": round(time.perf_counter() - t0, 2),
    }
