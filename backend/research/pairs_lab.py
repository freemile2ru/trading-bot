"""Pairs lab — do closely linked coins' price gaps snap back, enough to pay two legs of costs?

Uses the cached year of hourly closes (40 perpetuals). On the FIRST half only: for every pair, the
hedge ratio (OLS of log prices) and how strongly the spread mean-reverts (its half-life); the
pairs whose spread reverted fastest (half-life 6 h – 5 days) and stayed highly correlated are
picked. On the SECOND half those pairs are traded: enter when the spread's z-score (rolling 7-day
mean / sd, so nothing from the future) passes ±Z, exit at 0 or after a time stop. Costs: 0.08% per
side on both legs; funding on both legs. Daily P&L across the selected pairs; verdict: 95% block
bootstrap interval of the test half's mean daily return above zero.

    ./venv/bin/python -m research.pairs_lab
"""
from __future__ import annotations

import itertools
import json
import math
import os
import random
import time

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

RESULTS = os.path.join(CACHE_DIR, "pairs_lab_results.json")
COST = 0.0008
WINDOW = 24 * 7


def half_life(spread: pd.Series) -> float:
    s = spread.dropna()
    lag, d = s.shift(1).iloc[1:], s.diff().iloc[1:]
    beta = np.polyfit(lag.values, d.values, 1)[0]
    return -math.log(2) / beta if beta < 0 else float("inf")


def trade_pair(la: pd.Series, lb: pd.Series, beta: float, fa: pd.Series, fb: pd.Series, z_in: float,
               max_hold: int) -> pd.Series:
    """Hourly P&L (per unit gross notional) of the pair; position long A / short β·B when z < −z_in."""
    spread = la - beta * lb
    z = (spread - spread.rolling(WINDOW).mean()) / spread.rolling(WINDOW).std()
    pos = pd.Series(0.0, index=spread.index)
    cur, held = 0.0, 0
    zv = z.values
    out = np.zeros(len(zv))
    for i in range(len(zv)):
        if np.isnan(zv[i]):
            out[i] = cur
            continue
        if cur == 0 and abs(zv[i]) > z_in:
            cur, held = -np.sign(zv[i]), 0
        elif cur != 0:
            held += 1
            if (cur > 0 and zv[i] >= 0) or (cur < 0 and zv[i] <= 0) or held >= max_hold:
                cur = 0.0
        out[i] = cur
    pos[:] = out
    ra, rb = np.exp(la).pct_change(), np.exp(lb).pct_change()
    w = 1 / (1 + abs(beta))                                 # split gross notional between the legs
    leg = pos.shift(1) * (w * ra - w * beta * rb)
    turn = pos.diff().abs().fillna(0.0) * (w + w * abs(beta))
    fund = pos.shift(1) * (w * fa - w * beta * fb)          # long A pays A's funding, short B receives B's
    return (leg - turn * COST - fund).fillna(0.0)


def main() -> dict:
    px, fund, _ = pd.read_pickle(os.path.join(CACHE_DIR, "history_365d_40c.pkl"))
    closes = pd.DataFrame({s: d["c"] for s, d in px.items()}).sort_index()
    closes = closes.loc[:, closes.notna().mean() > 0.95].ffill()
    logp = np.log(closes)
    hourly_fund = pd.DataFrame({s: f for s, f in fund.items()}).reindex(closes.index, method=None).fillna(0.0)
    hourly_fund = hourly_fund.reindex(columns=closes.columns).fillna(0.0)
    mid = closes.index[len(closes) // 2]
    train, test = logp[logp.index < mid], logp[logp.index >= mid]
    rets = train.diff()
    cands = []
    for a, b in itertools.combinations(closes.columns, 2):
        corr = rets[a].corr(rets[b])
        if corr < 0.7:
            continue
        beta = np.polyfit(train[b].values, train[a].values, 1)[0]
        hl = half_life(train[a] - beta * train[b])
        if 6 <= hl <= 120:
            cands.append((a, b, beta, hl, corr))
    cands.sort(key=lambda x: x[3])
    picked = cands[:10]
    rng = random.Random(2)
    results = {}
    for z_in, max_hold in ((2.0, 72), (2.5, 72), (2.0, 168)):
        pnl_train, pnl_test = [], []
        for a, b, beta, hl, corr in picked:
            p = trade_pair(logp[a], logp[b], beta, hourly_fund[a], hourly_fund[b], z_in, max_hold)
            pnl_train.append(p[p.index < mid])
            pnl_test.append(p[p.index >= mid])
        tr = pd.concat(pnl_train, axis=1).mean(axis=1).resample("1D").sum() if pnl_train else pd.Series(dtype=float)
        te = pd.concat(pnl_test, axis=1).mean(axis=1).resample("1D").sum() if pnl_test else pd.Series(dtype=float)
        v, n = te.values, len(te)
        boot = sorted(v[np.concatenate([np.arange(s, s + 7) for s in (rng.randrange(n - 7) for _ in range(math.ceil(n / 7)))])[:n]].mean()
                      for _ in range(2000)) if n > 20 else [np.nan] * 2000
        results[f"z {z_in}, max {max_hold}h"] = {
            "train_annual_pct": round(tr.mean() * 365 * 100, 1), "test_annual_pct": round(te.mean() * 365 * 100, 1),
            "test_sharpe": round(te.mean() / te.std() * math.sqrt(365), 2) if te.std() else None,
            "test_ci_bps_day": [round(boot[50] * 1e4, 2), round(boot[1950] * 1e4, 2)],
            "verdict": "PASS" if boot[50] > 0 else "fails" if boot[1950] < 0 else "no edge"}
    out = {"generated_at": time.time(), "pairs_considered": len(cands),
           "picked": [{"pair": f"{a}/{b}", "beta": round(beta, 3), "half_life_h": round(hl, 1), "corr": round(corr, 2)}
                      for a, b, beta, hl, corr in picked], "split": str(mid.date()), "results": results}
    json.dump(out, open(RESULTS, "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    r = main()
    print(f"{r['pairs_considered']} pairs met corr ≥ 0.7 and half-life 6h–5d on the first half (to {r['split']}); traded the 10 fastest:")
    print("  " + ", ".join(f"{p['pair']} (hl {p['half_life_h']}h)" for p in r["picked"]))
    for k, v in r["results"].items():
        print(f"  {k}: train {v['train_annual_pct']}%/yr | TEST {v['test_annual_pct']}%/yr, Sharpe {v['test_sharpe']}, CI {v['test_ci_bps_day']} bps/day → {v['verdict']}")
