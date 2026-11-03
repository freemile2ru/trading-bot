"""The reversal lane's entry score — what research/entry_lab.py found in the scanner's own readings (2026-10-31).

Over the scanner's recorded history, what it likes most reverses over the next day: its conviction, TA and FA scores,
24h/7d returns and ranks, open-interest growth, volume growth, funding, taker buying and news bursts all carry a
NEGATIVE rank IC against the next 24 hours versus the cross-section; the top-trader long share a positive one. The lab
scored those readings' cross-sectional ranks, signed, and the score held out of sample (IC 0.10 on the second half).
The year-long panel says plain momentum fading loses in trending markets, so this is a LANE at measuring size, sized by
its own record per regime — not a promoted rule.

The score is rebuilt from a sweep's scans the way the lab built it from the stored rows: each chosen reading's
percentile rank across the coins scanned, signed by the lab's IC, averaged. Higher = buy, lower = sell.
"""
from __future__ import annotations

import json
import os

RESULTS = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "entry_results.json")
# The lab's chosen readings and signs, as of 2026-10-31 (the file overrides these when present).
DEFAULT_CHOSEN: dict[str, float] = {
    "oi_change_24h_pct": -1.0, "fa_score": -1.0, "rank_24h": -1.0, "rs_24h": -1.0, "ret_24h": -1.0, "fa_relative_strength": -1.0,
    "fa_momentum_shape": -1.0, "conviction": -1.0, "ta_score": -1.0, "composite": -1.0, "ret_7d": -1.0, "rs_7d": -1.0, "rank_7d": -1.0,
    "oi_change_7d_pct": -1.0, "volume_change_24h": -1.0, "chain_tvl_change_7d_pct": -1.0, "funding_rate": -1.0, "taker_buy_sell_24h": -1.0,
    "fa_liquidity": -1.0, "news_burst": -1.0, "oi_price_confirm": -1.0, "top_trader_long_pct": 1.0,
}
MIN_COINS = 8


def chosen() -> dict[str, float]:
    try:
        d = json.load(open(RESULTS))
        c = d.get("chosen") or {}
        return {k: float(v) for k, v in c.items()} if c else dict(DEFAULT_CHOSEN)
    except Exception:
        return dict(DEFAULT_CHOSEN)


def readings(scan: dict) -> dict[str, float]:
    """A live scan's readings under the same names the lab used (storage/signals.add_scan_features)."""
    fa = scan.get("fa") or {}
    out: dict = {"conviction": scan.get("conviction"), "composite": scan.get("composite_score"), "ta_score": scan.get("ta_score"), "fa_score": fa.get("score")}
    for k, v in (fa.get("features") or {}).items():
        out[k] = v
    for k, v in (fa.get("components") or {}).items():
        out[f"fa_{k}"] = (v or {}).get("score") if isinstance(v, dict) else v
    news = ((scan.get("signal_package") or {}).get("news_summary")) or {}
    if news.get("score") is not None:
        out.update(news_score=news["score"], news_burst=news.get("burst"), news_stories_6h=news.get("stories_6h"))
    clean = {}
    for k, v in out.items():
        try:
            if v is not None and not isinstance(v, (list, dict, str)):
                clean[k] = float(v)
        except (TypeError, ValueError):
            continue
    return clean


def score(scans: list[dict], chosen_: dict[str, float] | None = None) -> dict[str, float]:
    """{symbol: score} — the signed average of each chosen reading's percentile rank across the scans. Coins missing a
    reading just don't take part in that reading's rank; a coin needs half the readings to be scored."""
    chosen_ = chosen_ or chosen()
    rows = {s["symbol"]: readings(s) for s in scans if s.get("symbol")}
    if len(rows) < MIN_COINS:
        return {}
    parts: dict[str, list[float]] = {sym: [] for sym in rows}
    for feat, sign in chosen_.items():
        have = [(sym, r[feat]) for sym, r in rows.items() if feat in r]
        if len(have) < MIN_COINS:
            continue
        have.sort(key=lambda x: x[1])
        n = len(have)
        for i, (sym, _) in enumerate(have):
            parts[sym].append(sign * ((i + 0.5) / n - 0.5))
    need = max(1, len(chosen_) // 2)
    return {sym: round(sum(p) / len(p), 4) for sym, p in parts.items() if len(p) >= need}


def picks(scores: dict[str, float], k: int, sides: tuple[str, ...] = ("short",)) -> list[tuple[str, str]]:
    """The slot's legs: the bottom k of the score sold, the top k bought — for the sides the lane runs. Short only by
    default (2026-11-02): the lab's 190 slots paid the short side +1.7%/slot and charged the long side −0.4%."""
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    out: list[tuple[str, str]] = []
    if "short" in sides:
        out += [(sym, "short") for sym, _ in ranked[:k]]
    if "long" in sides:
        out += [(sym, "long") for sym, _ in ranked[-k:]]
    return out
