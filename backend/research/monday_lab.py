"""The Monday Asia-open effect — does the week's first move carry through Monday?

Why (Concretum 2025, "Seasonality in Bitcoin Intraday Trend Trading"): intraday trend models on BTC, 2018–2025,
earned most of their edge from Sunday 19:00 ET through the following 24 hours — fresh Asian-week positioning
setting a direction that persists — and more so after mid-2020.

Run: venv/bin/python -m research.monday_lab   (cached 15m candles, 40 perps, a year; writes
research_cache/monday_results.json)

  rule      at WAIT hours after Monday 00:00 UTC, take the direction of the move since 00:00; hold until
            Tuesday 00:00 UTC (also: exit at 12:00). WAIT ∈ {2, 4, 6}
  control   the same rule on every other weekday — if Monday isn't special, it should look the same
  filter    only when the opening move is at least MIN_MOVE (0.5%, 1%)
Costs 16 bps a round trip. Weeks are few (≈57), so the result is a 95% block bootstrap on weekly sums, and the
first 60% of weeks choose WAIT / MIN_MOVE while the last 40% judge once. Also reported for BTC alone.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

KLINES = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
OUT = os.path.join(CACHE_DIR, "monday_results.json")
COST = 0.0016
WAITS = (2, 4, 6)
MIN_MOVES = (0.0, 0.005, 0.01)


def day_trades(df: pd.DataFrame, weekday: int, wait_h: int, min_move: float, exit_h: int = 24) -> pd.DataFrame:
    """One trade per qualifying day: (date, ret) — direction of the first `wait_h` hours, held to `exit_h`."""
    c = df["close"]
    days = sorted({t.normalize() for t in c.index if t.weekday() == weekday})
    out = []
    for d in days:
        try:
            p0, p1, p2 = c.asof(d), c.asof(d + pd.Timedelta(hours=wait_h)), c.asof(d + pd.Timedelta(hours=exit_h))
        except KeyError:
            continue
        if any(pd.isna(x) for x in (p0, p1, p2)) or c.index[0] > d or c.index[-1] < d + pd.Timedelta(hours=exit_h):
            continue
        move = p1 / p0 - 1
        if abs(move) < min_move:
            continue
        s = 1 if move > 0 else -1
        out.append((d, s * (p2 / p1 - 1) - COST))
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
        return {"trades": 0}
    weekly = tr.groupby("date")["ret"].mean()                 # equal weight across coins each day
    lo, hi = _ci(weekly.values)
    return {"trades": int(len(tr)), "days": int(len(weekly)), "per_trade_pct": round(float(tr["ret"].mean() * 100), 3),
            "per_day_pct": round(float(weekly.mean() * 100), 3), "ci95_day_pct": [round(lo * 100, 3), round(hi * 100, 3)],
            "win_pct": round(float((tr["ret"] > 0).mean() * 100), 1), "positive_days_pct": round(float((weekly > 0).mean() * 100), 1)}


def main() -> dict:
    kl = pd.read_pickle(KLINES)
    any_df = next(iter(kl.values()))
    split = any_df.index[int(len(any_df) * 0.6)].normalize()
    res = {"period": [str(any_df.index[0].date()), str(any_df.index[-1].date())], "split": str(split.date()), "cost_bps": COST * 1e4}
    for universe, syms in (("all", list(kl)), ("btc", ["BTCUSDT"])):
        best, grid = None, []
        for wait in WAITS:
            for mm in MIN_MOVES:
                tr = pd.concat([day_trades(kl[s], 0, wait, mm) for s in syms if s in kl], ignore_index=True)
                srch, hold = tr[tr["date"] < split], tr[tr["date"] >= split]
                g = {"wait_h": wait, "min_move": mm, "search": summarize(srch), "holdout": summarize(hold)}
                grid.append(g)
                if g["search"].get("trades") and (best is None or g["search"]["per_day_pct"] > best["search"]["per_day_pct"]):
                    best = g
        # control: the chosen rule on each other weekday, whole period
        ctl = {}
        for wd, name in ((1, "tue"), (2, "wed"), (3, "thu"), (4, "fri")):
            tr = pd.concat([day_trades(kl[s], wd, best["wait_h"], best["min_move"]) for s in syms if s in kl], ignore_index=True)
            ctl[name] = summarize(tr)
        mon_all = summarize(pd.concat([day_trades(kl[s], 0, best["wait_h"], best["min_move"]) for s in syms if s in kl], ignore_index=True))
        res[universe] = {"chosen": {k: best[k] for k in ("wait_h", "min_move")}, "search": best["search"], "holdout": best["holdout"],
                         "monday_whole_period": mon_all, "control_other_weekdays": ctl,
                         "verdict": "passes" if best["holdout"].get("ci95_day_pct", [0])[0] > 0 else "no edge", "grid": grid}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print("period", r["period"], "split", r["split"])
    for u in ("all", "btc"):
        v = r[u]
        print(f"[{u}] chosen {v['chosen']} | search {v['search']['per_day_pct']:+.3f}%/Monday ({v['search']['days']}) | "
              f"holdout {v['holdout'].get('per_day_pct')}%/Monday ({v['holdout'].get('days')}) ci {v['holdout'].get('ci95_day_pct')} → {v['verdict']}")
        print("   whole-period Monday", v["monday_whole_period"]["per_day_pct"], "| other weekdays:",
              {k: c.get("per_day_pct") for k, c in v["control_other_weekdays"].items()})
