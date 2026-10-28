"""Fundamental analysis layer — scores a coin from CMC market data and global regime.

Complements the TA composite in analysis/scoring.py. Where TA answers "is this chart
set up to move", FA answers "is this the coin worth trading right now" — which is the
question that actually matters when ranking ten candidates against each other.
"""


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def score_relative_strength(coin: dict, btc: dict) -> tuple[float, str]:
    """Coin's 24h move minus BTC's — the core alt-selection signal.

    An alt rising while BTC is flat is showing genuine independent bid; an alt rising
    only as much as BTC is just beta and carries no edge.
    """
    c24 = coin.get("percent_change_24h")
    b24 = btc.get("percent_change_24h")
    if c24 is None or b24 is None:
        return 0.0, "no relative-strength data"

    if coin.get("symbol") == "BTC":
        return 0.0, "is the benchmark"

    rs = c24 - b24
    score = _clamp(rs / 8.0)
    if rs > 3:
        note = f"outperforming BTC by {rs:.1f}% over 24h"
    elif rs < -3:
        note = f"underperforming BTC by {abs(rs):.1f}% over 24h"
    else:
        note = f"tracking BTC ({rs:+.1f}%)"
    return score, note


def score_momentum_shape(coin: dict) -> tuple[float, str]:
    """Blend 1h/24h/7d returns, weighted toward the medium horizon.

    The interesting case is disagreement: a coin down over 7d but up over 1h and 24h
    is a potential reversal, and shows up here as a score near zero with a divergent note.
    """
    h1 = coin.get("percent_change_1h") or 0.0
    h24 = coin.get("percent_change_24h") or 0.0
    d7 = coin.get("percent_change_7d") or 0.0

    score = (
        _clamp(h1 / 3.0) * 0.20
        + _clamp(h24 / 8.0) * 0.45
        + _clamp(d7 / 20.0) * 0.35
    )

    short_up = h1 > 0 and h24 > 0
    if d7 < -5 and short_up:
        note = f"reversal forming — 7d {d7:.1f}% but 24h {h24:+.1f}%"
    elif d7 > 5 and h1 < 0 and h24 < 0:
        note = f"pullback in uptrend — 7d {d7:+.1f}%, 24h {h24:.1f}%"
    elif d7 > 0 and short_up:
        note = f"aligned uptrend across 1h/24h/7d ({d7:+.1f}% 7d)"
    elif d7 < 0 and h1 < 0 and h24 < 0:
        note = f"aligned downtrend across 1h/24h/7d ({d7:.1f}% 7d)"
    else:
        note = f"mixed momentum (1h {h1:+.1f}%, 24h {h24:+.1f}%, 7d {d7:+.1f}%)"
    return _clamp(score), note


def score_liquidity(coin: dict) -> tuple[float, str]:
    """Turnover (24h volume / market cap) as a proxy for real participation.

    Very low turnover means a signal has no volume behind it; very high turnover is
    often capitulation or a blow-off rather than a clean trend, so it scores down again.
    """
    mcap = coin.get("market_cap") or 0
    vol = coin.get("volume_24h") or 0
    if not mcap or not vol:
        return 0.0, "no volume data"

    turnover = vol / mcap
    if turnover < 0.01:
        score, note = -0.40, f"thin turnover {turnover:.1%} — little participation"
    elif turnover < 0.03:
        score, note = 0.0, f"normal turnover {turnover:.1%}"
    elif turnover < 0.10:
        score, note = 0.35, f"active turnover {turnover:.1%}"
    elif turnover < 0.30:
        score, note = 0.60, f"high turnover {turnover:.1%} — strong interest"
    else:
        score, note = 0.15, f"extreme turnover {turnover:.1%} — possible blow-off"

    vol_change = coin.get("volume_change_24h")
    if vol_change is not None:
        if vol_change > 50:
            score = _clamp(score + 0.20)
            note += f", volume +{vol_change:.0f}%"
        elif vol_change < -40:
            score = _clamp(score - 0.15)
            note += f", volume {vol_change:.0f}%"
    return _clamp(score), note


