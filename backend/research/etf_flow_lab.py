"""Spot-ETF flows — do yesterday's net flows into the US spot Bitcoin ETFs predict today's return?

Why: the evidence is split. Lim (SSRN 6592830, Jan 2024–Apr 2025) finds flows explain 21% of same-day variance and
predict the next day (t = 3.1); Fabus et al. (SSRN 7545019) find lagged returns predict flows and lagged flows predict
returns only marginally once the ETF session (16:00 ET close) is aligned with the 24/7 market; DWF Labs (694 days)
finds a next-day correlation of 0.09 — flows follow price. Alignment decides it, so this lab aligns carefully.

Data: Farside's daily table refuses automated fetches (403) and the other public pages carry no history, so the lab
reads research_cache/btc_etf_flows.csv (columns: date, total_flow_musd — one row per US trading day, outflows
negative; paste from farside.co.uk/btc/ or export from a provider). Without it the lab writes a "waiting for data"
result and the shelf says so.

Run: venv/bin/python -m research.etf_flow_lab   (BTC daily closes at 21:00 UTC from the hourly cache; writes
research_cache/etf_flow_results.json)

  alignment flow dated T is known after the 16:00 ET close (21:00 UTC in summer); the return it may predict is
            21:00 UTC on T → 21:00 UTC on the next trading day
  measures  rank correlation of flow and next return; mean next return by flow quintile; the same-day relation
            (flow vs the return into the close) as the "flows follow price" check
  rule      long when flow ≥ +THR, short when ≤ −THR (THR = $100m, $300m), held to the next close, 16 bps a round
            trip; first half chooses THR and whether to short, second half judges (95% block bootstrap).
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

FLOWS = os.path.join(CACHE_DIR, "btc_etf_flows.csv")
HOURLY = os.path.join(CACHE_DIR, "history_365d_40c.pkl")
OUT = os.path.join(CACHE_DIR, "etf_flow_results.json")
COST = 0.0016
THRS = (100.0, 300.0)


def btc_closes_21utc() -> pd.Series:
    hist = pd.read_pickle(HOURLY)
    btc = hist[0]["BTCUSDT"]["c"] if isinstance(hist, tuple) else hist["BTCUSDT"]["c"]
    c = btc[btc.index.hour == 21]
    c.index = c.index.normalize()
    return c


def _ci(v: np.ndarray, seed=1, block=5, boot=2000) -> tuple[float, float]:
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


def main() -> dict:
    if not os.path.exists(FLOWS):
        res = {"status": "waiting for data", "needs": FLOWS, "note": "farside.co.uk refuses automated fetches (403); paste its daily table as date,total_flow_musd"}
        json.dump(res, open(OUT, "w"), indent=1)
        return res
    flows = pd.read_csv(FLOWS, parse_dates=["date"]).dropna()
    flows["date"] = pd.to_datetime(flows["date"], utc=True).dt.normalize()
    flows = flows.set_index("date")["total_flow_musd"].astype(float).sort_index()
    closes = btc_closes_21utc()
    days = closes.index
    rows = []
    for t, f in flows.items():
        if t not in closes.index:
            continue
        i = days.get_loc(t)
        if i + 1 >= len(days) or i == 0:
            continue
        rows.append({"date": t, "flow": f, "same_day": float(closes.iloc[i] / closes.iloc[i - 1] - 1), "next": float(closes.iloc[i + 1] / closes.iloc[i] - 1)})
    df = pd.DataFrame(rows)
    if len(df) < 60:
        res = {"status": "too little overlap", "rows": int(len(df)), "note": "the hourly cache covers one year; extend it or the flow file"}
        json.dump(res, open(OUT, "w"), indent=1)
        return res
    split = df["date"].iloc[len(df) // 2]
    res = {"status": "ran", "days": int(len(df)), "period": [str(df["date"].iloc[0].date()), str(df["date"].iloc[-1].date())], "split": str(split.date()), "cost_bps": COST * 1e4,
           "same_day_rank_corr": round(float(df["flow"].rank().corr(df["same_day"].rank())), 3),
           "next_day_rank_corr": round(float(df["flow"].rank().corr(df["next"].rank())), 3),
           "next_by_flow_quintile_bps": {str(k): round(float(v.mean() * 1e4), 1) for k, v in df["next"].groupby(pd.qcut(df["flow"], 5, labels=["q1 out", "q2", "q3", "q4", "q5 in"]), observed=True)}}
    best, grid = None, []
    for thr in THRS:
        for short in (False, True):
            s = np.where(df["flow"] >= thr, 1, np.where((df["flow"] <= -thr) & short, -1, 0))
            ret = pd.Series(s * df["next"].values - np.where(s != 0, COST, 0.0), index=df["date"])
            ret = ret[s != 0]
            a, b = ret[ret.index < split], ret[ret.index >= split]
            lo, hi = _ci(b.values)
            g = {"thr_musd": thr, "shorts": short, "first_half": {"trades": int(len(a)), "bps_trade": round(float(a.mean() * 1e4), 1) if len(a) else None},
                 "second_half": {"trades": int(len(b)), "bps_trade": round(float(b.mean() * 1e4), 1) if len(b) else None, "ci95_bps": [round(lo * 1e4, 1), round(hi * 1e4, 1)]}}
            grid.append(g)
            if len(a) >= 15 and (best is None or g["first_half"]["bps_trade"] > best["first_half"]["bps_trade"]):
                best = g
    res["rule"] = {"chosen": best, "grid": grid, "verdict": "passes" if best and best["second_half"]["ci95_bps"][0] > 0 else "no edge"}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print(json.dumps({k: v for k, v in r.items() if k != "rule"}, indent=1, default=str))
    if r.get("rule"):
        for g in r["rule"]["grid"]:
            print(g)
        print("→", r["rule"]["verdict"])
