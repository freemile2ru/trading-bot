"""Short-term reversal — do last week's losers beat last week's winners, and is it only in the thin names?

Why (Liu, Tsyvinski & Wu 2022 found one-to-four-week momentum in the largest coins; later work — Dobrynskaya,
a four-factor model with a reversal factor — finds a one-week reversal premium, and one study locates it in the
less-traded pairs, which reads as a liquidity-provision premium rather than a risk premium): the sign at one week is
unsettled, and whether it survives 16 bps a leg in the liquid names is the question that matters to us.

Run: venv/bin/python -m research.reversal_lab   (cached 4 years of daily closes for 40 perps, and the hourly
cache's quote volume for the last year's liquidity split; writes research_cache/reversal_results.json)

  rule      each rebalance, rank by the trailing LOOKBACK-day return (1, 3, 7); long the K worst, short the K best
            (reversal) or the reverse (momentum), equal weight, held HOLD days (1, 7); K = 8; 16 bps a round trip a leg
  split     the first two years choose sign × LOOKBACK × HOLD; the last two judge (95% block bootstrap on per-rebalance
            returns). Then the chosen rule on the last year, in the 20 most-traded names and the 20 least.
"""
from __future__ import annotations

import json
import os
import pickle
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

DAILY = os.path.join(CACHE_DIR, "daily_4y_40c.pkl")
HOURLY = os.path.join(CACHE_DIR, "history_365d_40c.pkl")
OUT = os.path.join(CACHE_DIR, "reversal_results.json")
COST = 0.0016
K = 8
LOOKBACKS = (1, 3, 7)
HOLDS = (1, 7)
SIGNS = {"reversal": -1, "momentum": 1}


def rebalances(closes: pd.DataFrame, sign: int, lookback: int, hold: int) -> pd.DataFrame:
    past = closes / closes.shift(lookback) - 1
    days = closes.index
    out = []
    for i in range(lookback, len(days) - hold, hold):
        d0, d1 = days[i], days[i + hold]
        s = past.loc[d0].dropna()
        if len(s) < 2 * K + 4:
            continue
        ranked = s.sort_values()
        longs = list(ranked.index[:K]) if sign < 0 else list(ranked.index[-K:])
        shorts = list(ranked.index[-K:]) if sign < 0 else list(ranked.index[:K])
        fwd = closes.loc[d1] / closes.loc[d0] - 1
        legs = [fwd[c] - COST for c in longs] + [-fwd[c] - COST for c in shorts]
        legs = [x for x in legs if np.isfinite(x)]
        if legs:
            out.append((d0, float(np.mean(legs))))
    return pd.DataFrame(out, columns=["date", "ret"])


def _ci(v: np.ndarray, seed=1, block=4, boot=2000) -> tuple[float, float]:
    rng, n, means = random.Random(seed), len(v), []
    if n == 0:
        return 0.0, 0.0
    for _ in range(boot):
        ix: list[int] = []
        while len(ix) < n:
            st = rng.randrange(0, max(n - block, 1)); ix += range(st, st + block)
        means.append(v[ix[:n]].mean())
    means.sort()
    return means[int(boot * .025)], means[int(boot * .975)]


def summarize(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"rebalances": 0}
    lo, hi = _ci(tr["ret"].values)
    return {"rebalances": int(len(tr)), "per_rebalance_pct": round(float(tr["ret"].mean() * 100), 3), "ci95_pct": [round(lo * 100, 3), round(hi * 100, 3)],
            "positive_pct": round(float((tr["ret"] > 0).mean() * 100), 1), "sum_pct": round(float(tr["ret"].sum() * 100), 1),
            "t": round(float(tr["ret"].mean() / (tr["ret"].std(ddof=1) / np.sqrt(len(tr)))), 2) if len(tr) > 2 and tr["ret"].std() > 0 else None}


def main() -> dict:
    closes, _ = pickle.load(open(DAILY, "rb"))
    closes = closes.sort_index()
    split = closes.index[len(closes) // 2]
    res = {"coins": int(closes.shape[1]), "period": [str(closes.index[0].date()), str(closes.index[-1].date())], "split": str(split.date()), "k": K, "cost_bps": COST * 1e4, "grid": []}
    best = None
    for sign_name, sign in SIGNS.items():
        for lb in LOOKBACKS:
            for hold in HOLDS:
                tr = rebalances(closes, sign, lb, hold)
                a, b = tr[tr["date"] < split], tr[tr["date"] >= split]
                g = {"sign": sign_name, "lookback": lb, "hold": hold, "first_half": summarize(a), "second_half": summarize(b)}
                res["grid"].append(g)
                if g["first_half"].get("rebalances") and (best is None or g["first_half"]["per_rebalance_pct"] > best["first_half"]["per_rebalance_pct"]):
                    best = g
    res["chosen"] = best
    res["verdict"] = "passes" if best and best["second_half"].get("ci95_pct", [0])[0] > 0 else "no edge"
    # liquidity split on the last year: the chosen rule in the 20 most- and 20 least-traded names
    try:
        hist = pd.read_pickle(HOURLY)
        hist = hist[0] if isinstance(hist, tuple) else hist
        qv = pd.Series({s: float(d["qv"].sum()) for s, d in hist.items() if s in closes.columns}).sort_values()
        thin, thick = list(qv.index[:20]), list(qv.index[-20:])
        last_year = closes[closes.index >= closes.index[-1] - pd.Timedelta(days=365)]
        res["liquidity_last_year"] = {}
        for name, cols in (("most_traded_20", thick), ("least_traded_20", thin)):
            tr = rebalances(last_year[cols], SIGNS[best["sign"]], best["lookback"], best["hold"])
            res["liquidity_last_year"][name] = summarize(tr)
        # and the pure one-week reversal in each, whatever was chosen
        res["liquidity_last_year"]["reversal_7_7_most_traded"] = summarize(rebalances(last_year[thick], -1, 7, 7))
        res["liquidity_last_year"]["reversal_7_7_least_traded"] = summarize(rebalances(last_year[thin], -1, 7, 7))
    except Exception as e:
        res["liquidity_last_year"] = {"error": str(e)}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print("coins", r["coins"], "period", r["period"], "split", r["split"])
    for g in sorted(r["grid"], key=lambda g: -(g["first_half"].get("per_rebalance_pct") or -9)):
        print(f"{g['sign']:9} lb {g['lookback']} hold {g['hold']} | first {g['first_half'].get('per_rebalance_pct')}% ({g['first_half'].get('rebalances')}) "
              f"| second {g['second_half'].get('per_rebalance_pct')}% ci {g['second_half'].get('ci95_pct')} t {g['second_half'].get('t')}")
    print("chosen", {k: r["chosen"][k] for k in ("sign", "lookback", "hold")}, "→", r["verdict"])
    print("liquidity (last year):", r["liquidity_last_year"])
