"""Entry timing and trade construction for the autonomous experiment — pure functions.

The scanner answers *which way* a coin is leaning; this module answers *when* to act on
it. A trade is taken in three stages:

  1. Bias      — the scan's conviction must lean clearly one way and the timeframes must
                 not disagree. Without a bias the symbol is simply "watching".
  2. Arm       — with a bias, the symbol is armed and waits. Nothing is bought yet.
  3. Trigger   — entry happens only when the fast (15m) timeframe offers a good moment:
                 a pullback that resumes, a squeeze breaking out on volume, or a bounce
                 off a level. An anti-chase filter vetoes entries into stretched price.

No LLM is involved; every decision is deterministic and explained in `reasons`.
"""

import os

MIN_CONVICTION = float(os.getenv("AUTOTRADER_MIN_CONVICTION", "0.20"))
# Entries this many 15m ATRs beyond the 15m EMA21 (in the trade's direction) are late:
# on the replay, signals ≥2 ATR stretched averaged −0.16R vs +1.84R within 1 ATR.
LATE_ATR = float(os.getenv("LATE_ENTRY_ATR", "2.0"))
# (The FA veto was retired on 2026-09-29 — FA's inputs had no predictive power over a year of
# 40 coins; see the Retired tab. FA is still recorded at entry and shown as context.)
ENTRY_SCORE = int(os.getenv("AUTOTRADER_ENTRY_SCORE", "60"))
# Market regime gate, from research/setup_lab.py (87.5k replayed scans, 1 year × 40 perpetuals,
# every quarter checked separately, R averaged per day so correlated trades count once):
#   * shorts lost in every volatility regime: −0.22R/day calm (t −2.7), −0.20R/day otherwise (t −4.2)
#   * longs lost when BTC's 30-day realized vol was above ~37%/yr: −0.22R/day, negative in all
#     four quarters (t −4.5); at or below it they were flat (≈0R/day)
# The volatility gate stays on by default. Shorts were switched back ON at the user's request
# (2026-09-28) despite that replay result — set AUTOTRADER_ALLOW_SHORTS=false to go long-only.
ALLOW_SHORTS = os.getenv("AUTOTRADER_ALLOW_SHORTS", "true").lower() == "true"
MAX_BTC_VOL = float(os.getenv("AUTOTRADER_MAX_BTC_VOL", "37"))      # % annualized; 0 = no gate
RISK_PCT = float(os.getenv("AUTOTRADER_RISK_PCT", "1.0"))        # % of equity lost at the stop
# Futures: notional can exceed equity; these cap how far. Margin (notional / leverage) is
# checked by the wallet, and total open risk by MAX_OPEN_RISK_PCT.
MAX_POSITION_PCT = float(os.getenv("AUTOTRADER_MAX_POSITION_PCT", "300"))   # notional per position
MAX_GROSS_PCT = float(os.getenv("AUTOTRADER_MAX_GROSS_PCT", "600"))         # notional across the wallet
# Portfolio heat: the sum of every open position's loss-at-stop. Six correlated alt longs
# are one bet on BTC; this caps what that one bet can cost.
MAX_OPEN_RISK_PCT = float(os.getenv("AUTOTRADER_MAX_OPEN_RISK_PCT", "4.0"))

TP1_R = 1.5          # stop moves to breakeven here
TP2_R = 2.5          # the trade closes as a win here
FLIP_EXIT_CONVICTION = 0.30

_BULL_LABELS = {"buy", "strong_buy"}
_BEAR_LABELS = {"sell", "strong_sell"}


def fast_tf(scan: dict) -> tuple[str, dict]:
    tf_signals = (scan.get("signal_package") or {}).get("ta_summary", {}).get("timeframe_signals", {})
    for tf in ("15m", "30m", scan.get("best_timeframe") or "1h", "1h"):
        if tf_signals.get(tf):
            return tf, tf_signals[tf]
    return "", {}


def extension_atr(fast: dict, direction: str) -> float | None:
    """How far price is beyond the 15m EMA21, in 15m ATRs, measured in the trade's direction."""
    price, ema, atr = fast.get("price"), fast.get("ema_21"), fast.get("atr")
    if not (price and ema and atr):
        return None
    return (price - ema) / atr * (1 if direction == "long" else -1)


def compact_fast(fast: dict) -> dict:
    """The few fast-TF readings remembered between ticks, to detect turns and breakouts."""
    return {
        "score": fast.get("score"),
        "stoch_k": fast.get("stoch_k"),
        "macd_histogram": fast.get("macd_histogram"),
        "bb_squeeze": bool(fast.get("bb_squeeze")),
        "rsi": fast.get("rsi"),
    }


