"""Weekly near-high basket with a funding filter — do the shorts that pay funding drag the book?

The weekly book (experiments/weekly_rs.py) longs the 5 coins nearest their 7-day high and shorts the 5 furthest
below, for a week. Shorting a coin whose funding is negative means paying it three times a day; longing one with
rich positive funding does the same. This replays the basket on the year of 4h data behind the original study
(research_cache/signal_rebuild_data.pkl: 40 perps, klines + 8h funding), nets the funding, and tests skipping
legs whose trailing 7-day funding would cost more than SKIP bps/day.

Run: venv/bin/python -m research.weekly_funding_lab   (writes research_cache/weekly_funding_results.json)

Variants: baseline (no filter); skip legs paying ≥ 5 / 10 / 20 bps a day; "replace" (fill the skipped slot with
the next-ranked coin that doesn't pay). First 60% of weeks choose, last 40% judge; block bootstrap on weekly sums.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

DATA = os.path.join(CACHE_DIR, "signal_rebuild_data.pkl")
OUT = os.path.join(CACHE_DIR, "weekly_funding_results.json")
TOP, COST = 5, 0.0016                       # taker in and out, per leg
SKIPS = (None, 5.0, 10.0, 20.0)             # bps/day of funding a leg would pay


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    d = pd.read_pickle(DATA)
    close = pd.DataFrame({c: v["k"]["c"] for c, v in d.items()}).sort_index()
    fund = pd.DataFrame({c: v["funding"] for c, v in d.items()}).sort_index()
    return close, fund


def weeks(close: pd.DataFrame) -> list[pd.Timestamp]:
    idx = close.index
    return [t for t in idx if t.weekday() == 0 and t.hour == 0]


def replay(close: pd.DataFrame, fund: pd.DataFrame, skip_bps: float | None, replace: bool) -> pd.DataFrame:
    """Weekly rows: date, gross, funding, net (fractions of capital, 10% a leg)."""
    hi7 = close.rolling(42).max()
    out = []
    mondays = weeks(close)
    for a, b in zip(mondays[:-1], mondays[1:]):
        if a not in close.index or b not in close.index:
            continue
        sc = (close.loc[a] / hi7.loc[a] - 1).dropna()
        if len(sc) < 2 * TOP:
            continue
        # trailing 7d funding per 8h → bps/day a short would PAY (negative funding) / a long would PAY (positive)
        f7 = fund[(fund.index > a - pd.Timedelta(days=7)) & (fund.index <= a)].mean() * 3 * 1e4
        ranked = sc.sort_values()
        shorts, longs = [], []
        for c in ranked.index:                                   # worst first
            pay = -f7.get(c, 0.0)
            if skip_bps is not None and pay >= skip_bps:
                if not replace:
                    shorts.append(None)
                continue
            shorts.append(c)
            if len(shorts) >= TOP:
                break
        for c in ranked.index[::-1]:
            pay = f7.get(c, 0.0)
            if skip_bps is not None and pay >= skip_bps:
                if not replace:
                    longs.append(None)
                continue
            longs.append(c)
            if len(longs) >= TOP:
                break
        shorts, longs = shorts[:TOP], longs[:TOP]
        gross = fnd = 0.0
        fw = fund[(fund.index > a) & (fund.index <= b)].sum()     # funding over the holding week per coin
        for c in longs:
            if c is None:
                continue
            gross += 0.1 * (close.at[b, c] / close.at[a, c] - 1) - 0.1 * COST
            fnd -= 0.1 * fw.get(c, 0.0)                           # long pays positive funding
        for c in shorts:
            if c is None:
                continue
            gross += 0.1 * (1 - close.at[b, c] / close.at[a, c]) - 0.1 * COST
            fnd += 0.1 * fw.get(c, 0.0)                           # short receives positive funding
        out.append({"date": a, "gross": gross, "funding": fnd, "net": gross + fnd,
                    "legs": sum(1 for c in longs + shorts if c is not None)})
    return pd.DataFrame(out)


def _ci(v: np.ndarray, seed=1, block=4, boot=2000) -> tuple[float, float]:
    rng, n, out = random.Random(seed), len(v), []
    if n == 0:
        return 0.0, 0.0
    for _ in range(boot):
        ix: list[int] = []
        while len(ix) < n:
            st = rng.randrange(0, max(n - block, 1)); ix += range(st, st + block)
        out.append(v[ix[:n]].mean())
    out.sort()
    return out[int(boot * .025)], out[int(boot * .975)]


def summarize(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"weeks": 0}
    lo, hi = _ci(df["net"].values)
    return {"weeks": int(len(df)), "net_week_pct": round(float(df["net"].mean() * 100), 3), "gross_week_pct": round(float(df["gross"].mean() * 100), 3),
            "funding_week_pct": round(float(df["funding"].mean() * 100), 3), "ci95_week_pct": [round(lo * 100, 3), round(hi * 100, 3)],
            "weeks_positive_pct": round(float((df["net"] > 0).mean() * 100), 1), "avg_legs": round(float(df["legs"].mean()), 1)}


def main() -> dict:
    close, fund = load()
    res = {"period": [str(close.index[0].date()), str(close.index[-1].date())], "coins": int(close.shape[1]), "variants": {}}
    split = None
    for skip in SKIPS:
        for replace in ((False, True) if skip is not None else (False,)):
            df = replay(close, fund, skip, replace)
            if split is None and not df.empty:
                split = df["date"].iloc[int(len(df) * 0.6)]
            key = "baseline" if skip is None else f"skip_{skip:g}bps{'_replace' if replace else ''}"
            res["variants"][key] = {"search": summarize(df[df["date"] < split]), "holdout": summarize(df[df["date"] >= split]), "all": summarize(df)}
    res["split"] = str(split.date()) if split is not None else None
    base = res["variants"]["baseline"]
    best = max((k for k in res["variants"] if k != "baseline"), key=lambda k: res["variants"][k]["search"].get("net_week_pct") or -9)
    gain = (res["variants"][best]["holdout"].get("net_week_pct") or 0) - (base["holdout"].get("net_week_pct") or 0)
    res.update(chosen=best, holdout_gain_week_pct=round(gain, 3),
               verdict="filter helps" if gain > 0.1 else "filter hurts" if gain < -0.1 else "no clear difference")
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print("period", r["period"], "split", r["split"], "→", r["chosen"], r["verdict"], f"(holdout {r['holdout_gain_week_pct']:+.3f}%/week vs baseline)")
    for k, v in r["variants"].items():
        s, h = v["search"], v["holdout"]
        print(f"   {k:20s} search net {s.get('net_week_pct')}%/wk (funding {s.get('funding_week_pct')}) | holdout net {h.get('net_week_pct')}%/wk ci {h.get('ci95_week_pct')} +weeks {h.get('weeks_positive_pct')}% legs {h.get('avg_legs')}")
