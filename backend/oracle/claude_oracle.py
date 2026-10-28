"""Claude oracle — synthesizes all signals into a structured trade recommendation."""

import json
import os
import anthropic

_client: anthropic.AsyncAnthropic | None = None

# One model for every Claude call in the app (scanner signals, autotrader confirmations,
# Claude swing unless it sets its own). Override with CLAUDE_MODEL in .env.
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")


# Output budgets. Opus 5.5 thinks before answering and the thinking counts against max_tokens, so
# the old 2,500 cut replies off mid-JSON. Only tokens actually produced are billed.
MAX_TOKENS_ORACLE = 8000
MAX_TOKENS_REVIEW = 8000


def response_text(response) -> str:
    """The reply's text. Newer models (Opus 5.5) put a thinking block before the answer, so
    `content[0].text` is not the answer — join the text blocks and skip everything else.
    Raises if the reply was cut off (stop_reason max_tokens), rather than returning broken JSON."""
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise ValueError("Claude's reply was cut off at max_tokens")
    return "".join(getattr(b, "text", None) or "" for b in response.content if getattr(b, "type", "") == "text").strip()


def parse_json(raw: str):
    """JSON from a reply that may wrap it in a ```json code fence."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.startswith("json") else raw
    return json.loads(raw.strip())


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

## Instrument: USDT-M perpetual futures, isolated margin, up to 20x leverage
- SELL (short) is exactly as valid as BUY. Judge direction on evidence only; a bearish structure deserves a SELL, not a HOLD.
- Positions are sized by risk (a fixed % of equity lost at the stop) and leverage is chosen per trade so liquidation sits at least 2× the stop distance away. So your stop distance directly sets the usable leverage: ≤2.3% allows ~20x, 4% ~11x. Never widen a stop to "give room" — a stop that is structurally correct but wide is fine; one that is arbitrary is not.
- Costs are real: ~0.05% taker fee each side plus slippage, and funding every 8h (positive rate = longs pay). Prefer setups whose TP1 is comfortably more than 0.3% away; against a strongly positive funding rate, a long needs extra conviction (and vice versa).
- Leverage amplifies wicks: in thin or news-driven conditions, prefer HOLD over a tight stop that noise will hit.

## Risk Management Rules
- Stop loss: Place BEYOND the nearest S/R or Fibonacci level in the loss direction. ATR×1.5 is the minimum distance — if the nearest support/resistance is farther, use that level with a 0.1–0.2% buffer.
- Risk:Reward: Minimum 2:1. Use Fibonacci extensions (1.618×, 2.618×) for TP targets.
- Position size: Inversely proportional to ATR%. High volatility → smaller size.
- HOLD when: timeframes conflict, signals are mixed, near major scheduled events, or confidence < 50.

## Internal reasoning steps (not in output — think through these then write JSON):
1. What is the dominant market regime (trending/ranging/volatile)?
2. Do the 4h and 1h agree on direction? What about 30m/15m?
3. List the 3 strongest confirming signals.
4. List the 2 biggest risk factors and the invalidation price.
5. Optimal entry — NEVER use raw current price unless it coincides with a key level:
   - BUY: target the nearest Fibonacci support, S1 pivot, or VWAP as entry. If current price is already at support (within 0.3%), entry ≈ current price. If price is >0.5% above VWAP, set entry at VWAP or the nearest Fibonacci support below current price.
   - SELL: target nearest Fibonacci resistance, R1 pivot, or VWAP. If price is >0.5% below VWAP, set entry at VWAP or nearest Fibonacci resistance above current price.
   - HOLD: entry_price = current price.
   - entry_zone: low = entry × 0.998, high = entry × 1.002 (tighten near a hard key level).
   - risk = abs(entry_price − stop_loss). TP1 = entry ± risk×2. TP2 = entry ± risk×3. TP3 = entry ± risk×4.618.

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
    "timeframe": "<best trading timeframe e.g. 4h, 1h>",
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

CRITICAL: entry_price must be the OPTIMAL entry (Fibonacci support/resistance, S/R pivot, or VWAP) — NOT the raw current price unless it coincides with one of those levels. Derive all levels from provided current price, ATR, Fibonacci levels, and S/R zones in the data."""


