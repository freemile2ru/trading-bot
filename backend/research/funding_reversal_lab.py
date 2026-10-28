"""Funding extremes, conditioned on the day's direction — does the next day follow?

Why (Shek 2026, SSRN 7520320): an extreme funding print alone predicts little, but what the price did *that day*
splits the outcomes — after extreme negative funding, up-days continued up ≈ +1.9% over 24h, down-days continued
down ≈ −1.7%; a 3.6pp gap with a tight bootstrap interval. Crowded shorts being squeezed vs crowded shorts winning.

Run: venv/bin/python -m research.funding_reversal_lab   (daily perp closes + daily funding, 40 coins, 4 years;
writes research_cache/funding_reversal_results.json)

  event      a coin's day whose funding is in its own lowest (or highest) Q% — the percentile is a parameter
  rules      "follow": trade the event day's direction for the next day; "fade": the opposite
  control    the same rule on random days (no funding condition)
Costs 16 bps a round trip. The first 60% of days choose the percentile, the last 40% judge it once (block
bootstrap on the held-out daily sums). Verdict: the held-out 95% interval must be above zero.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

DAILY = os.path.join(CACHE_DIR, "daily_4y_40c.pkl")
OUT = os.path.join(CACHE_DIR, "funding_reversal_results.json")
COST = 0.0016
QS = (1, 2.5, 5, 10)


def events(perp: pd.DataFrame, funding: pd.DataFrame, q: float, side: str) -> pd.DataFrame:
    """Rows (date, coin, day_ret, next_ret) for days whose funding is in the coin's bottom/top q%."""
    ret = perp.pct_change(fill_method=None)
    nxt = ret.shift(-1)
    out = []
    for c in perp.columns:
        f = funding[c].dropna()
        if len(f) < 200:
            continue
        thr = np.nanpercentile(f, q if side == "neg" else 100 - q)
        mask = (f <= thr) if side == "neg" else (f >= thr)
        for d in f.index[mask]:
            r, n = ret.at[d, c], nxt.at[d, c]
            if pd.notna(r) and pd.notna(n):
                out.append((d, c, float(r), float(n)))
    return pd.DataFrame(out, columns=["date", "coin", "day_ret", "next_ret"])


def pnl(ev: pd.DataFrame, rule: str) -> pd.Series:
    """Daily sum of per-event returns after costs (one unit per event)."""
    s = np.sign(ev["day_ret"]) * (1 if rule == "follow" else -1)
    r = s * ev["next_ret"] - COST
    return r.groupby(ev["date"]).sum()


def _ci(v: np.ndarray, seed=1, block=10, boot=2000) -> tuple[float, float]:
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


def summarize(daily: pd.Series, idx: pd.DatetimeIndex, n_events: int) -> dict:
    d = daily.reindex(idx, fill_value=0.0)
    lo, hi = _ci(d.values)
    return {"events": int(n_events), "per_event_pct": round(float(daily.sum() / max(n_events, 1) * 100), 3),
            "day_pct": round(float(d.mean() * 100), 4), "ci95_day_pct": [round(lo * 100, 4), round(hi * 100, 4)],
            "positive_days_pct": round(float((d[d != 0] > 0).mean() * 100), 1) if (d != 0).any() else None}


def main() -> dict:
    perp, funding = pd.read_pickle(DAILY)
    split = perp.index[int(len(perp) * 0.6)]
    res = {"period": [str(perp.index[0].date()), str(perp.index[-1].date())], "split": str(split.date()), "cost_bps": COST * 1e4}
    for side in ("neg", "pos"):
        for rule in ("follow", "fade"):
            best, grid = None, []
            for q in QS:
                ev = events(perp, funding, q, side)
                srch, hold = ev[ev["date"] < split], ev[ev["date"] >= split]
                s = summarize(pnl(srch, rule), perp.index[perp.index < split], len(srch))
                h = summarize(pnl(hold, rule), perp.index[perp.index >= split], len(hold))
                grid.append({"q": q, "search": s, "holdout": h})
                if best is None or s["day_pct"] > best[1]["search"]["day_pct"]:
                    best = (q, grid[-1])
            q, g = best
            # split the held-out events by the day's direction, as the paper does
            ev = events(perp, funding, q, side)
            hold = ev[ev["date"] >= split]
            up, dn = hold[hold["day_ret"] > 0], hold[hold["day_ret"] <= 0]
            res[f"{side}_{rule}"] = {"chosen_q": q, **g, "holdout_up_days_next_pct": round(float(up["next_ret"].mean() * 100), 3) if len(up) else None,
                                     "holdout_down_days_next_pct": round(float(dn["next_ret"].mean() * 100), 3) if len(dn) else None,
                                     "verdict": "passes" if g["holdout"]["ci95_day_pct"][0] > 0 else "no edge", "grid": grid}
    # control: the follow rule on every day (plain 1-day momentum) over the holdout
    ret = perp.pct_change(fill_method=None)
    ctl = (np.sign(ret) * ret.shift(-1) - COST)
    ctl = ctl[ctl.index >= split].sum(axis=1)
    lo, hi = _ci(ctl.dropna().values)
    res["control_follow_all_days_holdout"] = {"day_pct": round(float(ctl.mean() * 100), 4), "ci95_day_pct": [round(lo * 100, 4), round(hi * 100, 4)]}
    json.dump(res, open(OUT, "w"), indent=1)
    return res


if __name__ == "__main__":
    r = main()
    print("period", r["period"], "split", r["split"])
    for k in ("neg_follow", "neg_fade", "pos_follow", "pos_fade"):
        v = r[k]
        print(f"{k:11s} q={v['chosen_q']:<4} search {v['search']['per_event_pct']:+.3f}%/event ({v['search']['events']}) | "
              f"holdout {v['holdout']['per_event_pct']:+.3f}%/event ({v['holdout']['events']}) ci/day {v['holdout']['ci95_day_pct']} "
              f"| up-days→next {v['holdout_up_days_next_pct']} down-days→next {v['holdout_down_days_next_pct']} | {v['verdict']}")
    print("control (follow every day, holdout):", r["control_follow_all_days_holdout"])
