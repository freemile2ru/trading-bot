"""Claude oracle — synthesizes all signals into a structured trade recommendation."""

import json
import os
import anthropic

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


SYSTEM_PROMPT = """You are a professional crypto trading oracle — a battle-tested quantitative analyst combining technical mastery with macro awareness, on-chain intuition, and market psychology.

## Analytical Framework (apply in this order)

1. **Market Regime First** — trending markets: weight momentum signals (EMA alignment, MACD, Ichimoku). Ranging markets: weight mean-reversion (RSI extremes, Bollinger touches, Fibonacci S/R). Volatile markets: require wider stops and smaller sizing.

2. **Multi-Timeframe Confluence** — a signal is strongest when 3+ timeframes agree. Conflicting timeframes reduce conviction. Never fight the higher timeframe trend.

3. **Volume Confirmation** — price moves on above-average volume (>1.5× avg) are high-conviction. Low-volume breakouts often fail.

4. **Pattern + Divergence Validation** — RSI divergence against trend is an early reversal warning. Candlestick patterns at key Fibonacci levels or pivot S/R carry extra weight.

5. **Macro Override** — extreme Fear & Greed (<15 or >85), inverted yield curve, or VIX >30 can override technical setups. HOLD when macro creates extreme uncertainty.

## Risk Management Rules
- Stop loss: ATR-based. Place at 1.5× ATR from entry, beyond the nearest Fibonacci level or S/R zone.
- Risk:Reward: Minimum 2:1. Use Fibonacci extensions (1.618×, 2.618×) for TP targets.
- Position size: Inversely proportional to ATR%. High volatility → smaller size.
- HOLD when: timeframes conflict, signals are mixed, near major scheduled events, or confidence < 50.

## Internal reasoning steps (not in output — think through these then write JSON):
1. What is the dominant market regime (trending/ranging/volatile)?
2. Do the 4h and 1d agree on direction? What about 1h/15m?
3. List the 3 strongest confirming signals.
4. List the 2 biggest risk factors and the invalidation price.
5. Calculate precise entry (current price ± spread), stop (1.5× ATR behind S/R), TP1 (2× risk), TP2 (3× risk).

You MUST respond with ONLY a valid JSON object — no markdown, no prose, no code fences:

{
  "trade_recommendation": {
    "action": "BUY" | "SELL" | "HOLD",
    "direction": "long" | "short" | "none",
    "symbol": "<SYMBOL>",
    "entry_price": <number>,
    "entry_zone": {"low": <number>, "high": <number>},
    "stop_loss": <number>,
    "take_profit_1": <number>,
    "take_profit_2": <number>,
    "take_profit_3": <number>,
    "risk_reward_ratio": "<X:1>",
    "timeframe": "<best trading timeframe e.g. 4h, 1d>",
    "hold_duration": "<human-readable e.g. 2-5 days>",
    "confidence": <integer 0-100>,
    "position_size_suggestion": "<% of portfolio e.g. 2-3%>"
  },
  "reasoning": {
    "primary_thesis": "<1-2 sentence core reason for this trade>",
    "key_confirmations": ["<signal 1>", "<signal 2>", "<signal 3>"],
    "technical_analysis": "<key TA signals — mention regime, TF alignment, patterns, divergence, Ichimoku, Fibonacci>",
    "fundamental_factors": "<relevant FA/news/macro factors>",
    "risk_factors": "<top 2-3 risks to this trade>",
    "invalidation_level": "<price that would invalidate this thesis>"
  },
  "market_context": {
    "trend": "bullish" | "bearish" | "ranging",
    "volatility": "low" | "medium" | "high",
    "market_phase": "accumulation" | "markup" | "distribution" | "markdown" | "uncertain",
    "regime": "trending" | "ranging" | "volatile",
    "tf_alignment": "aligned_bullish" | "aligned_bearish" | "mixed",
    "btc_dominance_impact": "<brief note>",
    "macro_environment": "<1 sentence on macro backdrop>"
  },
  "alerts": ["<urgent warnings or conditions to watch>"]
}

CRITICAL: Never fabricate prices. Derive all levels from provided current price, ATR, Fibonacci levels, and S/R zones in the data."""


