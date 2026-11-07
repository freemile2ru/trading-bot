"""Basis as a cross-sectional signal — does ranking perps by their premium pick the next week's winners and losers?

Why (Chi 2023, J. Futures Markets; Cao, Luo, Cheng & Dong 2026, SSRN 6365329): across 134–170 predictors the perp
basis (the premium of the perp over the index, which the funding rate is built from) is the strongest predictor of
which coins outperform which; a two-factor model of log-basis plus a price-volume factor explains every significant
sort. Our carry books earn the basis as income on one coin at a time; this asks whether it RANKS coins.

Run: venv/bin/python -m research.basis_xs_lab   (fetches a year of 8h premium-index klines, funding and daily closes
for the 40 perps of the 15m cache, cached under research_cache/basis_xs/; writes research_cache/basis_xs_results.json)

  signal    the average premium index over the last LOOKBACK days (1, 3, 7)
  rule      each rebalance, long the K coins with the lowest signal and short the K with the highest (K = 8), or the
            reverse ("sign"), equal weight; held HOLD days (1, 7); total return = price return ± funding paid over the hold
  costs     16 bps a round trip a leg
  judging   the first half of the days chooses sign × LOOKBACK × HOLD by average return per rebalance; the second
            half judges the chosen rule once: a 95% block bootstrap on per-rebalance returns must sit above zero.
"""
from __future__ import annotations

import json
import os
import random
import time

import httpx
import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

KLINES = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
CACHE = os.path.join(CACHE_DIR, "basis_xs")
OUT = os.path.join(CACHE_DIR, "basis_xs_results.json")
FAPI = "https://fapi.binance.com/fapi/v1"
DAYS = 400
COST = 0.0016
K = 8
LOOKBACKS = (1, 3, 7)
HOLDS = (1, 7)
SIGNS = {"long_low_basis": -1, "long_high_basis": 1}


def _page(c: httpx.Client, path: str, params: dict, start_ms: int, key: str = "startTime", stamp=lambda r: r[0]) -> list:
    rows, t = [], start_ms
    while True:
        try:
            r = c.get(f"{FAPI}/{path}", params={**params, key: t, "limit": 1000}, timeout=30).json()
        except Exception:
            break
        if not isinstance(r, list) or not r:
            break
        rows += r
        if len(r) < 1000:
            break
        t = stamp(r[-1]) + 1
        time.sleep(0.12)
    return rows


