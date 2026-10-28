"""Named trade setups layered on top of the existing regime-weighted composite.

Every input here is already computed by analysis/technical.py — this module adds no
indicator math. Its job is to turn the indicator soup into a handful of recognisable,
nameable setups so a signal can be explained ("squeeze breakout on 1h") rather than
shown as an opaque number, and so conviction can be boosted only when a real pattern
is present rather than whenever the weighted average drifts high.
"""

# Timeframes worth evaluating setups on, ordered by how much weight the existing
# consensus already gives them.
SETUP_TIMEFRAMES = ("4h", "1h", "30m")


def _num(s: dict, key: str, default: float = 0.0) -> float:
    v = s.get(key)
    return float(v) if isinstance(v, (int, float)) else default


def trend_continuation(s: dict) -> dict | None:
    """Established trend with momentum still expanding — the highest-quality continuation."""
    align = s.get("ema_alignment")
    adx = _num(s, "adx")
    adx_slope = _num(s, "adx_slope")
    if align not in ("bullish_aligned", "bearish_aligned") or adx < 22:
        return None

    bullish = align == "bullish_aligned"
    reasons = [f"EMA 9/21/50 {'bullish' if bullish else 'bearish'} aligned", f"ADX {adx:.0f}"]
    strength = 0.45

    if adx_slope > 0:
        strength += 0.15
        reasons.append("ADX rising")
    if s.get("vwap_above") is bullish:
        strength += 0.10
        reasons.append(f"price {'above' if bullish else 'below'} VWAP")
    if s.get("macd_histogram_expanding") and (_num(s, "macd_histogram") > 0) == bullish:
        strength += 0.15
        reasons.append("MACD histogram expanding")
    if s.get("market_structure") == ("bullish" if bullish else "bearish"):
        strength += 0.15
        reasons.append(f"{'HH/HL' if bullish else 'LH/LL'} structure")

    if strength < 0.60:
        return None
    return {
        "name": "trend_continuation",
        "label": "Trend continuation",
        "direction": "long" if bullish else "short",
        "strength": round(min(strength, 1.0), 2),
        "reasons": reasons,
    }


def squeeze_breakout(s: dict) -> dict | None:
    """Volatility compression resolving into a volume-backed expansion."""
    vol_ratio = _num(s, "volume_ratio", 1.0)
    position = s.get("bb_position")

    if s.get("bb_squeeze"):
        # Still compressed — armed but not yet directional. Worth watching, not trading.
        return None
    if position not in ("overbought", "oversold") or vol_ratio < 1.5:
        return None

    bullish = position == "overbought"
    structure = s.get("market_structure")
    if structure == ("bearish" if bullish else "bullish"):
        return None  # breaking out against structure is a trap more often than not

    strength = 0.50
    reasons = [
        f"price outside {'upper' if bullish else 'lower'} Bollinger band",
        f"volume {vol_ratio:.1f}x average",
    ]
    if vol_ratio > 2.5:
        strength += 0.20
        reasons.append("volume surge")
    if s.get("obv_trend") == ("rising" if bullish else "falling"):
        strength += 0.15
        reasons.append(f"OBV {'rising' if bullish else 'falling'}")
    if structure == ("bullish" if bullish else "bearish"):
        strength += 0.15
        reasons.append("structure confirms")

    return {
        "name": "squeeze_breakout",
        "label": "Squeeze breakout",
        "direction": "long" if bullish else "short",
        "strength": round(min(strength, 1.0), 2),
        "reasons": reasons,
    }


def divergence_reversal(s: dict) -> dict | None:
    """RSI divergence at a tested level with candlestick confirmation."""
    div = s.get("rsi_divergence")
    if div not in ("bullish", "bearish"):
        return None

    bullish = div == "bullish"
    at_level = (
        s.get("fib_near_support") if bullish else s.get("fib_near_resistance")
    ) or abs(_num(s, "distance_to_s1_pct" if bullish else "distance_to_r1_pct", 99)) < 1.0
    if not at_level:
        return None

    strength = 0.50
    reasons = [f"{div} RSI divergence", "at tested support" if bullish else "at tested resistance"]

    if s.get("pattern_signal") == ("bullish" if bullish else "bearish"):
        strength += 0.20
        patterns = s.get("candlestick_patterns") or []
        reasons.append(f"{', '.join(patterns)} candle" if patterns else "candle confirms")
    touches = _num(s, "fib_support_touches" if bullish else "fib_resistance_touches")
    if touches >= 2:
        strength += 0.15
        reasons.append(f"level tested {touches:.0f}x")
    rsi = _num(s, "rsi", 50)
    if (bullish and rsi < 35) or (not bullish and rsi > 65):
        strength += 0.15
        reasons.append(f"RSI {rsi:.0f}")

    if strength < 0.65:
        return None
    return {
        "name": "divergence_reversal",
        "label": "Divergence reversal",
        "direction": "long" if bullish else "short",
        "strength": round(min(strength, 1.0), 2),
        "reasons": reasons,
    }


