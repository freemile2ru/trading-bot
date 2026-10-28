"""Volatility premium, conditional — sell the condor only when implied vol is rich against what has been happening.

research/vrp_lab.py found a premium on average; this asks whether it lives in a regime. The signal, known at the
time: IV(30d) minus the trailing 30-day realised vol. Sell only when that spread is above θ; otherwise sit out.

Run: venv/bin/python -m research.vrp_regime_lab   (research_cache/options_730d.pkl; writes vrp_regime_results.json)

Grid θ ∈ {−5 … +10 vol points}; the first half of the two years picks θ, the second half judges it; reported
per coin: share of time active, P&L per 30-day structure when active vs always-on, and the drawdown.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR
from research.vrp_lab import DATA, FEE, H

OUT = os.path.join(CACHE_DIR, "vrp_regime_results.json")
THETAS = (-5, -2, 0, 2, 4, 6, 8, 10)


def structure_pnl(df: pd.DataFrame) -> pd.Series:
    """Daily-sampled P&L (% of notional) of a 30-day 1σ/2σ condor opened that day — the vrp_lab proxy."""
    s, iv = df["S"], df["iv"]
    T = 30 / 365
    sig = iv * math.sqrt(T)
    move = (s.shift(-H) / s - 1)
    premium = 0.32 * sig
    payout = (move.abs() - sig).clip(lower=0).clip(upper=sig)
    pnl = (premium - payout - FEE).dropna()
    return pnl[pnl.index.hour == 0]


def trailing_rv(s: pd.Series, hours: int = H) -> pd.Series:
    return np.log(s).diff().rolling(hours).std() * math.sqrt(24 * 365)


def summarize(pnl: pd.Series, active: pd.Series) -> dict:
    on = pnl[active.reindex(pnl.index).fillna(False)]
    eq = (1 + pnl.where(active.reindex(pnl.index).fillna(False), 0.0) / 30).cumprod()      # 1/30 of capital per daily structure
    dd = (eq / eq.cummax() - 1).min()
    return {"days": int(len(pnl)), "active_pct": round(100 * len(on) / len(pnl), 1) if len(pnl) else 0.0,
            "pnl_per_30d_pct": round(float(on.mean() * 100), 3) if len(on) else None,
            "win_pct": round(float((on > 0).mean() * 100), 1) if len(on) else None,
            "worst_pct": round(float(on.min() * 100), 2) if len(on) else None,
            "annualised_pct": round(float(on.mean() * 100) * 12 * len(on) / len(pnl), 1) if len(pnl) and len(on) else 0.0,
            "max_dd_pct": round(float(dd * 100), 2)}


def main() -> dict:
    data = pd.read_pickle(DATA)
    res = {}
    for coin, df in data.items():
        df = df.dropna()
        pnl = structure_pnl(df)
        spread = ((df["iv"] - trailing_rv(df["S"])) * 100).reindex(pnl.index)
        half = pnl.index[len(pnl) // 2]
        always = pd.Series(True, index=pnl.index)
        grid = {}
        for th in THETAS:
            act = spread > th
            grid[th] = {"search": summarize(pnl[pnl.index < half], act[act.index < half]),
                        "holdout": summarize(pnl[pnl.index >= half], act[act.index >= half])}
        base = {"search": summarize(pnl[pnl.index < half], always[always.index < half]),
                "holdout": summarize(pnl[pnl.index >= half], always[always.index >= half])}
        # choose θ on the search half by annualised return (active share matters, not just per-trade)
        best = max(THETAS, key=lambda t: grid[t]["search"]["annualised_pct"])
        gain = grid[best]["holdout"]["annualised_pct"] - base["holdout"]["annualised_pct"]
        res[coin] = {"split": str(half.date()), "always_on": base, "chosen_theta": best, "chosen": grid[best],
                     "holdout_gain_annualised_pp": round(gain, 1),
                     "verdict": "filter helps" if gain > 2 else "filter hurts" if gain < -2 else "no clear difference",
                     "grid": {str(t): v for t, v in grid.items()},
                     "spread_now": round(float(spread.dropna().iloc[-1]), 1) if spread.notna().any() else None}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    for coin, r in main().items():
        b, c = r["always_on"]["holdout"], r["chosen"]["holdout"]
        print(f"{coin}: θ={r['chosen_theta']} → {r['verdict']} | holdout always-on {b['annualised_pct']:+.1f}%/yr (per 30d {b['pnl_per_30d_pct']}, DD {b['max_dd_pct']}%) "
              f"vs filtered {c['annualised_pct']:+.1f}%/yr (active {c['active_pct']}%, per 30d {c['pnl_per_30d_pct']}, win {c['win_pct']}%, DD {c['max_dd_pct']}%) | spread now {r['spread_now']}")
        for t, g in r["grid"].items():
            print(f"   θ={t:>3}: search {g['search']['annualised_pct']:+6.1f}%/yr active {g['search']['active_pct']:5.1f}% | holdout {g['holdout']['annualised_pct']:+6.1f}%/yr active {g['holdout']['active_pct']:5.1f}%")
