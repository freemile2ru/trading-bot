"""Carry lab — cash-and-carry: own the coin, short its perpetual, collect the funding.

Why there should be an edge: perpetual funding is what leveraged longs pay shorts to keep the perp
pinned to spot. Over 2022–2026 it averaged ~7%/yr on BTC and ETH and 6–9%/yr on many large coins.
A position that is long spot and short the same notional of the perp has (almost) no price risk —
the legs cancel — and collects that funding. The risks are the funding turning negative, the
spot–perp basis moving while the trade is on, and the operational ones (exchange, margin on the
short leg in a squeeze). It is paid for providing leverage to others, not for predicting price.

Daily P&L per unit of notional: spot return − perp return + the day's funding (positive funding is
paid to the short). Costs: opening or closing a position trades both legs — spot taker 0.10% +
perp taker 0.05% + slippage 0.03% on each — 0.21% each way.

Versions:
  always     each coin, carried all the time
  gated      each coin, carried only while its trailing L-day funding is above θ (annualised)
  portfolio  each week, carry the k coins with the highest trailing funding (equal weight)

Walk-forward with an UNTOUCHED final holdout: the first 50% of days choose the parameters, the next
25% confirm them, the last 25% (≈ the most recent year) is judged once. Verdict: the holdout's 95%
block-bootstrap interval of the mean daily return must be above zero. Returns are shown on notional
and on capital (spot paid in full + the perp short margined at 2×: capital = 1.5 × notional).

    ./venv/bin/python -m research.carry_lab
"""
from __future__ import annotations

import json
import math
import os
import random
import time

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

PERP = os.path.join(CACHE_DIR, "daily_4y_40c.pkl")
SPOT = os.path.join(CACHE_DIR, "spot_daily_4y.pkl")
RESULTS = os.path.join(CACHE_DIR, "carry_lab_results.json")
SWITCH_COST = 0.0021            # one side of both legs: spot 0.10% + perp 0.05% + 2 × 0.03% slippage
CAPITAL_PER_NOTIONAL = 1.5
BLOCK, SIMS = 10, 2000


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    perp, funding = pd.read_pickle(PERP)
    spot = pd.read_pickle(SPOT)
    idx = perp.index.intersection(spot.index)
    cols = [c for c in spot.columns if c in perp.columns]
    return perp.loc[idx, cols], spot.loc[idx, cols], funding.reindex(idx)[cols].fillna(0.0)


