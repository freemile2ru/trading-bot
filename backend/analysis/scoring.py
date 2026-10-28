"""Aggregate TA + FA + macro + sentiment into a unified signal package for the oracle."""
import os

NEWS_IN_SCORE = os.getenv("NEWS_IN_SCORE", "false").lower() == "true"


def get_regime_weights(regime: str) -> dict:
    """Dynamic component weights based on market regime.

    They sum to 0.85, not 1: the missing 0.15 was an on-chain score (hashrate, mempool,
    gas) that only measured network activity, so it could only ever lean bullish. It was
    removed without rescaling, which keeps composites on the scale the thresholds were
    tuned for (alts never had on-chain data, so they always summed to 0.85).
    """
    if regime == "trending":
        return {"ta": 0.55, "sentiment": 0.15, "macro": 0.15}
    elif regime == "volatile":
        return {"ta": 0.40, "sentiment": 0.25, "macro": 0.20}
    else:  # ranging / uncertain
        return {"ta": 0.35, "sentiment": 0.30, "macro": 0.20}


def score_news_sentiment(news_data: dict) -> float:
    """Convert raw news data into a -1 to +1 score."""
    base = float(news_data.get("aggregate_sentiment", 0.0))
    if news_data.get("scraped"):
        return max(-1.0, min(1.0, base))          # already importance/recency-weighted (news/scoring.py)
    cp = news_data.get("cryptopanic", [])
    if cp:
        cp_scores = [a.get("sentiment_compound", 0) for a in cp]
        cp_avg = sum(cp_scores) / len(cp_scores)
        base = base * 0.4 + cp_avg * 0.6
    return max(-1.0, min(1.0, base))


def score_macro(macro_data: dict, fear_greed: dict) -> float:
    """Convert macro indicators into a -1 to +1 score."""
    score = 0.0
    count = 0

    # Fear & Greed: contrarian at extremes, neutral in mid-range.
    # Professional crypto traders buy extreme fear, sell extreme greed.
    fg = fear_greed.get("value", 50)
    if fg <= 15:    fg_score = 0.80    # Extreme fear → contrarian strong buy
    elif fg <= 30:  fg_score = 0.50    # Fear → contrarian buy
    elif fg <= 45:  fg_score = 0.15    # Mild fear → slight bullish lean
    elif fg <= 55:  fg_score = 0.00    # Neutral
    elif fg <= 70:  fg_score = -0.15   # Mild greed → slight bearish lean
    elif fg <= 85:  fg_score = -0.50   # Greed → contrarian sell
    else:           fg_score = -0.80   # Extreme greed → contrarian strong sell
    score += fg_score
    count += 1

    if macro_data.get("yield_curve_inverted"):
        score -= 0.3
        count += 1
    elif "yield_curve_spread" in macro_data:
        spread = macro_data["yield_curve_spread"]
        score += min(0.3, spread / 2)
        count += 1

    dxy = macro_data.get("dxy_index", {})
    if isinstance(dxy, dict) and dxy.get("latest"):
        history = dxy.get("history", [])
        if len(history) >= 2:
            prev = next((v["value"] for v in history[1:] if v["value"]), None)
            curr = dxy["latest"]
            if prev and curr:
                dxy_change = (curr - prev) / prev
                score -= dxy_change * 10
                count += 1

    vix = macro_data.get("vix", {})
    if isinstance(vix, dict) and vix.get("latest"):
        vix_val = float(vix["latest"])
        if vix_val > 30:
            score -= 0.4
        elif vix_val > 20:
            score -= 0.2
        elif vix_val < 15:
            score += 0.2
        count += 1

    return max(-1.0, min(1.0, score / count if count else 0.0))


def _pick_top_headlines(news: dict) -> list[str]:
    """Return up to 6 headlines across all categories, interleaving sources."""
    crypto = [a.get("title") for a in (news.get("cryptopanic", []) + news.get("crypto_news", []))[:4]]
    macro = [a.get("title") for a in news.get("macro_news", [])[:2]]
    geo = [a.get("title") for a in news.get("geopolitical_news", [])[:1]]
    combined = []
    for h in crypto + macro + geo:
        if h and h not in combined:
            combined.append(h)
    return combined[:6]


