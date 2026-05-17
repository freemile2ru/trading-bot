"""Aggregate TA + FA + macro + sentiment into a unified signal package for the oracle."""


def get_regime_weights(regime: str) -> dict:
    """Dynamic component weights based on market regime."""
    if regime == "trending":
        return {"ta": 0.55, "sentiment": 0.15, "macro": 0.15, "onchain": 0.15}
    elif regime == "volatile":
        return {"ta": 0.40, "sentiment": 0.25, "macro": 0.20, "onchain": 0.15}
    else:  # ranging / uncertain
        return {"ta": 0.35, "sentiment": 0.30, "macro": 0.20, "onchain": 0.15}


def score_news_sentiment(news_data: dict) -> float:
    """Convert raw news data into a -1 to +1 score."""
    base = float(news_data.get("aggregate_sentiment", 0.0))
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

    fg = fear_greed.get("value", 50)
    score += (fg - 50) / 50
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


def score_onchain(onchain: dict) -> float:
    """On-chain signals — hash rate, mempool, gas as proxy for network health."""
    asset = onchain.get("asset", "")
    score = 0.0
    count = 0

    if asset == "BTC":
        hashrate = onchain.get("hashrate", {})
        if hashrate and not hashrate.get("error"):
            score += 0.3
            count += 1
        mempool = onchain.get("mempool", {})
        if mempool and not mempool.get("error"):
            tx_count = mempool.get("count", 0)
            if tx_count > 50000:
                score += 0.2
            elif tx_count < 5000:
                score -= 0.1
            count += 1

    elif asset == "ETH":
        gas = onchain.get("gas", {})
        if gas and not gas.get("error"):
            fast_gwei = gas.get("fast_gas_gwei", 0)
            if fast_gwei > 100:
                score += 0.2
            elif fast_gwei > 30:
                score += 0.1
            count += 1

    return max(-1.0, min(1.0, score / count if count else 0.0))


def build_signal_package(
    symbol: str,
    ta_result: dict,
    news: dict,
    macro: dict,
    fear_greed: dict,
    onchain: dict,
    social: dict,
    coingecko: dict,
) -> dict:
    """Combine all data sources into a structured signal package for the oracle."""
    ta_score = ta_result.get("consensus_score", 0.0)
    sentiment_score = score_news_sentiment(news)
    macro_score = score_macro(macro, fear_greed)
    onchain_score = score_onchain(onchain)

    social_compound = social.get("sentiment", 0.0)
    blended_sentiment = sentiment_score * 0.6 + social_compound * 0.4

    # Dynamic weights based on market regime
    regime_info = ta_result.get("dominant_regime", {})
    regime = regime_info.get("regime", "ranging")
    weights = get_regime_weights(regime)

    composite = (
        ta_score * weights["ta"]
        + blended_sentiment * weights["sentiment"]
        + macro_score * weights["macro"]
        + onchain_score * weights["onchain"]
    )

    # Apply TF alignment multiplier once to the final composite
    alignment = ta_result.get("tf_alignment", {})
    alignment_mult = alignment.get("multiplier", 1.0)
    composite = max(-1.0, min(1.0, composite * alignment_mult))

    direction = "long" if composite > 0.1 else "short" if composite < -0.1 else "neutral"
    confidence = min(100, int(abs(composite) * 100 * 1.5))

    best_tf = ta_result.get("best_timeframe", "1h")
    timeframes = ta_result.get("timeframes", {})

    hold_map = {
        "15m": {"strong": "2-6 hours", "moderate": "1-3 hours", "weak": "30-60 min"},
        "1h": {"strong": "12-48 hours", "moderate": "4-12 hours", "weak": "1-4 hours"},
        "4h": {"strong": "3-7 days", "moderate": "1-3 days", "weak": "4-12 hours"},
        "1d": {"strong": "1-4 weeks", "moderate": "3-7 days", "weak": "1-3 days"},
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
            "adx": d.get("adx", {}).get("adx"),
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
            "candlestick_patterns": d.get("candlestick_patterns", {}).get("detected", []),
            "pattern_signal": d.get("candlestick_patterns", {}).get("signal"),
            "fib_position": d.get("fibonacci", {}).get("price_position"),
            "fib_nearest_support": d.get("fibonacci", {}).get("nearest_support"),
            "fib_nearest_resistance": d.get("fibonacci", {}).get("nearest_resistance"),
            "fib_near_support": d.get("fibonacci", {}).get("near_support", False),
            "fib_near_resistance": d.get("fibonacci", {}).get("near_resistance", False),
            "market_regime": d.get("market_regime", {}).get("regime"),
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
            "macro": round(macro_score, 4),
            "onchain": round(onchain_score, 4),
        },
        "ta_summary": {
            "consensus_label": ta_result.get("consensus_label"),
            "timeframe_signals": tf_signals,
        },
        "fear_greed": fear_greed,
        "news_summary": {
            "aggregate_sentiment": news.get("aggregate_label"),
            "total_articles": news.get("total_articles"),
            "top_headlines": [
                a.get("title") for a in
                (news.get("cryptopanic", []) + news.get("crypto_news", []))[:5]
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