def regime_block(direction: str, btc_vol_pct: float | None) -> str | None:
    """Why the market regime rules this entry out, or None if it's allowed."""
    if direction == "short" and not ALLOW_SHORTS:
        return "shorts off — they lost in every volatility regime over the last year (setup lab)"
    if MAX_BTC_VOL and btc_vol_pct is not None and btc_vol_pct > MAX_BTC_VOL:
        return (f"standing aside — BTC 30-day volatility {btc_vol_pct:.0f}% > {MAX_BTC_VOL:.0f}% "
                "(longs lost in every quarter in this regime)")
    return None


def bias(scan: dict) -> tuple[str | None, list[str]]:
    """Direction worth trading, or None with the reason there isn't one."""
    conv = scan.get("conviction", 0.0)
    if abs(conv) < MIN_CONVICTION:
        return None, [f"conviction {conv:+.2f} too weak (need ±{MIN_CONVICTION:.2f})"]

    mult = (scan.get("tf_alignment") or {}).get("multiplier", 1.0)
    if mult < 1.0:
        return None, [f"timeframes disagree (alignment {mult:.2f})"]

    detail = scan.get("conviction_detail") or {}
    aligned, opposing = detail.get("aligned_setups", []), detail.get("opposing_setups", [])
    if len(opposing) > len(aligned):
        return None, [f"opposing setups outweigh aligned ({', '.join(opposing)})"]

    direction = "long" if conv > 0 else "short"
    fa = scan.get("fa") or {}
    fa_score = fa.get("score")
    reasons = [f"bias {direction} — conviction {conv:+.2f}"]
    if fa_score is not None:
        reasons.append(f"FA {fa_score:+.2f} ({fa.get('label', '')})")
    if aligned:
        reasons.append(f"setups: {', '.join(aligned)}")
    return direction, reasons


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def timing(direction: str, fast: dict, prev: dict | None, scan: dict) -> dict:
    """Score the current moment for an entry in `direction`. 0–100 plus reasons.

    Returns {"score", "enter", "reasons", "blocked"}; `blocked` is set when the anti-chase
    filter vetoes the entry regardless of score.
    """
    s = 1 if direction == "long" else -1
    prev = prev or {}
    atr_pct = max(_num((scan.get("triggers") or {}).get("atr_pct"), 1.0), 0.1)

    rsi = _num(fast.get("rsi"), 50)
    stoch = _num(fast.get("stoch_k"), 50)
    hist = _num(fast.get("macd_histogram"))
    label = fast.get("signal")
    dist_r1 = _num(fast.get("distance_to_r1_pct"), 99)
    dist_s1 = _num(fast.get("distance_to_s1_pct"), 99)
    vwap_dev = _num(fast.get("vwap_deviation_pct"))
    volume = _num(fast.get("volume_ratio"), 1.0)

    # ── Anti-chase: never buy the top of a spike or sell the bottom of a flush ──
    stretched_rsi = rsi > 72 if s > 0 else rsi < 28
    stretched_band = fast.get("bb_position") == ("overbought" if s > 0 else "oversold")
    stretched_vwap = vwap_dev * s > 2.5 * atr_pct
    # Room to the opposing level: entering a long 0.3 ATR below resistance is asking to be rejected.
    room = dist_r1 if s > 0 else dist_s1
    cramped = 0 < room < 0.5 * atr_pct

    ext = extension_atr(fast, direction)
    if ext is not None and ext >= LATE_ATR:
        return {"score": 0, "enter": False, "blocked": True,
                "reasons": [f"{ext:.1f} ATR beyond the 15m EMA — late; waiting for a pullback"]}
    if stretched_rsi or stretched_band:
        return {"score": 0, "enter": False, "blocked": True,
                "reasons": [f"overextended (15m RSI {rsi:.0f}) — waiting for a pullback"]}
    if stretched_vwap:
        return {"score": 0, "enter": False, "blocked": True,
                "reasons": [f"{vwap_dev:+.1f}% from VWAP — too stretched to chase"]}
    if cramped:
        level = "resistance" if s > 0 else "support"
        return {"score": 0, "enter": False, "blocked": True,
                "reasons": [f"only {room:.2f}% to {level} — no room to run"]}

    score = 0
    primary = False
    reasons: list[str] = []

    # A. Pullback that resumes — the trend-follower's entry.
    fresh_label = label in (_BULL_LABELS if s > 0 else _BEAR_LABELS)
    pulled_back = 38 <= rsi <= 58 if s > 0 else 42 <= rsi <= 62
    turned = prev.get("score") is not None and (_num(fast.get("score")) - _num(prev["score"])) * s > 0.05
    hist_turn = prev.get("macd_histogram") is not None and (hist - _num(prev["macd_histogram"])) * s > 0
    if pulled_back and (turned or hist_turn) and (fresh_label or hist * s > 0):
        score += 40
        primary = True
        reasons.append("pullback resuming — 15m momentum turned back in trend direction")

    # B. Squeeze breakout on volume.
    was_squeezed = prev.get("bb_squeeze") or fast.get("bb_squeeze")
    broke = (dist_r1 <= 0) if s > 0 else (dist_s1 <= 0)
    if was_squeezed and volume >= 1.5 and broke:
        score += 45
        primary = True
        reasons.append(f"squeeze breakout through {'R1' if s > 0 else 'S1'} on {volume:.1f}× volume")

    # C. Bounce off a level with confirmation.
    at_level = (fast.get("fib_near_support") or 0 <= dist_s1 < 0.5) if s > 0 else \
               (fast.get("fib_near_resistance") or 0 <= dist_r1 < 0.5)
    want = "bullish" if s > 0 else "bearish"
    stoch_turn = prev.get("stoch_k") is not None and (
        (s > 0 and _num(prev["stoch_k"]) < 25 and stoch > _num(prev["stoch_k"]))
        or (s < 0 and _num(prev["stoch_k"]) > 75 and stoch < _num(prev["stoch_k"]))
    )
    if at_level and (fast.get("pattern_signal") == want or fast.get("rsi_divergence") == want or stoch_turn):
        score += 40
        primary = True
        reasons.append(f"bounce off {'support' if s > 0 else 'resistance'} confirmed")

    # Supporting evidence.
    if fast.get("macd_cross") == f"{want}_cross":
        score += 20
        reasons.append(f"15m MACD {want} cross")
    if fast.get("vwap_above") is (s > 0):
        score += 10
        reasons.append(f"price {'above' if s > 0 else 'below'} VWAP")
    if fast.get("ema_alignment") == f"{want}_aligned":
        score += 10
        reasons.append("EMAs stacked")
    if fast.get("obv_trend") == ("rising" if s > 0 else "falling"):
        score += 5
    if volume >= 1.2:
        score += 5
    if (scan.get("conviction_detail") or {}).get("aligned_setups"):
        score += 10
    score += int(min(20, max(0, (abs(scan.get("conviction", 0)) - MIN_CONVICTION) * 50)))

    score = min(100, score)
    enter = score >= ENTRY_SCORE and (primary or score >= 75)
    if not reasons:
        reasons.append("no entry trigger on 15m yet")
    return {"score": score, "enter": enter, "blocked": False, "reasons": reasons}


