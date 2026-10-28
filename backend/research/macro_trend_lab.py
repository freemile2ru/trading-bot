"""Macro trend lab — diversified trend following across commodities, indices, FX and bonds.

Time-series momentum ("managed futures": long what has been rising, short what has been falling,
across many unrelated markets, sized to equal risk) is the best-documented edge outside crypto —
positive across a century of data in most studies, because trends in macro markets come from slow
information flow, hedging pressure and policy cycles, and because it pays most in crises.

Markets: what Binance trades as perpetuals (gold, silver, platinum, palladium, copper, WTI oil,
natural gas; SPY / QQQ / IWM; Japan, Korea, Brazil), measured with ETFs that hold the real thing
for 20 years — ETF prices include the cost of rolling futures, which raw continuous-futures series
don't. A wider set adds bonds and currencies (not on Binance) as a robustness check.

Rules: each market long when its L-day return is positive, short (or flat) when negative; sized to
equal volatility (~1% a day each, capped at 1/N of capital); daily. Variants: L ∈ {63, 126, 252}
days and their average ("blend"). Costs: 0.08% per side of turnover (perp taker + slippage).

Judged like everything else, with an untouched holdout: chosen on 2006–2014, confirmed on
2015–2019, judged once on 2020–2026. Verdict: holdout 95% block-bootstrap interval of mean daily
return above zero — and, for the long/flat variants, above simply holding the same markets.

    ./venv/bin/python -m research.macro_trend_lab
"""
from __future__ import annotations

import json
import math
import os
import random
import time

import httpx
import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

DATA = os.path.join(CACHE_DIR, "macro_etfs_20y.pkl")
RESULTS = os.path.join(CACHE_DIR, "macro_trend_results.json")
# ETF → the Binance perpetual it stands in for.
BINANCE = {"GLD": "XAUUSDT", "SLV": "XAGUSDT", "PPLT": "XPTUSDT", "PALL": "XPDUSDT", "CPER": "COPPERUSDT",
           "USO": "CLUSDT", "UNG": "NATGASUSDT", "SPY": "SPYUSDT", "QQQ": "QQQUSDT", "IWM": "IWMUSDT",
           "EWJ": "EWJUSDT", "EWY": "EWYUSDT", "EWZ": "EWZUSDT"}
EXTRA = ["TLT", "IEF", "FXE", "FXY", "FXB", "FXA", "DBA", "EFA", "EEM"]      # bonds, currencies, ag, intl — robustness
COST = 0.0008
VOL_TARGET = 0.01
BLOCK, SIMS = 20, 2000
SEARCH_END, CONFIRM_END = "2015-01-01", "2020-01-01"


def _yahoo(t: str) -> pd.Series | None:
    # Explicit dates: with range=max Yahoo silently switches to MONTHLY bars.
    j = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}",
                  params={"period1": int(pd.Timestamp("2005-01-01").timestamp()), "period2": int(time.time()), "interval": "1d"},
                  headers={"User-Agent": "Mozilla/5.0"}, timeout=30).json()["chart"]["result"]
    if not j:
        return None
    j = j[0]
    adj = (j["indicators"].get("adjclose") or [{}])[0].get("adjclose") or j["indicators"]["quote"][0]["close"]
    s = pd.Series(adj, index=pd.to_datetime(j["timestamp"], unit="s").normalize(), dtype=float).dropna()
    return s[~s.index.duplicated(keep="last")]


def _check_daily(df: pd.DataFrame) -> None:
    gaps = df.index.to_series().diff().dt.days.median()
    if gaps is None or gaps > 2:
        raise RuntimeError(f"price data isn't daily (median gap {gaps} days) — Yahoo changed what it returns")


def load(refresh: bool = False) -> pd.DataFrame:
    if os.path.exists(DATA) and not refresh and time.time() - os.path.getmtime(DATA) < 86_400:
        cached = pd.read_pickle(DATA)
        if len(cached) > 1000:
            return cached
    px = {}
    for t in list(BINANCE) + EXTRA:
        s = _yahoo(t)
        if s is not None:
            px[t] = s[s.index >= "2005-01-01"]
        time.sleep(0.2)
    df = pd.DataFrame(px).sort_index()
    df = df[df.notna().sum(axis=1) >= 3]
    _check_daily(df)
    pd.to_pickle(df, DATA)
    return df


def weights(px: pd.DataFrame, lookbacks: list[int], mode: str) -> pd.DataFrame:
    sig = sum(np.sign(px.pct_change(lb, fill_method=None)) for lb in lookbacks) / len(lookbacks)
    if mode == "long_flat":
        sig = sig.clip(lower=0)
    vol = px.pct_change(fill_method=None).rolling(60).std()
    n = px.notna().sum(axis=1).clip(lower=1)
    return (sig * (VOL_TARGET / vol).clip(upper=1.0)).div(n, axis=0).where(px.notna()).fillna(0.0)


def run(w: pd.DataFrame, px: pd.DataFrame) -> pd.Series:
    rets = px.pct_change(fill_method=None).shift(-1).fillna(0.0)
    gross = (w * rets).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    return (gross - turnover * COST).iloc[:-1]


