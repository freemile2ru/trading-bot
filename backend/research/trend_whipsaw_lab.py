"""Trend whipsaw — does a buffer, a weekly check or a cooling-off period stop the trend books flapping?

Run: venv/bin/python -m research.trend_whipsaw_lab   (cached daily data: research_cache/equity_daily_10y.pkl,
macro_etfs_20y.pkl; writes research_cache/trend_whipsaw_results.json)

The live trend books (markets/engine.py) go long when the 126-day return is > 0 and flat otherwise,
checked every day, with a 15% disaster stop — and the next check re-buys if the trend is still up.
Live, GOOGL and AMZN flipped out, back in and out again within days, and AMD was re-bought two hours
after its disaster stop. Each flip costs fees and, near the threshold, usually a small loss.

Variants, each run per asset as long/flat with the live sizing (1% daily vol target, capped at full,
equal weight) and 10 bps a switch:
  live        126d > 0, daily, re-entry allowed right after a disaster stop
  band X      enter above +X%, exit below −X% (hysteresis)
  weekly      only check on the last trading day of the week (stop still daily)
  cooldown N  no re-entry for N trading days after a disaster stop
Universes: the Dow as of 2016 (hindsight-free), 68 US stocks, 15 Asian stocks, the commodity ETFs.
Judged on annual return, Sharpe, max drawdown and switches per asset-year, first vs second half.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "trend_whipsaw_results.json")
LOOK, VOL_TARGET, DISASTER, COST = 126, 0.01, 0.15, 0.0010
COMMODITIES = ["GLD", "SLV", "PPLT", "PALL", "CPER", "USO", "UNG"]


def run(px: pd.DataFrame, band: float = 0.0, weekly: bool = False, cooldown: int = 0) -> dict:
    px = px.dropna(how="all")
    ret = px.pct_change()
    trend = px / px.shift(LOOK) - 1
    vol = ret.rolling(20).std()
    week_end = pd.Series(px.index, index=px.index).dt.to_period("W").ne(
        pd.Series(px.index, index=px.index).dt.to_period("W").shift(-1)).values
    n_assets = px.shape[1]
    daily = np.zeros(len(px))
    switches = 0
    for j, col in enumerate(px.columns):
        p, tr, v, r = px[col].values, trend[col].values, vol[col].values, ret[col].values
        pos, entry, block_until, w = 0, 0.0, -1, 0.0
        for i in range(LOOK + 21, len(p)):
            if np.isnan(p[i]) or np.isnan(tr[i - 1]):
                continue
            # today's return earned by yesterday's position
            if pos and not np.isnan(r[i]):
                daily[i] += w * r[i] / n_assets
            # disaster stop on today's close
            if pos and p[i] <= entry * (1 - DISASTER):
                pos, block_until = 0, i + cooldown
                daily[i] -= w * COST / n_assets
                switches += 1
                continue
            if weekly and not week_end[i]:
                continue
            t = tr[i]
            if np.isnan(t):
                continue
            want = (t > band) if not pos else (t > -band)
            if want and not pos and i > block_until and v[i] > 0:
                pos, entry, w = 1, p[i], min(VOL_TARGET / v[i], 1.0)
                daily[i] -= w * COST / n_assets
                switches += 1
            elif not want and pos:
                pos = 0
                daily[i] -= w * COST / n_assets
                switches += 1
    d = pd.Series(daily, index=px.index).iloc[LOOK + 21:]
    years = len(d) / 252
    eq = (1 + d).cumprod()
    half = len(d) // 2
    ann = lambda x: float((1 + x).prod() ** (252 / len(x)) - 1) * 100
    return {"annual_pct": round(ann(d), 2), "sharpe": round(float(d.mean() / d.std() * np.sqrt(252)), 2) if d.std() else 0,
            "max_dd_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
            "switches_per_asset_year": round(switches / n_assets / years, 1),
            "first_half_pct": round(ann(d.iloc[:half]), 2), "second_half_pct": round(ann(d.iloc[half:]), 2)}


VARIANTS = {"live (daily, no band)": {}, "band ±2%": {"band": 0.02}, "band ±5%": {"band": 0.05}, "band ±10%": {"band": 0.10},
            "weekly": {"weekly": True}, "cooldown 20d": {"cooldown": 20},
            "band ±5% + cooldown 20d": {"band": 0.05, "cooldown": 20},
            "weekly + band ±5% + cooldown 20d": {"weekly": True, "band": 0.05, "cooldown": 20}}


def main() -> dict:
    eq = pd.read_pickle(os.path.join(CACHE_DIR, "equity_daily_10y.pkl"))
    macro = pd.read_pickle(os.path.join(CACHE_DIR, "macro_etfs_20y.pkl"))
    universes = {"Dow 2016 (hindsight-free)": eq["dow_2016"]["px"], "US stocks (68)": eq["us"]["px"],
                 "Asian stocks (15)": eq["asia"]["px"], "Commodity ETFs (7)": macro[COMMODITIES]}
    out = {u: {k: run(px, **v) for k, v in VARIANTS.items()} for u, px in universes.items()}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    for u, res in main().items():
        print(f"== {u}")
        for k, v in res.items():
            print(f"  {k:34s} {v['annual_pct']:+6.2f}%/yr sharpe {v['sharpe']:5.2f} maxDD {v['max_dd_pct']:6.1f}% "
                  f"switches {v['switches_per_asset_year']:5.1f}/asset/yr | halves {v['first_half_pct']:+6.2f} {v['second_half_pct']:+6.2f}")