def score_supply(coin: dict) -> tuple[float, str]:
    """Circulating vs max supply — how much future dilution is still to come."""
    circ = coin.get("circulating_supply")
    mx = coin.get("max_supply")

    if not circ:
        return 0.0, "no supply data"
    if not mx:
        return -0.15, "uncapped supply — ongoing dilution"

    ratio = circ / mx
    if ratio > 0.90:
        return 0.30, f"{ratio:.0%} of max supply circulating — minimal dilution ahead"
    if ratio > 0.70:
        return 0.10, f"{ratio:.0%} of max supply circulating"
    if ratio > 0.50:
        return 0.0, f"{ratio:.0%} of max supply circulating"
    return -0.20, f"only {ratio:.0%} circulating — significant unlocks ahead"


def score_market_regime(coin: dict, global_market: dict) -> tuple[float, str]:
    """Risk-on/risk-off from BTC dominance and total market cap direction.

    Rising dominance drains alts and favours BTC, so the same reading scores in
    opposite directions depending on which coin is being evaluated.
    """
    mcap_change = global_market.get("market_cap_change_24h_pct")
    dominance = global_market.get("btc_dominance_pct")
    is_btc = coin.get("symbol") == "BTC"

    score = 0.0
    parts = []

    if mcap_change is not None:
        score += _clamp(mcap_change / 5.0) * 0.6
        parts.append(f"total mcap {mcap_change:+.1f}% 24h")

    if dominance is not None:
        # Dominance itself is a level, not a direction; treat the extremes as the signal.
        if dominance > 60:
            score += 0.25 if is_btc else -0.25
            parts.append(f"BTC dominance high at {dominance:.1f}%")
        elif dominance < 45:
            score += -0.15 if is_btc else 0.25
            parts.append(f"BTC dominance low at {dominance:.1f}% — alt-friendly")
        else:
            parts.append(f"BTC dominance {dominance:.1f}%")

    return _clamp(score), "; ".join(parts) if parts else "no global market data"


# Directional components say which way the coin is leaning. Their weights sum to 0.75
# (the scale thresholds like the autotrader's FA veto were tuned on).
WEIGHTS = {
    "relative_strength": 0.30,
    "momentum_shape": 0.25,
    "market_regime": 0.20,
}
# Quality components say how far to trust that lean, not which way it points: heavy
# turnover is "strong interest" in a crash as much as in a rally. They used to be added
# into the score, which gave liquid, low-dilution coins a standing bullish tilt (and made
# shorts on exactly those coins harder to take). Now they scale the directional score.
QUALITY_WEIGHTS = {"liquidity": 0.6, "supply": 0.4}
QUALITY_SPAN = 0.25          # quality −1 … +1 → directional score × 0.75 … 1.25


def compute_fundamentals(coin: dict, btc: dict, global_market: dict) -> dict:
    """FA score in -1..+1 (direction × quality), with per-component breakdown for the UI."""
    rs, rs_note = score_relative_strength(coin, btc)
    mom, mom_note = score_momentum_shape(coin)
    regime, regime_note = score_market_regime(coin, global_market)
    liq, liq_note = score_liquidity(coin)
    sup, sup_note = score_supply(coin)

    components = {
        "relative_strength": {"score": round(rs, 4), "note": rs_note},
        "momentum_shape": {"score": round(mom, 4), "note": mom_note},
        "market_regime": {"score": round(regime, 4), "note": regime_note},
        "liquidity": {"score": round(liq, 4), "note": f"quality, not direction — {liq_note}"},
        "supply": {"score": round(sup, 4), "note": f"quality, not direction — {sup_note}"},
    }

    direction = sum(components[k]["score"] * w for k, w in WEIGHTS.items())
    quality = sum(components[k]["score"] * w for k, w in QUALITY_WEIGHTS.items())
    quality_mult = 1 + QUALITY_SPAN * _clamp(quality)
    score = _clamp(direction * quality_mult)

    return {
        "score": round(score, 4),
        "label": (
            "bullish" if score > 0.25 else
            "bearish" if score < -0.25 else
            "neutral"
        ),
        "direction": round(direction, 4),
        "quality": round(quality, 4),
        "quality_multiplier": round(quality_mult, 3),
        "components": components,
        "market_cap_rank": coin.get("cmc_rank"),
        "rs_vs_btc_24h": (
            round((coin.get("percent_change_24h") or 0) - (btc.get("percent_change_24h") or 0), 2)
            if coin.get("symbol") != "BTC" else 0.0
        ),
    }