def mean_reversion(s: dict) -> dict | None:
    """Range-bound fade from a band extreme — only valid while the regime stays ranging."""
    if s.get("market_regime") != "ranging":
        return None
    position = s.get("bb_position")
    if position not in ("overbought", "oversold"):
        return None

    bullish = position == "oversold"
    stoch = _num(s, "stoch_k", 50)
    if not ((bullish and stoch < 25) or (not bullish and stoch > 75)):
        return None

    strength = 0.50
    reasons = [f"ranging regime, price {position}", f"Stochastic {stoch:.0f}"]

    cmf = _num(s, "cmf")
    if (bullish and cmf > 0) or (not bullish and cmf < 0):
        strength += 0.20
        reasons.append(f"CMF {cmf:+.2f} turning")
    rsi = _num(s, "rsi", 50)
    if (bullish and rsi < 35) or (not bullish and rsi > 65):
        strength += 0.15
        reasons.append(f"RSI {rsi:.0f}")

    return {
        "name": "mean_reversion",
        "label": "Mean reversion",
        "direction": "long" if bullish else "short",
        "strength": round(min(strength, 1.0), 2),
        "reasons": reasons,
    }


def ichimoku_breakout(s: dict) -> dict | None:
    """Cloud breakout with a fresh TK cross — a clean trend-initiation trigger."""
    signal = s.get("ichimoku_signal")
    if signal not in ("bullish", "bearish"):
        return None
    bullish = signal == "bullish"

    tk = s.get("ichimoku_tk_cross")
    above = s.get("ichimoku_above_cloud")
    if above is not bullish:
        return None

    strength = 0.45
    reasons = [f"price {'above' if bullish else 'below'} Ichimoku cloud"]

    if tk == ("bullish" if bullish else "bearish"):
        strength += 0.25
        reasons.append(f"{tk} TK cross")
    if s.get("ichimoku_cloud_color") == ("green" if bullish else "red"):
        strength += 0.15
        reasons.append("forward cloud confirms")
    if _num(s, "volume_ratio", 1.0) > 1.3:
        strength += 0.15
        reasons.append("above-average volume")

    if strength < 0.60:
        return None
    return {
        "name": "ichimoku_breakout",
        "label": "Ichimoku breakout",
        "direction": "long" if bullish else "short",
        "strength": round(min(strength, 1.0), 2),
        "reasons": reasons,
    }


_TF_SETUPS = (
    trend_continuation,
    squeeze_breakout,
    divergence_reversal,
    mean_reversion,
    ichimoku_breakout,
)


def mtf_confluence(alignment: dict) -> dict | None:
    """Independent of any single timeframe — do the timeframes agree with each other?"""
    total = alignment.get("total_tfs") or 0
    bull = alignment.get("bullish_tfs") or 0
    bear = alignment.get("bearish_tfs") or 0
    if total < 3:
        return None

    dominant, direction = (bull, "long") if bull > bear else (bear, "short")
    if dominant < 3:
        return None

    return {
        "name": "mtf_confluence",
        "label": "MTF confluence",
        "direction": direction,
        "strength": round(min(0.50 + 0.20 * (dominant - 2), 1.0), 2),
        "reasons": [f"{dominant} of {total} timeframes agree"],
        "timeframe": "multi",
    }


def detect_setups(tf_signals: dict, alignment: dict) -> list[dict]:
    """All setups firing across the evaluated timeframes, strongest first.

    A setup that fires on more than one timeframe is kept once, at its strongest
    reading, so multi-timeframe repetition doesn't inflate the conviction multiplier.
    """
    best: dict[str, dict] = {}

    for tf in SETUP_TIMEFRAMES:
        s = tf_signals.get(tf)
        if not isinstance(s, dict):
            continue
        for detector in _TF_SETUPS:
            try:
                fired = detector(s)
            except Exception:
                continue
            if not fired:
                continue
            fired["timeframe"] = tf
            prior = best.get(fired["name"])
            if prior is None or fired["strength"] > prior["strength"]:
                best[fired["name"]] = fired

    confluence = mtf_confluence(alignment or {})
    if confluence:
        best[confluence["name"]] = confluence

    return sorted(best.values(), key=lambda x: x["strength"], reverse=True)