def build_levels(direction: str, price: float, scan: dict, fast: dict) -> dict:
    """ATR stop, tucked behind the nearest structure level when one is close by."""
    s = 1 if direction == "long" else -1
    atr_pct = _num((scan.get("triggers") or {}).get("atr_pct"), 1.0)
    sl_pct = min(max(1.5 * atr_pct, 0.4), 6.0)

    level = _num(fast.get("support_1" if s > 0 else "resistance_1"))
    if level > 0:
        level_dist = (price - level) / price * 100 * s
        # Only widen to the level if it's between 1× and 1.6× the ATR stop — beyond that
        # the stop is too wide for the risk budget and the ATR stop stands.
        if sl_pct <= level_dist + 0.1 <= 1.6 * sl_pct:
            sl_pct = level_dist + 0.1

    stop = price * (1 - s * sl_pct / 100)
    risk = abs(price - stop)
    return {
        "stop_loss": round(stop, 6),
        "take_profit_1": round(price + s * TP1_R * risk, 6),
        "take_profit_2": round(price + s * TP2_R * risk, 6),
        "sl_pct": round(sl_pct, 3),
    }


def position_size_pct(sl_pct: float, gross_used_pct: float) -> float:
    """Risk RISK_PCT of balance at the stop, capped per-position and wallet-wide."""
    size = RISK_PCT / max(sl_pct, 0.1) * 100
    size = min(size, MAX_POSITION_PCT, MAX_GROSS_PCT - gross_used_pct)
    return round(max(size, 0.0), 2)


def should_flip_exit(trade: dict, scan: dict, fast: dict) -> str | None:
    """Close early when the thesis is clearly broken — the scan now leans hard the other way."""
    s = 1 if trade["direction"] == "long" else -1
    conv = scan.get("conviction", 0.0)
    against = fast.get("signal") in (_BEAR_LABELS if s > 0 else _BULL_LABELS)
    if conv * s <= -FLIP_EXIT_CONVICTION and against:
        return f"signal flipped — conviction {conv:+.2f} against the position"
    return None


# ── Entry planning: market now, or a limit near Claude's level ─────────────────