def _positioning_lines(p: dict) -> list[str]:
    """Futures positioning — who is holding the coin and how crowded that side is."""
    if not p.get("funding_rate") and p.get("funding_rate") != 0:
        return []
    lines = [
        "",
        "### DERIVATIVES POSITIONING (Binance perpetual)",
        f"- Funding per 8h: now {_fmt(p.get('funding_rate', 0) * 100, '+.4f')}%, 7d avg "
        f"{_fmt((p.get('funding_7d_avg') or 0) * 100, '+.4f')}% (neutral baseline +0.0100%); "
        f"7d vs 30d z-score {_fmt(p.get('funding_z'), '+.2f')}",
        f"- Open interest: ${_fmt((p.get('oi_usd') or 0) / 1e6, ',.0f')}M, {_fmt(p.get('oi_change_24h_pct'), '+.1f')}% 24h, "
        f"{_fmt(p.get('oi_change_7d_pct'), '+.1f')}% 7d",
        f"- Taker buy/sell volume 24h: {_fmt(p.get('taker_buy_sell_24h'), '.3f')} · accounts long "
        f"{_fmt(p.get('long_account_pct'), '.1f')}% · top traders long {_fmt(p.get('top_trader_long_pct'), '.1f')}% · "
        f"basis {_fmt(p.get('basis_pct'), '+.3f')}%",
        "- Read it as crowding: high funding with OI piling in while price stalls is a crowded long "
        "(squeeze risk down); negative funding into a rising price is shorts paying to be wrong.",
    ]
    return lines