# ── Research features ──────────────────────────────────────────────────────────
# Raw, unscored inputs from every fundamentals source, recorded on each full sweep so
# research/swing_lab.py can test which of them actually predict the next move. None of
# these feed the live score until the lab shows one holds up out of sample.

def _rank(value: float | None, pool: list[float]) -> float | None:
    """Where `value` sits in the pool, −1 (lowest) … +1 (highest)."""
    if value is None or len(pool) < 5:
        return None
    below = sum(v < value for v in pool)
    equal = sum(v == value for v in pool)
    return (below + (equal - 1) / 2) / (len(pool) - 1) * 2 - 1


def research_features(coin: dict, btc: dict, positioning: dict, usage: dict, shared: dict) -> dict:
    pool = shared.get("pool_changes") or {}
    c24, c7 = coin.get("percent_change_24h"), coin.get("percent_change_7d")
    b24, b7 = btc.get("percent_change_24h"), btc.get("percent_change_7d")
    gm = shared.get("global_market") or {}
    stable = shared.get("stablecoins") or {}
    f = {
        # price-derived (CMC)
        "ret_1h": coin.get("percent_change_1h"), "ret_24h": c24, "ret_7d": c7,
        "rs_24h": c24 - b24 if None not in (c24, b24) else None,
        "rs_7d": c7 - b7 if None not in (c7, b7) else None,
        "rank_24h": _rank(c24, pool.get("24h", [])), "rank_7d": _rank(c7, pool.get("7d", [])),
        "turnover": (coin.get("volume_24h") or 0) / coin["market_cap"] if coin.get("market_cap") else None,
        "volume_change_24h": coin.get("volume_change_24h"),
        # derivatives positioning (Binance)
        **{k: positioning.get(k) for k in (
            "funding_rate", "funding_7d_avg", "funding_z", "basis_pct", "oi_change_24h_pct",
            "oi_change_7d_pct", "taker_buy_sell_24h", "long_account_pct", "top_trader_long_pct")},
        # usage (DefiLlama)
        **{k: usage.get(k) for k in (
            "protocol_tvl_change_7d_pct", "protocol_fees_change_7d_pct", "chain_tvl_change_7d_pct",
            "chain_tvl_change_30d_pct", "chain_fees_change_7d_pct")},
        # market-wide
        "btc_dominance_pct": gm.get("btc_dominance_pct"),
        "market_cap_change_24h_pct": gm.get("market_cap_change_24h_pct"),
        "stablecoin_growth_7d_pct": stable.get("stablecoin_growth_7d_pct"),
        "stablecoin_growth_30d_pct": stable.get("stablecoin_growth_30d_pct"),
    }
    oi24 = positioning.get("oi_change_24h_pct")
    if oi24 is not None and c24 is not None:
        # OI rising into a falling price (or falling into a rising one) = one side piling in
        # against the move; the product is positive when OI confirms the price direction.
        f["oi_price_confirm"] = oi24 * (1 if c24 > 0 else -1 if c24 < 0 else 0)
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in f.items() if v is not None}