# How long a resting limit waits for price, by the scan's best timeframe.
ORDER_TTL_SECONDS = {"15m": 3600, "30m": 2 * 3600, "1h": 4 * 3600, "4h": 8 * 3600}
# Maker entries (since 2026-10-07): an entry that would have been a market order is placed as a
# post-only limit a hair inside the market instead — 0.02% maker fee and no slippage instead of
# 0.05% + ~0.03%, about 6 bps saved per entry. The cost: it can miss (no fill in MAKER_TTL_S), and
# it tends to fill when price moves against it. Every maker order gets a market-entry twin in the
# shadow book, so the saving and the misses are measured, not assumed.
MAKER_ENTRIES = os.getenv("AUTOTRADER_MAKER_ENTRIES", "true").lower() != "false"
MAKER_OFFSET_BPS = float(os.getenv("AUTOTRADER_MAKER_OFFSET_BPS", "2"))     # rest this far inside the last price
MAKER_TTL_S = int(os.getenv("AUTOTRADER_MAKER_TTL", "900"))                 # cancel if unfilled after this
# Pullback entries (since 2026-10-19) replace the 2 bps maker entry: the post-only limit rests
# PULLBACK_R × the stop distance back from the signal price, for PULLBACK_TTL_S, and the stop stays
# where it was set from the signal price — a better price and a closer stop. On a year of replayed
# scanner candidates (research/pullback_year_lab.py, 1,666 signals, strict fills, maker fee in) it beat
# market entry by +0.075R a signal, train +0.075 / test +0.076, 95% range +0.02…+0.13, 79% filled;
# on our own live trades (research/confirm_entry_lab.py) +0.05R, within noise. Each order keeps its
# market twin in the shadow book, so the maker report measures it live.
PULLBACK_ENTRIES = os.getenv("AUTOTRADER_PULLBACK_ENTRIES", "true").lower() != "false"
PULLBACK_R = float(os.getenv("AUTOTRADER_PULLBACK_R", "0.25"))
PULLBACK_TTL_S = int(os.getenv("AUTOTRADER_PULLBACK_TTL", str(6 * 3600)))


def pullback_limit(direction: str, price: float, stop_loss: float) -> float:
    """Where a pullback entry rests: PULLBACK_R of the way from the signal price toward its stop."""
    return round(price - (price - stop_loss) * PULLBACK_R, 8)
MARKET_IF_GAP_UNDER = 0.10   # × stop distance — Claude's level is effectively "here"
MAX_PULLBACK = 1.0           # × stop distance — deeper than this is a different trade
FILL_MARGIN = 0.20           # rest this fraction of the gap short of Claude's exact level


def plan_entry(direction: str, price: float, claude_entry: float | None, sl_pct: float,
               best_tf: str | None) -> dict:
    """Compare our entry (the current price) with Claude's suggested entry.

    * Claude's level is at or worse than ours, or within 0.1 stop-widths → buy now.
    * Claude's level is better → rest a limit between the two, FILL_MARGIN of the gap
      short of Claude's price: exact levels are often front-run by a few ticks, and
      missing the fill by a hair is the most common way a good limit fails.
    * Claude wants a pullback deeper than one stop-width → cap it there; beyond that the
      setup we armed on is no longer the setup being traded.
    """
    s = 1 if direction == "long" else -1
    if not claude_entry or claude_entry <= 0 or not price:
        return {"type": "market", "price": price, "note": "no Claude entry level — entering at market"}

    # Positive gap = Claude's level is better than buying/selling here.
    gap_pct = (price - claude_entry) / price * 100 * s
    if gap_pct <= MARKET_IF_GAP_UNDER * sl_pct:
        where = "at or near" if gap_pct > 0 else ("above" if s > 0 else "below")
        return {"type": "market", "price": price, "claude_entry": claude_entry, "gap_pct": round(gap_pct, 3),
                "note": f"Claude's entry {claude_entry:g} is {where} ours — entering at market"}

    capped = min(gap_pct, MAX_PULLBACK * sl_pct)
    rest_gap = capped * (1 - FILL_MARGIN)
    limit = price * (1 - s * rest_gap / 100)
    note = (f"limit {limit:.6g}: {rest_gap:.2f}% {'below' if s > 0 else 'above'} market, "
            f"{FILL_MARGIN:.0%} margin short of Claude's {claude_entry:g}")
    if gap_pct > capped:
        note += f" (Claude wanted {gap_pct:.2f}% — capped at one stop-width)"
    return {
        "type": "limit",
        "price": round(limit, 8),
        "claude_entry": claude_entry,
        "gap_pct": round(gap_pct, 3),
        "margin_pct": round(capped - rest_gap, 3),
        "valid_seconds": ORDER_TTL_SECONDS.get(best_tf or "1h", 4 * 3600),
        "note": note,
    }