def _usage_lines(u: dict) -> list[str]:
    """Protocol / chain usage from DefiLlama — is anyone using what the token represents?"""
    lines = []
    if u.get("protocol_tvl_usd"):
        lines.append(f"- Protocol {u.get('protocol')}: TVL ${u['protocol_tvl_usd'] / 1e6:,.0f}M "
                     f"({_fmt(u.get('protocol_tvl_change_7d_pct'), '+.1f')}% 7d), fees 7d "
                     f"${_fmt((u.get('protocol_fees_7d_usd') or 0) / 1e6, ',.2f')}M "
                     f"({_fmt(u.get('protocol_fees_change_7d_pct'), '+.1f')}% vs prior week)")
    if u.get("chain_tvl_usd"):
        lines.append(f"- Chain {u.get('chain')}: TVL ${u['chain_tvl_usd'] / 1e6:,.0f}M "
                     f"({_fmt(u.get('chain_tvl_change_7d_pct'), '+.1f')}% 7d, {_fmt(u.get('chain_tvl_change_30d_pct'), '+.1f')}% 30d), "
                     f"fees 7d ${_fmt((u.get('chain_fees_7d_usd') or 0) / 1e6, ',.2f')}M "
                     f"({_fmt(u.get('chain_fees_change_7d_pct'), '+.1f')}% vs prior week)")
    return ["", "### USAGE (DefiLlama)", *lines] if lines else []


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
            model=MODEL,
            max_tokens=MAX_TOKENS_ORACLE,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )

        raw = response_text(response)
        recommendation = parse_json(raw)
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
        *([f"Instrument: {pkg['asset_note']}"] if pkg.get("asset_note") else []),
        f"Current Price: ${price:,.4f}",
        "",
        "### COMPOSITE SIGNAL SCORES",
        f"- Overall composite: {pkg.get('composite_score', 0):.4f}  (−1=max bearish, +1=max bullish)",
        f"- Technical      (weight {weights.get('ta', 0.45):.0%}): {_fmt(pkg['component_scores']['technical'], '.4f')}",
        f"- Sentiment      (weight {weights.get('sentiment', 0.20):.0%}): {_fmt(pkg['component_scores']['sentiment'], '.4f')}",
        f"- Macro          (weight {weights.get('macro', 0.20):.0%}): {_fmt(pkg['component_scores']['macro'], '.4f')}",
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
        sup_touches = d.get("fib_support_touches", 0)
        res_touches = d.get("fib_resistance_touches", 0)
        hist_roc = d.get("macd_histogram_roc", 0)
        hist_expanding = d.get("macd_histogram_expanding", False)
        macd_momentum = "expanding" if hist_expanding else ("building" if hist_roc > 0 else "fading")
        sections += [
            "",
            f"#### [{tf}] Score: {_fmt(d.get('score', 0), '.3f')} → {d.get('signal', 'N/A')}",
            f"  Structure: {d.get('market_structure', 'unclear')} (score {_fmt(d.get('structure_score', 0), '+.3f')})",
            f"  RSI: {_fmt(d.get('rsi'), '.1f')} ({d.get('rsi_signal', 'N/A')}) | Divergence: {d.get('rsi_divergence', 'none')}",
            f"  MACD: histogram={_fmt(d.get('macd_histogram'), '.4f')}, cross={d.get('macd_cross', 'none')}, momentum={macd_momentum} (roc {_fmt(hist_roc, '+.5f')})",
            f"  Bollinger: {d.get('bb_position', 'N/A')} | Squeeze: {d.get('bb_squeeze', False)}",
            f"  EMA Alignment: {d.get('ema_alignment', 'N/A')} | ADX: {_fmt(d.get('adx'), '.1f')} ({d.get('adx_strength', 'N/A')}, {d.get('adx_momentum', 'flat')}, slope {_fmt(d.get('adx_slope'), '+.1f')})",
            f"  Stoch K: {_fmt(d.get('stoch_k'), '.1f')} | Volume: {d.get('volume_signal', 'N/A')} | OBV: {d.get('obv_trend', 'N/A')}",
            f"  VWAP: ${_fmt(d.get('vwap'), ',.4f')} | {'Above' if d.get('vwap_above') else 'Below'} ({_fmt(d.get('vwap_deviation_pct'), '+.2f')}%) | CMF: {_fmt(d.get('cmf'), '+.3f')}",
            f"  Ichimoku: signal={d.get('ichimoku_signal', 'N/A')}, TK_cross={d.get('ichimoku_tk_cross', 'none')}, above_cloud={d.get('ichimoku_above_cloud', 'N/A')}, cloud_color={d.get('ichimoku_cloud_color', 'N/A')}",
            f"  Candlestick: {pattern_str} → {d.get('pattern_signal', 'neutral')}",
            f"  Fibonacci: position={_fmt(d.get('fib_position', 0.5), '.2f')}, support=${_fmt(fib_sup, '.4f')} ({sup_touches} tests), resistance=${_fmt(fib_res, '.4f')} ({res_touches} tests), near_support={d.get('fib_near_support', False)}, near_resistance={d.get('fib_near_resistance', False)}",
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
    events = pkg["news_summary"].get("key_events") or []
    if events:
        ns = pkg["news_summary"]
        sections.append(f"- News score {_fmt(ns.get('score'), '+.2f')} (importance- and recency-weighted); "
                        f"{ns.get('stories_6h') or 0} stories on this coin in 6 h, {_fmt(ns.get('burst'), '.1f')}× its usual rate")
        sections.append("- Key classified events (type · direction · importance 1–5 · age):")
        for e in events:
            sections.append(f"  • [{e.get('event_type')} · {_fmt(e.get('direction'), '+.1f')} · {e.get('importance')} · "
                            f"{_fmt(e.get('age_h'), '.1f')}h] {e.get('title')}" + (f" — {e['note']}" if e.get("note") else ""))

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

    fa = pkg.get("fundamentals")
    if fa and fa.get("components"):
        sections += [
            "",
            "### FUNDAMENTALS (scanner FA layer, −1 bearish … +1 bullish)",
            f"- Overall: {_fmt(fa.get('score'), '+.2f')} ({fa.get('label', 'n/a')}); "
            f"relative strength vs BTC over 24h: {_fmt(fa.get('rs_vs_btc_24h'), '+.2f')} pp",
        ]
        for name, comp in fa["components"].items():
            sections.append(f"- {name.replace('_', ' ')}: {_fmt(comp.get('score'), '+.2f')} — {comp.get('note', '')}")
        sections.append("- Treat this score as context, not a verdict: its inputs (relative strength, momentum, "
                        "market regime) showed no predictive power over the next 24–72h in a one-year backtest "
                        "across 40 coins, and they largely repeat the chart. Positioning and usage below are "
                        "still being evaluated.")
        sections += _positioning_lines(fa.get("positioning") or {}) + _usage_lines(fa.get("usage") or {})

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
