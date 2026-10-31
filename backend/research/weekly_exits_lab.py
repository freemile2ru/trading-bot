"""Weekly basket exits lab — does the 30% disaster stop (and no take-profit) cause "huge losses, change for wins"?

The weekly near-high / near-low book (experiments/weekly_rs.py) holds ten legs for a week with only a 30% stop. Its
research (signal_rebuild_lab.weekly) measured 7-day returns with NO path — stops and take-profits were never tested.
Live, PEPE's short hit the 30% stop for −1R while the winning legs cash a few percent, which is the asymmetry the
user is seeing. This lab replays the same Monday baskets on the 4-hour bars with every leg's actual path:

  stop       none / 10 / 15 / 20 / 30 % from entry (taker exit at the stop)
  take       none / 5 / 10 / 15 % in the leg's favour (limit exit)
  sizing     equal legs, or legs scaled by 1 / 7-day volatility within each side (wild coins get less)
  trail      (added 2026-10-29, before any trailing result was seen) the 30% stop stays; once a leg is ARM % in
             its favour (0 = from entry) a stop follows the best price at DIST % behind it: ARM 0/10/15/20 ×
             DIST 5/10/15/20. The best price is updated with a bar's extreme only after that bar is checked
             against the stop set by earlier bars (conservative), and a bar opening through a stop fills at the
             open (gap) — applied to every variant, the baseline included.

Reported per variant: net % a week, both halves, positive weeks, 95% bootstrap range, worst week, max drawdown,
average winning leg, average losing leg, the payoff ratio between them, and how often stops hit.
Pre-registered rule (2026-10-29): adopt a variant live only if it beats the baseline (stop 30, no take-profit, equal
legs) on net % a week in BOTH halves AND has a better worst week; a tie keeps the baseline.

    ./venv/bin/python -m research.weekly_exits_lab
"""
from __future__ import annotations

import itertools
import json
import os
import random

import numpy as np
import pandas as pd

from research.signal_rebuild_lab import COST, DATA, TOP

CACHE = os.path.dirname(DATA)
RESULTS = os.path.join(CACHE, "weekly_exits_results.json")
HOLD_BARS = 42                    # 7 days of 4h bars
STOPS = [None, 0.10, 0.15, 0.20, 0.30]
TAKES = [None, 0.05, 0.10, 0.15]
TRAILS = [(arm, dist) for arm in (0.0, 0.10, 0.15, 0.20) for dist in (0.05, 0.10, 0.15, 0.20)]
SIZINGS = ["equal", "inv_vol"]


def frames(data: dict) -> dict[str, pd.DataFrame]:
    out = {}
    for sym, d in data.items():
        if sym == "BTCUSDT":
            continue
        k = d["k"][["o", "h", "l", "c"]].copy()
        k["dist_hi7"] = k["c"] / k["h"].rolling(HOLD_BARS).max() - 1
        k["vol_7d"] = np.log(k["c"]).diff().rolling(HOLD_BARS).std()
        out[sym] = k
    return out


def leg(k: pd.DataFrame, i: int, side: int, stop: float | None, take: float | None,
        trail: tuple[float, float] | None = None) -> tuple[float, str]:
    """Signed net return of one leg opened at bar i's close and walked through the next HOLD_BARS bars."""
    entry = k["c"].iloc[i] * (1 + side * COST)                   # taker entry
    disaster = entry * (1 - side * stop) if stop else None
    take_px = entry * (1 + side * take) if take else None
    best = entry
    end = min(i + HOLD_BARS, len(k) - 1)
    for j in range(i + 1, end + 1):
        o, h, l = k["o"].iloc[j], k["h"].iloc[j], k["l"].iloc[j]
        stop_px, how = disaster, "stop"
        if trail and side * (best / entry - 1) >= trail[0]:                  # armed: follow the best price
            t_px = best * (1 - side * trail[1])
            if stop_px is None or side * (t_px - stop_px) > 0:
                stop_px, how = t_px, "trail"
        hit_stop = stop_px is not None and ((l <= stop_px) if side > 0 else (h >= stop_px))
        hit_take = take_px is not None and ((h >= take_px) if side > 0 else (l <= take_px))
        if hit_stop:                                                  # a bar that touches both: the stop is assumed first (conservative)
            fill = min(stop_px, o) if side > 0 else max(stop_px, o)   # a bar opening through the stop fills at the open
            return side * (fill * (1 - side * COST) / entry - 1), how
        best = max(best, h) if side > 0 else min(best, l)
        if hit_take:
            return side * (take_px / entry - 1) - COST / 4, "take"    # a resting limit: a quarter of the taker cost
    exit_px = k["c"].iloc[end] * (1 - side * COST)
    return side * (exit_px / entry - 1), "hold"


