"""Scalping rules — pure functions over market features. No LLM, no network.

Entry ("flow burst with book support"): aggressive order flow over the last 30s is
unusually one-sided (z-score vs the last 10 minutes), the last 10s agrees, the depth-5
book leans the same way, the 1-minute trend (EMA9 vs EMA21) isn't against it, the
spread is normal, and price isn't already stretched from its 15-minute VWAP. Entry is
a post-only limit at the touch — the scalper pays the maker fee or doesn't trade.

Exits: a resting maker take-profit, a taker stop, a time stop, and a flow-reversal exit.
"""

from scalper import config as C


def entry_signal(f: dict) -> dict:
    """Returns {"side": "long"|"short"|None, "reasons": [...], "candidate": bool}.

    `candidate` marks near-misses worth logging for research (flow already notable).
    """
    z = f["flow_z"]
    side = "long" if z > 0 else "short"
    s = 1 if side == "long" else -1
    candidate = abs(z) >= C.CANDIDATE_Z
    reasons: list[str] = []

    def no(why: str) -> dict:
        return {"side": None, "reasons": reasons + [why], "candidate": candidate, "wanted": side}

    if abs(z) < C.FLOW_Z_ENTRY:
        return no(f"flow z {z:+.2f} below {C.FLOW_Z_ENTRY}")
    reasons.append(f"flow z {z:+.2f}")
    if f["flow_10s"] * s <= 0:
        return no("last 10s flow disagrees")
    if f["obi"] * s < C.OBI_ENTRY:
        return no(f"book imbalance {f['obi']:+.2f} not supportive")
    reasons.append(f"book {f['obi']:+.2f}")
    if f["ema9"] is None or f["ema21"] is None or f["atr_bps"] is None:
        return no("1m trend/ATR not ready")
    if (f["ema9"] - f["ema21"]) * s < 0:
        return no("1m trend against")
    med = f["median_spread_bps"]
    if f["spread_bps"] is None or (med and f["spread_bps"] > C.MAX_SPREAD_MULT * med):
        return no(f"spread {f['spread_bps']:.2f}bps wide vs median {med:.2f}")
    if f["vwap_dev_atr"] is not None and f["vwap_dev_atr"] * s > C.MAX_VWAP_DEV_ATR:
        return no(f"{f['vwap_dev_atr']:+.1f} ATR from 15m VWAP — chasing")
    return {"side": side, "reasons": reasons, "candidate": True, "wanted": side}


def levels(side: str, entry: float, atr_bps: float) -> dict:
    """TP/SL in bps from 1m ATR, floored so the target clears costs several times over."""
    tp_bps = min(max(C.TP_ATR * atr_bps, C.MIN_TP_BPS), C.MAX_TP_BPS)
    sl_bps = max(C.SL_ATR * atr_bps, C.MIN_SL_BPS)
    s = 1 if side == "long" else -1
    return {
        "take_profit": entry * (1 + s * tp_bps / 1e4),
        "stop_loss": entry * (1 - s * sl_bps / 1e4),
        "tp_bps": round(tp_bps, 2),
        "sl_bps": round(sl_bps, 2),
    }


def should_flow_exit(side: str, f: dict, open_bps: float, tp_bps: float) -> bool:
    """Flow has turned hard against the position and it isn't already well in profit."""
    s = 1 if side == "long" else -1
    return f["flow_z"] * s <= C.FLOW_EXIT_Z and open_bps < 0.5 * tp_bps