def build_signal_package(
    symbol: str,
    ta_result: dict,
    news: dict,
    macro: dict,
    fear_greed: dict,
    social: dict,
    coingecko: dict,
) -> dict:
    """Combine all data sources into a structured signal package for the oracle."""
    ta_score = ta_result.get("consensus_score", 0.0)
    sentiment_score = score_news_sentiment(news)
    macro_score = score_macro(macro, fear_greed)

    social_compound = social.get("sentiment", 0.0)
    # The scraped-news score fed this straight from 2026-10-05, to be judged after ~14 days. On 21 days of
    # live readings (research/signal_scores.py) it didn't predict returns — IC −0.06 at 24h, −0.03 at 72h —
    # so since 2026-10-19 it's reported (News page, Claude's prompt, signal scoring) but no longer scored.
    scored_news = sentiment_score if NEWS_IN_SCORE else 0.0
    blended_sentiment = scored_news * 0.6 + social_compound * 0.4

    # Dynamic weights based on market regime
    regime_info = ta_result.get("dominant_regime", {})
    regime = regime_info.get("regime", "ranging")
    weights = get_regime_weights(regime)

    composite = (
        ta_score * weights["ta"]
        + blended_sentiment * weights["sentiment"]
        + macro_score * weights["macro"]
    )

    # Apply TF alignment multiplier once to the final composite
    alignment = ta_result.get("tf_alignment", {})
    alignment_mult = alignment.get("multiplier", 1.0)
    composite = max(-1.0, min(1.0, composite * alignment_mult))

    direction = "long" if composite > 0.1 else "short" if composite < -0.1 else "neutral"

    # Confidence from three factors:
    #   1. Signal strength — composite magnitude (max 65 pts)
    #   2. TF alignment bonus/penalty (up to ±20 pts)
    #   3. Convergence — proportion of TFs agreeing (up to ±10 pts)
    strength_conf = min(65, int(abs(composite) * 100))
    mult = alignment.get("multiplier", 1.0)
    if mult >= 1.4:     align_bonus = 20
    elif mult >= 1.2:   align_bonus = 10
    elif mult <= 0.75:  align_bonus = -20
    else:               align_bonus = 0
    bull_tfs = alignment.get("bullish_tfs", 0)
    bear_tfs = alignment.get("bearish_tfs", 0)
    total_tfs = max(alignment.get("total_tfs", 1), 1)
    dominant = max(bull_tfs, bear_tfs)
    conv_bonus = int((dominant / total_tfs - 0.5) * 20)   # -10 to +10
    confidence = min(95, max(5, strength_conf + align_bonus + conv_bonus))

    best_tf = ta_result.get("best_timeframe", "1h")
    timeframes = ta_result.get("timeframes", {})

    hold_map = {
        "15m": {"strong": "2-6 hours", "moderate": "1-3 hours", "weak": "30-60 min"},
        "30m": {"strong": "4-12 hours", "moderate": "2-6 hours", "weak": "1-2 hours"},
        "1h": {"strong": "12-48 hours", "moderate": "4-12 hours", "weak": "1-4 hours"},
        "4h": {"strong": "3-7 days", "moderate": "1-3 days", "weak": "4-12 hours"},
    }
    strength = "strong" if abs(composite) > 0.5 else "moderate" if abs(composite) > 0.25 else "weak"
    hold_duration = hold_map.get(best_tf, {}).get(strength, "4-12 hours")

    price = 0
    atr_val = 0
    if best_tf in timeframes:
        tf_data = timeframes[best_tf]
        price = tf_data.get("price", 0)
        atr_val = tf_data.get("atr", 0)

    sl_pct = (atr_val / price * 100 * 1.5) if price and atr_val else 2.0
    tp1_pct = sl_pct * 2.0
    tp2_pct = sl_pct * 3.0

    if direction == "long":
        stop_loss = round(price * (1 - sl_pct / 100), 4)
        take_profit_1 = round(price * (1 + tp1_pct / 100), 4)
        take_profit_2 = round(price * (1 + tp2_pct / 100), 4)
    elif direction == "short":
        stop_loss = round(price * (1 + sl_pct / 100), 4)
        take_profit_1 = round(price * (1 - tp1_pct / 100), 4)
        take_profit_2 = round(price * (1 - tp2_pct / 100), 4)
    else:
        stop_loss = take_profit_1 = take_profit_2 = 0.0

    # Rich per-timeframe TA summary
    tf_signals = {}
    for tf, d in timeframes.items():
        if d.get("error"):
            continue
        tf_signals[tf] = {
            "rsi": d.get("rsi"),
            "rsi_signal": d.get("rsi_signal"),
            "rsi_divergence": d.get("rsi_divergence", {}).get("type", "none"),
            "macd_cross": d.get("macd", {}).get("cross", "none"),
            "macd_histogram": d.get("macd", {}).get("histogram"),
            "bb_position": d.get("bollinger_bands", {}).get("position"),
            "bb_squeeze": d.get("bollinger_bands", {}).get("squeeze", False),
            "ema_alignment": d.get("ema_cloud", {}).get("ema_alignment"),
            "ema_21": d.get("ema_cloud", {}).get("ema_21"),
            "atr": d.get("atr"),
            "price": d.get("price"),
            "adx": d.get("adx", {}).get("adx"),
            "adx_slope": d.get("adx", {}).get("adx_slope"),
            "adx_momentum": d.get("adx", {}).get("trend_momentum"),
            "adx_strength": d.get("adx", {}).get("trend_strength"),
            "stoch_k": d.get("stochastic", {}).get("k"),
            "volume_ratio": d.get("volume", {}).get("volume_ratio"),
            "volume_signal": d.get("volume", {}).get("volume_signal"),
            "obv_trend": d.get("volume", {}).get("obv_trend"),
            "pivot": d.get("support_resistance", {}).get("pivot"),
            "resistance_1": d.get("support_resistance", {}).get("resistance_1"),
            "resistance_2": d.get("support_resistance", {}).get("resistance_2"),
            "support_1": d.get("support_resistance", {}).get("support_1"),
            "support_2": d.get("support_resistance", {}).get("support_2"),
            "distance_to_r1_pct": d.get("support_resistance", {}).get("distance_to_r1_pct"),
            "distance_to_s1_pct": d.get("support_resistance", {}).get("distance_to_s1_pct"),
            "ichimoku_signal": d.get("ichimoku", {}).get("signal"),
            "ichimoku_tk_cross": d.get("ichimoku", {}).get("tk_cross"),
            "ichimoku_above_cloud": d.get("ichimoku", {}).get("above_cloud"),
            "ichimoku_cloud_color": d.get("ichimoku", {}).get("cloud_color"),
            "vwap": d.get("vwap", {}).get("vwap"),
            "vwap_above": d.get("vwap", {}).get("price_above_vwap"),
            "vwap_deviation_pct": d.get("vwap", {}).get("deviation_pct"),
            "cmf": d.get("cmf"),
            "candlestick_patterns": d.get("candlestick_patterns", {}).get("detected", []),
            "pattern_signal": d.get("candlestick_patterns", {}).get("signal"),
            "fib_position": d.get("fibonacci", {}).get("price_position"),
            "fib_nearest_support": d.get("fibonacci", {}).get("nearest_support"),
            "fib_nearest_resistance": d.get("fibonacci", {}).get("nearest_resistance"),
            "fib_near_support": d.get("fibonacci", {}).get("near_support", False),
            "fib_near_resistance": d.get("fibonacci", {}).get("near_resistance", False),
            "fib_support_touches": d.get("fibonacci", {}).get("nearest_support_touches", 0),
            "fib_resistance_touches": d.get("fibonacci", {}).get("nearest_resistance_touches", 0),
            "market_regime": d.get("market_regime", {}).get("regime"),
            "market_structure": d.get("market_structure", {}).get("structure", "unclear"),
            "structure_score": d.get("market_structure", {}).get("score", 0.0),
            "macd_histogram_roc": d.get("macd", {}).get("histogram_roc", 0),
            "macd_histogram_expanding": d.get("macd", {}).get("histogram_expanding", False),
            "signal": d.get("signal_label"),
            "score": d.get("signal_score"),
        }

    return {
        "symbol": symbol,
        "price": price,
        "composite_score": round(composite, 4),
        "direction": direction,
        "confidence_pct": confidence,
        "best_timeframe": best_tf,
        "hold_duration": hold_duration,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_reward": "2:1 / 3:1",
        "stop_loss_pct": round(sl_pct, 2),
        "weights_used": weights,
        "market_regime": regime_info,
        "tf_alignment": alignment,
        "component_scores": {
            "technical": round(ta_score, 4),
            "sentiment": round(blended_sentiment, 4),
            "news": round(sentiment_score, 4),            # reported; in the composite only if NEWS_IN_SCORE
            "macro": round(macro_score, 4),
        },
        "ta_summary": {
            "consensus_label": ta_result.get("consensus_label"),
            "timeframe_signals": tf_signals,
        },
        "fear_greed": fear_greed,
        "news_summary": {
            "aggregate_sentiment": news.get("aggregate_label"),
            "total_articles": news.get("total_articles"),
            "top_headlines": _pick_top_headlines(news),
            "score": (news.get("scraped") or {}).get("score"),
            "burst": (news.get("scraped") or {}).get("burst"),
            "stories_6h": (news.get("scraped") or {}).get("stories_6h"),
            # The weightiest classified stories, for Claude: what happened, how big, how old.
            "key_events": [
                {k: a.get(k) for k in ("title", "event_type", "direction", "importance", "age_h", "note")}
                for a in sorted(news.get("crypto_news", []) + news.get("macro_news", []),
                                key=lambda a: -(a.get("weight") or 0))[:6]
                if not a.get("provisional")
            ],
        },
        "macro_summary": {
            "yield_curve_inverted": macro.get("yield_curve_inverted"),
            "fed_funds_rate": macro.get("fed_funds_rate", {}).get("latest"),
            "vix": macro.get("vix", {}).get("latest"),
        },
        "social": {
            "tweets_analyzed": social.get("tweets_analyzed", 0),
            "sentiment_label": social.get("label", "neutral"),
            "bullish_pct": social.get("bullish_pct", 0),
        },
        "coingecko": {
            "market_cap_rank": coingecko.get("market_cap_rank"),
            "price_change_24h_pct": coingecko.get("price_change_24h_pct"),
            "price_change_7d_pct": coingecko.get("price_change_7d_pct"),
            "sentiment_votes_up_pct": coingecko.get("sentiment_votes_up_pct"),
            "total_volume_usd": coingecko.get("total_volume_usd"),
        },
    }