def simulate(fr: dict[str, pd.DataFrame], stop, take, sizing, trail=None) -> dict:
    mondays = sorted({t for k in fr.values() for t in k.index if t.dayofweek == 0 and t.hour == 0})
    weeks, legs = [], []
    for t in mondays:
        rows = []
        for sym, k in fr.items():
            if t not in k.index:
                continue
            i = k.index.get_loc(t)
            if i < HOLD_BARS or i + 1 >= len(k) or np.isnan(k["dist_hi7"].iloc[i]) or np.isnan(k["vol_7d"].iloc[i]):
                continue
            rows.append((sym, i, k["dist_hi7"].iloc[i], k["vol_7d"].iloc[i]))
        if len(rows) < 2 * TOP + 2:
            continue
        rows.sort(key=lambda r: r[2])
        picks = [(r, -1) for r in rows[:TOP]] + [(r, 1) for r in rows[-TOP:]]
        total = 0.0
        for side in (-1, 1):
            group = [(r, s) for r, s in picks if s == side]
            if sizing == "inv_vol":
                inv = np.array([1 / max(r[3], 1e-6) for r, _ in group])
                w = inv / inv.sum() * 0.5
            else:
                w = np.full(len(group), 0.5 / len(group))
            for (r, s), wi in zip(group, w):
                ret, how = leg(fr[r[0]], r[1], s, stop, take, trail)
                legs.append({"ret": ret, "how": how, "w": wi})
                total += wi * ret
        weeks.append((t, total))
    w = pd.Series(dict(weeks)).sort_index()
    half = len(w) // 2
    rng = random.Random(4)
    means = sorted(float(np.mean(rng.choices(list(w.values), k=len(w)))) for _ in range(2000))
    eq = (1 + w).cumprod()
    wins = [x["ret"] for x in legs if x["ret"] > 0]
    losses = [x["ret"] for x in legs if x["ret"] <= 0]
    return {"stop": stop, "take": take, "sizing": sizing, "trail": list(trail) if trail else None, "weeks": int(len(w)),
            "net_week_pct": round(float(w.mean() * 100), 3), "first_half": round(float(w.iloc[:half].mean() * 100), 3),
            "second_half": round(float(w.iloc[half:].mean() * 100), 3), "positive_weeks_pct": round(float((w > 0).mean() * 100), 1),
            "ci95_pct": [round(means[50] * 100, 3), round(means[1949] * 100, 3)], "worst_week_pct": round(float(w.min() * 100), 2),
            "max_drawdown_pct": round(float((eq / eq.cummax() - 1).min() * 100), 2),
            "leg_win_rate_pct": round(100 * len(wins) / len(legs), 1), "avg_win_leg_pct": round(float(np.mean(wins)) * 100, 2) if wins else None,
            "avg_loss_leg_pct": round(float(np.mean(losses)) * 100, 2) if losses else None,
            "payoff": round(abs(np.mean(wins) / np.mean(losses)), 2) if wins and losses else None,
            "stops_hit_pct": round(100 * sum(1 for x in legs if x["how"] == "stop") / len(legs), 1),
            "trails_hit_pct": round(100 * sum(1 for x in legs if x["how"] == "trail") / len(legs), 1),
            "takes_hit_pct": round(100 * sum(1 for x in legs if x["how"] == "take") / len(legs), 1)}


def label(v: dict) -> str:
    t = v.get("trail")
    trail = (f" · trail {int(t[1] * 100)}% after +{int(t[0] * 100)}%" if t[0] else f" · trail {int(t[1] * 100)}% from entry") if t else ""
    return (f"stop {int(v['stop'] * 100)}%" if v["stop"] else "no stop") + " · " + (f"take {int(v['take'] * 100)}%" if v["take"] else "no take") + trail + f" · {v['sizing']}"


def main() -> dict:
    data = pd.read_pickle(DATA)
    fr = frames(data)
    variants = [simulate(fr, s, t, z) for s, t, z in itertools.product(STOPS, TAKES, SIZINGS)]
    variants += [simulate(fr, 0.30, None, z, tr) for tr, z in itertools.product(TRAILS, SIZINGS)]
    base = next(v for v in variants if v["stop"] == 0.30 and v["take"] is None and v["sizing"] == "equal" and not v["trail"])
    trails = sorted((v for v in variants if v["trail"]), key=lambda v: -v["net_week_pct"])
    ok = [v for v in variants if v["first_half"] > base["first_half"] and v["second_half"] > base["second_half"]
          and v["worst_week_pct"] > base["worst_week_pct"]]
    chosen = max(ok, key=lambda v: v["net_week_pct"]) if ok else None
    report = {"coins": len(fr), "weeks": base["weeks"], "baseline": base, "variants": sorted(variants, key=lambda v: -v["net_week_pct"]),
              "trails": trails, "chosen": chosen, "verdict": ("adopt " + label(chosen)) if chosen else "keep the baseline — no variant beat it on both halves and the worst week",
              "rule": "adopt only if better than the baseline on net %/week in BOTH halves and on the worst week"}
    json.dump(report, open(RESULTS, "w"), indent=1, default=str)
    return report


if __name__ == "__main__":
    rep = main()
    print(f"{rep['coins']} coins, {rep['weeks']} weeks. Baseline = {label(rep['baseline'])}\n")
    print(f"{'variant':52} {'net/wk':>7} {'1st':>6} {'2nd':>6} {'+wks':>5} {'worst':>7} {'maxDD':>7} {'win%':>5} {'avgW':>6} {'avgL':>6} {'payoff':>6} {'stops':>5} {'trail':>5}")
    for v in rep["variants"][:16] + [rep["baseline"]] + [None] + rep["trails"]:
        if v is None:
            print("\n— trailing stops (30% disaster stop kept) —")
            continue
        print(f"{label(v):52} {v['net_week_pct']:7.3f} {v['first_half']:6.2f} {v['second_half']:6.2f} {v['positive_weeks_pct']:5.0f} {v['worst_week_pct']:7.2f} "
              f"{v['max_drawdown_pct']:7.2f} {v['leg_win_rate_pct']:5.1f} {v['avg_win_leg_pct']:6.2f} {v['avg_loss_leg_pct']:6.2f} {v['payoff']:6.2f} {v['stops_hit_pct']:5.1f} {v['trails_hit_pct']:5.1f}")
    print(f"\nVerdict: {rep['verdict']}\nWritten to {RESULTS}")