def stats(r: pd.Series, rng: random.Random | None = None) -> dict:
    r = r.dropna()
    if len(r) < 60:
        return {"days": len(r)}
    m, sd = r.mean(), r.std()
    eq = (1 + r).cumprod()
    yearly = (1 + r).groupby(r.index.year).prod() - 1
    out = {"days": len(r), "annual_pct": round(((1 + m) ** 252 - 1) * 100, 2), "vol_pct": round(sd * math.sqrt(252) * 100, 1),
           "sharpe": round(m / sd * math.sqrt(252), 2) if sd else None,
           "max_drawdown_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
           "years_positive": f"{int((yearly > 0).sum())}/{len(yearly)}", "mean_bps_day": round(m * 1e4, 2)}
    if rng is not None:
        v, n = r.values, len(r)
        boot = sorted(v[np.concatenate([np.arange(s, s + BLOCK) for s in (rng.randrange(n - BLOCK + 1)
                                                                          for _ in range(math.ceil(n / BLOCK)))])[:n]].mean()
                      for _ in range(SIMS))
        out["ci_bps_day"] = [round(boot[int(.025 * SIMS)] * 1e4, 2), round(boot[int(.975 * SIMS)] * 1e4, 2)]
    return out


def evaluate(px: pd.DataFrame, label: str, rng: random.Random) -> dict:
    hold = run(px.notna().astype(float).div(px.notna().sum(axis=1).clip(lower=1), axis=0), px)
    variants = {}
    for mode in ("long_short", "long_flat"):
        for lbs, name in (([63], "3-month"), ([126], "6-month"), ([252], "12-month"), ([63, 126, 252], "blend 3/6/12-month")):
            variants[f"{name} trend, {mode.replace('_', '/')}"] = (mode, run(weights(px, lbs, mode), px))
    a, b = pd.Timestamp(SEARCH_END), pd.Timestamp(CONFIRM_END)
    best = max(variants, key=lambda k: stats(variants[k][1][variants[k][1].index < a]).get("sharpe") or -9)
    mode, r = variants[best]
    ho = r[r.index >= b]
    h_stats = stats(ho, rng)
    excess = stats(ho - hold.reindex(ho.index).fillna(0.0), rng) if mode == "long_flat" else None
    ci = h_stats.get("ci_bps_day")
    ok = ci and ci[0] > 0 and (excess is None or (excess.get("ci_bps_day") or [-1])[0] > 0)
    return {"universe": label, "markets": list(px.columns), "chosen": best,
            "search": stats(r[r.index < a]), "confirm": stats(r[(r.index >= a) & (r.index < b)]), "holdout": h_stats,
            "holdout_vs_holding": excess, "holdout_holding": stats(hold[hold.index >= b]),
            "verdict": "PASS" if ok else ("fails" if ci and ci[1] < 0 else "no edge"),
            "all_variants_holdout": {k: stats(v[v.index >= b]) for k, (_, v) in variants.items()},
            "crisis_years": {str(y): round(float((1 + r[r.index.year == y]).prod() - 1) * 100, 1) for y in (2008, 2020, 2022) if (r.index.year == y).any()}}


def main(refresh: bool = False) -> dict:
    px = load(refresh)
    rng = random.Random(8)
    out = {"generated_at": time.time(), "period": [str(px.index[0].date()), str(px.index[-1].date())],
           "search_until": SEARCH_END, "holdout_from": CONFIRM_END,
           "results": [evaluate(px[[c for c in BINANCE if c in px]], "Binance-tradeable markets (13)", rng),
                       evaluate(px, "wider set incl. bonds & currencies (robustness)", rng)]}
    json.dump(out, open(RESULTS, "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    res = main()
    print(f"{res['period'][0]} → {res['period'][1]}; chosen on data to {res['search_until']}, holdout from {res['holdout_from']}\n")
    for r in res["results"]:
        h, s, c = r["holdout"], r["search"], r["confirm"]
        print(f"■ {r['universe']} — {r['verdict']}\n   chosen: {r['chosen']}")
        print(f"   search: {s.get('annual_pct')}%/yr Sharpe {s.get('sharpe')} | confirm: {c.get('annual_pct')}%/yr Sharpe {c.get('sharpe')}")
        print(f"   HOLDOUT: {h.get('annual_pct')}%/yr, vol {h.get('vol_pct')}%, Sharpe {h.get('sharpe')}, max DD {h.get('max_drawdown_pct')}%, "
              f"years + {h.get('years_positive')}, CI {h.get('ci_bps_day')} bps/day")
        hh = r["holdout_holding"]
        print(f"   holding the same markets (holdout): {hh.get('annual_pct')}%/yr Sharpe {hh.get('sharpe')} DD {hh.get('max_drawdown_pct')}%"
              + (f" | trend vs holding CI {r['holdout_vs_holding'].get('ci_bps_day')}" if r["holdout_vs_holding"] else ""))
        print(f"   crisis years (chosen rule): {r['crisis_years']}")
        print("   every variant on the holdout: " + "; ".join(f"{k}: Sharpe {v.get('sharpe')}" for k, v in r["all_variants_holdout"].items()) + "\n")