def carry_returns(perp: pd.DataFrame, spot: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
    """Each coin's daily carry return (before switching costs), earned on day t+1 by a position
    held from day t's close. NaN where either leg has no price."""
    r = spot.pct_change(fill_method=None) - perp.pct_change(fill_method=None) + funding
    return r.shift(-1)


def apply_positions(pos: pd.DataFrame, carry: pd.DataFrame) -> pd.Series:
    """Daily return of holding `pos` (weights per coin, summing to ≤ 1) with switching costs."""
    pos = pos.reindex(carry.index).fillna(0.0)
    gross = (pos * carry.fillna(0.0)).sum(axis=1)
    turnover = pos.diff().abs().sum(axis=1)
    turnover.iloc[0] = pos.iloc[0].abs().sum()
    return (gross - turnover * SWITCH_COST).iloc[:-1]


def gated(funding: pd.DataFrame, lookback: int, theta_pa: float) -> pd.DataFrame:
    trail = funding.rolling(lookback).mean() * 365
    return (trail > theta_pa).astype(float)


def portfolio(funding: pd.DataFrame, valid: pd.DataFrame, lookback: int, k: int, theta_pa: float) -> pd.DataFrame:
    trail = (funding.rolling(lookback).mean() * 365).where(valid)
    w = pd.DataFrame(np.nan, index=funding.index, columns=funding.columns)
    for i in range(lookback, len(trail), 7):
        row = trail.iloc[i].dropna()
        row = row[row > theta_pa].sort_values().tail(k)
        target = pd.Series(0.0, index=funding.columns)
        if len(row):
            target[row.index] = 1.0 / k
        w.iloc[i] = target.values
    return w.ffill().fillna(0.0)


def stats(r: pd.Series, rng: random.Random | None = None) -> dict:
    r = r.dropna()
    if len(r) < 30:
        return {"days": len(r)}
    m, sd = r.mean(), r.std()
    eq = (1 + r).cumprod()
    monthly = (1 + r).groupby(r.index.to_period("M")).prod() - 1
    out = {"days": len(r), "annual_pct": round(((1 + m) ** 365 - 1) * 100, 2),
           "annual_on_capital_pct": round(((1 + m / CAPITAL_PER_NOTIONAL) ** 365 - 1) * 100, 2),
           "sharpe": round(m / sd * math.sqrt(365), 2) if sd else None,
           "max_drawdown_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 2),
           "worst_day_pct": round(float(r.min()) * 100, 2),
           "months_positive_pct": round(float((monthly > 0).mean()) * 100, 1), "mean_bps_day": round(m * 1e4, 3)}
    if rng is not None:
        v, n = r.values, len(r)
        boot = sorted(v[np.concatenate([np.arange(s, s + BLOCK) for s in (rng.randrange(n - BLOCK + 1)
                                                                          for _ in range(math.ceil(n / BLOCK)))])[:n]].mean()
                      for _ in range(SIMS))
        out["ci_bps_day"] = [round(boot[int(.025 * SIMS)] * 1e4, 3), round(boot[int(.975 * SIMS)] * 1e4, 3)]
    return out


def split(index: pd.Index) -> tuple[pd.Timestamp, pd.Timestamp]:
    return index[int(len(index) * .5)], index[int(len(index) * .75)]


def judge(r: pd.Series, a: pd.Timestamp, b: pd.Timestamp, rng: random.Random) -> dict:
    hold = stats(r[r.index >= b], rng)
    ci = hold.get("ci_bps_day")
    return {"search": stats(r[r.index < a]), "confirm": stats(r[(r.index >= a) & (r.index < b)]), "holdout": hold,
            "verdict": "no data" if not ci else "PASS" if ci[0] > 0 else "fails" if ci[1] < 0 else "no edge"}


def main() -> dict:
    perp, spot, funding = load()
    carry = carry_returns(perp, spot, funding)
    valid = perp.notna() & spot.notna()
    a, b = split(carry.index)
    rng = random.Random(6)
    out = {"generated_at": time.time(), "coins": len(carry.columns),
           "period": [str(carry.index[0].date()), str(carry.index[-1].date())],
           "search_until": str(a.date()), "holdout_from": str(b.date()), "versions": []}

    # Per coin, always on — reported for the record, not selected on.
    per_coin = []
    for c in carry.columns:
        pos = valid[[c]].astype(float)
        r = apply_positions(pos, carry[[c]])
        r = r[valid[c].reindex(r.index).fillna(False)]
        per_coin.append({"coin": c, **judge(r, a, b, rng)})
    out["per_coin_always"] = per_coin

    # Gated per coin and portfolio: parameters chosen on the search half only.
    grid_g = [(lb, th) for lb in (3, 7, 14, 30) for th in (0.0, 0.05, 0.10, 0.20)]
    grid_p = [(lb, k, th) for lb in (7, 14, 30) for k in (3, 5, 10) for th in (0.0, 0.05, 0.10)]
    cands = []
    for lb, th in grid_g:
        pos = gated(funding, lb, th).where(valid, 0.0)
        pos = pos.div(valid.sum(axis=1).clip(lower=1), axis=0)          # equal weight across coins
        cands.append(("gated", f"carry any coin whose {lb}-day funding > {th:.0%}/yr (equal weight)", apply_positions(pos, carry)))
    for lb, k, th in grid_p:
        pos = portfolio(funding, valid, lb, k, th)
        cands.append(("portfolio", f"weekly: top {k} by {lb}-day funding, if > {th:.0%}/yr", apply_positions(pos, carry)))
    all_on = valid.astype(float).div(valid.sum(axis=1).clip(lower=1), axis=0)
    cands.append(("always", "carry every coin, always (equal weight)", apply_positions(all_on, carry)))
    for kind in ("always", "gated", "portfolio"):
        group = [c for c in cands if c[0] == kind]
        best = max(group, key=lambda c: stats(c[2][c[2].index < a]).get("sharpe") or -99)
        out["versions"].append({"kind": kind, "chosen": best[1], **judge(best[2], a, b, rng),
                                "grid": [{"rule": d, "search_sharpe": stats(r[r.index < a]).get("sharpe"),
                                          "holdout_annual_pct": stats(r[r.index >= b]).get("annual_pct")} for _, d, r in group]})
    json.dump(out, open(RESULTS, "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    res = main()
    print(f"{res['coins']} coins, {res['period'][0]} → {res['period'][1]}; search until {res['search_until']}, "
          f"untouched holdout from {res['holdout_from']}\n")
    for v in res["versions"]:
        h, s = v["holdout"], v["search"]
        print(f"■ {v['kind']}: {v['chosen']} — {v['verdict']}")
        print(f"   search: {s.get('annual_pct')}%/yr Sharpe {s.get('sharpe')} | confirm: {v['confirm'].get('annual_pct')}%/yr "
              f"| HOLDOUT: {h.get('annual_pct')}%/yr ({h.get('annual_on_capital_pct')}% on capital), Sharpe {h.get('sharpe')}, "
              f"max DD {h.get('max_drawdown_pct')}%, worst day {h.get('worst_day_pct')}%, months + {h.get('months_positive_pct')}%, "
              f"CI {h.get('ci_bps_day')} bps/day")
    print("\nPer coin, always on (holdout year):")
    rows = sorted(res["per_coin_always"], key=lambda x: -(x["holdout"].get("annual_pct") or -99))
    for x in rows:
        h = x["holdout"]
        print(f"   {x['coin']:13} {x['verdict']:8} holdout {h.get('annual_pct')}%/yr Sharpe {h.get('sharpe')} "
              f"DD {h.get('max_drawdown_pct')}% months+ {h.get('months_positive_pct')}%  | search {x['search'].get('annual_pct')}%/yr")