def load(c: httpx.Client, sym: str) -> pd.DataFrame | None:
    """Daily rows: close, mean premium index over the day (8h klines), funding paid that day (sum of the three prints)."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{sym}.pkl")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 24 * 3600:
        return pd.read_pickle(p)
    start = int((time.time() - DAYS * 86400) * 1000)
    kl = _page(c, "klines", {"symbol": sym, "interval": "1d"}, start)
    if len(kl) < 120:
        return None
    pk = _page(c, "premiumIndexKlines", {"symbol": sym, "interval": "8h"}, start)
    fr = _page(c, "fundingRate", {"symbol": sym}, start, stamp=lambda r: r["fundingTime"])
    day = pd.to_datetime([k[0] for k in kl], unit="ms", utc=True).normalize()
    df = pd.DataFrame({"close": [float(k[4]) for k in kl], "qv": [float(k[7]) for k in kl]}, index=day)
    prem = pd.Series({pd.Timestamp(k[0], unit="ms", tz="UTC"): float(k[4]) for k in pk})
    df["prem"] = prem.groupby(prem.index.normalize()).mean().reindex(df.index)
    fund = pd.Series({pd.Timestamp(int(x["fundingTime"]), unit="ms", tz="UTC"): float(x["fundingRate"]) for x in fr})
    df["fund"] = fund.groupby(fund.index.normalize()).sum().reindex(df.index).fillna(0.0)
    df = df[~df.index.duplicated()].sort_index()
    pd.to_pickle(df, p)
    return df


def rebalances(data: dict[str, pd.DataFrame], sign: int, lookback: int, hold: int) -> pd.DataFrame:
    """One row per rebalance day: the equal-weight return of the long K / short K basket over the next `hold` days."""
    closes = pd.DataFrame({s: d["close"] for s, d in data.items()})
    prem = pd.DataFrame({s: d["prem"] for s, d in data.items()})
    fund = pd.DataFrame({s: d["fund"] for s, d in data.items()})
    sig = prem.rolling(lookback, min_periods=lookback).mean()
    days = closes.index
    out = []
    for i in range(lookback, len(days) - hold, hold):
        d0, d1 = days[i], days[i + hold]
        s = sig.loc[d0].dropna()
        if len(s) < 2 * K + 4:
            continue
        ranked = s.sort_values()
        longs = list(ranked.index[:K]) if sign < 0 else list(ranked.index[-K:])
        shorts = list(ranked.index[-K:]) if sign < 0 else list(ranked.index[:K])
        pr = closes.loc[d1] / closes.loc[d0] - 1
        fpaid = fund.loc[days[i + 1]:d1].sum()                       # funding prints over the hold, after entry
        legs = [pr[c] - fpaid[c] - COST for c in longs] + [-pr[c] + fpaid[c] - COST for c in shorts]
        legs = [x for x in legs if np.isfinite(x)]
        if legs:
            out.append((d0, float(np.mean(legs)), len(legs)))
    return pd.DataFrame(out, columns=["date", "ret", "legs"])


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
            "positive_pct": round(float((tr["ret"] > 0).mean() * 100), 1), "sum_pct": round(float(tr["ret"].sum() * 100), 2),
            "t": round(float(tr["ret"].mean() / (tr["ret"].std(ddof=1) / np.sqrt(len(tr)))), 2) if len(tr) > 2 and tr["ret"].std() > 0 else None}


def main() -> dict:
    syms = list(pd.read_pickle(KLINES).keys())
    data = {}
    with httpx.Client() as c:
        for s in syms:
            d = load(c, s)
            if d is not None:
                data[s] = d
    days = next(iter(data.values())).index
    split = days[len(days) // 2]
    res = {"coins": len(data), "period": [str(days[0].date()), str(days[-1].date())], "split": str(split.date()), "k": K, "cost_bps": COST * 1e4,
           "signal_spread": {}, "grid": [], "chosen": None}
    # descriptive: the premium's cross-sectional spread (is there anything to rank on?)
    prem = pd.DataFrame({s: d["prem"] for s, d in data.items()})
    res["signal_spread"] = {"median_coin_prem_bps_8h": round(float(prem.median().median() * 1e4), 2),
                            "p10_p90_coin_prem_bps_8h": [round(float(prem.mean().quantile(q) * 1e4), 2) for q in (0.1, 0.9)]}
    best = None
    for sign_name, sign in SIGNS.items():
        for lb in LOOKBACKS:
            for hold in HOLDS:
                tr = rebalances(data, sign, lb, hold)
                a, b = tr[tr["date"] < split], tr[tr["date"] >= split]
                g = {"sign": sign_name, "lookback": lb, "hold": hold, "first_half": summarize(a), "second_half": summarize(b), "all": summarize(tr)}
                res["grid"].append(g)
                if g["first_half"].get("rebalances") and (best is None or g["first_half"]["per_rebalance_pct"] > best["first_half"]["per_rebalance_pct"]):
                    best = g
    res["chosen"] = best
    res["verdict"] = "passes" if best and best["second_half"].get("ci95_pct", [0])[0] > 0 else "no edge"
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print("coins", r["coins"], "period", r["period"], "split", r["split"], "premium spread", r["signal_spread"])
    for g in sorted(r["grid"], key=lambda g: -(g["first_half"].get("per_rebalance_pct") or -9)):
        print(f"{g['sign']:16} lb {g['lookback']} hold {g['hold']} | first {g['first_half'].get('per_rebalance_pct')}% ({g['first_half'].get('rebalances')}) "
              f"| second {g['second_half'].get('per_rebalance_pct')}% ci {g['second_half'].get('ci95_pct')} t {g['second_half'].get('t')}")
    print("chosen", {k: r["chosen"][k] for k in ("sign", "lookback", "hold")}, "→", r["verdict"])