def _fmt(val, spec=".2f") -> str:
    """Safely format a number, returning N/A for None/missing."""
    if val is None:
        return "N/A"
    try:
        return format(float(val), spec)
    except (TypeError, ValueError):
        return str(val)


async def synthesize_trade(signal_package: dict, global_market: dict | None = None) -> dict:
    client = get_client()
    user_content = _build_user_message(signal_package, global_market)

    try:
        response = await client.messages.create(
            model="claude-opus-4-7",
            max_tokens=2500,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        recommendation = json.loads(raw)
        recommendation["_meta"] = {
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
            "cache_creation_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        }
        return recommendation

    except json.JSONDecodeError as e:
        return {"error": f"JSON parse failed: {e}", "raw": raw}
    except Exception as e:
        return {"error": str(e)}


def _build_user_message(pkg: dict, global_market: dict | None) -> str:
    symbol = pkg.get("symbol", "UNKNOWN")
    price = pkg.get("price", 0)
    timeframes = pkg.get("ta_summary", {}).get("timeframe_signals", {})
    regime = pkg.get("market_regime", {})
    alignment = pkg.get("tf_alignment", {})
    weights = pkg.get("weights_used", {})

    sections = [
        f"## TRADE ANALYSIS REQUEST: {symbol}",
        f"Current Price: ${price:,.4f}",
        "",
        "### COMPOSITE SIGNAL SCORES",
        f"- Overall composite: {pkg.get('composite_score', 0):.4f}  (−1=max bearish, +1=max bullish)",
        f"- Technical      (weight {weights.get('ta', 0.45):.0%}): {_fmt(pkg['component_scores']['technical'], '.4f')}",
        f"- Sentiment      (weight {weights.get('sentiment', 0.20):.0%}): {_fmt(pkg['component_scores']['sentiment'], '.4f')}",
        f"- Macro          (weight {weights.get('macro', 0.20):.0%}): {_fmt(pkg['component_scores']['macro'], '.4f')}",
        f"- On-chain       (weight {weights.get('onchain', 0.15):.0%}): {_fmt(pkg['component_scores']['onchain'], '.4f')}",
        f"- Preliminary direction: {pkg.get('direction', 'neutral')} | Confidence: {pkg.get('confidence_pct', 0)}%",
        "",
        "### MARKET REGIME",
        f"- Regime: {regime.get('regime', 'unknown')} (confidence: {_fmt(regime.get('confidence', 0), '.0%')})",
        f"- Trend direction: {regime.get('trend_direction', 'neutral')}",
        f"- ADX: {_fmt(regime.get('adx', 0), '.1f')} | BB Width: {_fmt(regime.get('bb_width_pct', 0), '.2f')}% | ATR%: {_fmt(regime.get('atr_pct', 0), '.2f')}%",
        f"- Weights dynamically adjusted for {regime.get('regime', 'ranging')} regime",
        "",
        "### MULTI-TIMEFRAME ALIGNMENT",
        f"- Status: {'✓ ALIGNED' if alignment.get('aligned') else '✗ DIVERGENT'} — {alignment.get('direction', 'mixed')}",
        f"- Breakdown: {alignment.get('bullish_tfs', 0)} bullish / {alignment.get('bearish_tfs', 0)} bearish out of {alignment.get('total_tfs', 0)} timeframes",
        f"- Multiplier applied to composite: {_fmt(alignment.get('multiplier', 1.0), '.2f')}×",
        "",
        "### TECHNICAL ANALYSIS (Per Timeframe)",
    ]

    for tf, d in timeframes.items():
        patterns = d.get("candlestick_patterns", [])
        pattern_str = ", ".join(patterns) if patterns else "none"
        fib_sup = d.get("fib_nearest_support")
        fib_res = d.get("fib_nearest_resistance")
        sections += [
            "",
            f"#### [{tf}] Score: {_fmt(d.get('score', 0), '.3f')} → {d.get('signal', 'N/A')}",
            f"  RSI: {_fmt(d.get('rsi'), '.1f')} ({d.get('rsi_signal', 'N/A')}) | Divergence: {d.get('rsi_divergence', 'none')}",
            f"  MACD: histogram={_fmt(d.get('macd_histogram'), '.4f')}, cross={d.get('macd_cross', 'none')}",
            f"  Bollinger: {d.get('bb_position', 'N/A')} | Squeeze: {d.get('bb_squeeze', False)}",
            f"  EMA Alignment: {d.get('ema_alignment', 'N/A')} | ADX: {_fmt(d.get('adx'), '.1f')} ({d.get('adx_strength', 'N/A')})",
            f"  Stoch K: {_fmt(d.get('stoch_k'), '.1f')} | Volume: {d.get('volume_signal', 'N/A')}",
            f"  Ichimoku: signal={d.get('ichimoku_signal', 'N/A')}, TK_cross={d.get('ichimoku_tk_cross', 'none')}, above_cloud={d.get('ichimoku_above_cloud', 'N/A')}, cloud_color={d.get('ichimoku_cloud_color', 'N/A')}",
            f"  Candlestick: {pattern_str} → {d.get('pattern_signal', 'neutral')}",
            f"  Fibonacci: position={_fmt(d.get('fib_position', 0.5), '.2f')}, support=${_fmt(fib_sup, '.4f')}, resistance=${_fmt(fib_res, '.4f')}, near_support={d.get('fib_near_support', False)}, near_resistance={d.get('fib_near_resistance', False)}",
            f"  Regime: {d.get('market_regime', 'N/A')}",
        ]

    sections += [
        "",
        f"### PRELIMINARY RISK LEVELS (ATR-based, best TF: {pkg.get('best_timeframe', 'N/A')})",
        f"- Stop loss: ${_fmt(pkg.get('stop_loss', 0), ',.4f')} ({_fmt(pkg.get('stop_loss_pct', 0), '.2f')}% from price)",
        f"- Take profit 1: ${_fmt(pkg.get('take_profit_1', 0), ',.4f')}",
        f"- Take profit 2: ${_fmt(pkg.get('take_profit_2', 0), ',.4f')}",
        f"- Suggested hold: {pkg.get('hold_duration', 'N/A')}",
        "",
        "### NEWS & SENTIMENT",
        f"- Overall news sentiment: {pkg['news_summary'].get('aggregate_sentiment', 'N/A')}",
        f"- Articles analyzed: {pkg['news_summary'].get('total_articles', 0)}",
        "- Top headlines:",
    ]

    for headline in pkg["news_summary"].get("top_headlines", []):
        if headline:
            sections.append(f"  • {headline}")

    sections += [
        "",
        "### SOCIAL SENTIMENT (Twitter/X)",
        f"- Tweets analyzed: {pkg['social'].get('tweets_analyzed', 0)}",
        f"- Sentiment: {pkg['social'].get('sentiment_label', 'N/A')} | Bullish: {_fmt(pkg['social'].get('bullish_pct', 0), '.1f')}%",
        "",
        "### MACRO ENVIRONMENT",
        f"- Fear & Greed: {pkg['fear_greed'].get('value', 50)} ({pkg['fear_greed'].get('classification', 'Neutral')})",
        f"- Yield curve inverted: {pkg['macro_summary'].get('yield_curve_inverted', 'N/A')}",
        f"- Fed funds rate: {_fmt(pkg['macro_summary'].get('fed_funds_rate'), '.2f')}%",
        f"- VIX: {_fmt(pkg['macro_summary'].get('vix'), '.1f')}",
        "",
        "### ON-CHAIN / MARKET DATA",
        f"- Market cap rank: #{pkg['coingecko'].get('market_cap_rank', 'N/A')}",
        f"- 7-day price change: {_fmt(pkg['coingecko'].get('price_change_7d_pct'), '.2f')}%",
        f"- Community sentiment votes up: {_fmt(pkg['coingecko'].get('sentiment_votes_up_pct'), '.1f')}%",
    ]

    if global_market:
        sections += [
            "",
            "### GLOBAL CRYPTO MARKET",
            f"- Total market cap: ${global_market.get('total_market_cap_usd', 0) / 1e9:.1f}B",
            f"- BTC dominance: {_fmt(global_market.get('btc_dominance_pct', 0), '.1f')}%",
            f"- ETH dominance: {_fmt(global_market.get('eth_dominance_pct', 0), '.1f')}%",
            f"- 24h market cap change: {_fmt(global_market.get('market_cap_change_24h_pct', 0), '.2f')}%",
        ]

    sections.append("\nApply your analytical framework step by step, then output your complete trade recommendation JSON.")
    return "\n".join(sections)
