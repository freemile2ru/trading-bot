"""Support and resistance — does trading off repeatedly-touched levels work, on a year of 15m data?

Run: venv/bin/python -m research.sr_lab   (cached 15m candles, 40 perps; writes research_cache/sr_lab_results.json)

Levels are found causally: swing highs/lows on 1h bars (a bar whose high/low is the extreme of the 3 bars either
side) over the trailing 7 days, clustered within CLUSTER of each other; a level counts once it has TOUCHES or more
swings in its cluster (the literature's "consensus" levels bounce more than arbitrary ones).

  bounce     price comes within NEAR of a level from inside the range and the 15m bar closes back away from it →
             enter away from the level; stop BEYOND × ATR-ish distance past the level; target the next level or
             2R, whichever is nearer; give up after 24h
  breakout   a 15m close BEYOND past a level → follow it; stop at the level's far side (the range's middle if
             nearer); target 2R; 24h
  luck       the bounce rule on levels shifted by a random 1–3% (same geometry, meaningless lines)
Costs: taker in and out (0.08% each). Results in R after costs, per signal; one trade per coin at a time; first
60% vs last 40%; 95% block bootstrap on daily sums.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "sr_lab_results.json")
KLINES = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
FILL = 0.0008
CLUSTER, NEAR, BEYOND, TOUCHES = 0.004, 0.0015, 0.004, 2
LOOKBACK_H, HOLD_BARS, RISK = 7 * 24, 96, 0.005


def swings(h1: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    hi, lo = h1["high"], h1["low"]
    top = hi[(hi == hi.rolling(7, center=True).max())]
    bot = lo[(lo == lo.rolling(7, center=True).min())]
    return top.dropna(), bot.dropna()


def levels_at(top: pd.Series, bot: pd.Series, t: pd.Timestamp) -> list[float]:
    """Clustered swing levels confirmed (3 bars after the swing) before t, from the last 7 days."""
    cutoff = t - pd.Timedelta(hours=LOOKBACK_H)
    pts = sorted(list(top[(top.index < t - pd.Timedelta(hours=3)) & (top.index >= cutoff)].values)
                 + list(bot[(bot.index < t - pd.Timedelta(hours=3)) & (bot.index >= cutoff)].values))
    out, cur = [], []
    for p in pts:
        if cur and p / cur[0] - 1 > CLUSTER:
            if len(cur) >= TOUCHES:
                out.append(float(np.mean(cur)))
            cur = []
        cur.append(p)
    if len(cur) >= TOUCHES:
        out.append(float(np.mean(cur)))
    return out


def run(df: pd.DataFrame, mode: str, rng: random.Random | None = None) -> list[dict]:
    h1 = df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    top, bot = swings(h1)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    idx = df.index
    atr = (df["high"] - df["low"]).rolling(96).mean().values
    out, i, n = [], 8 * 96, len(df)
    cache_t, cache_lv = None, []
    while i < n - HOLD_BARS - 1:
        t = idx[i]
        if cache_t is None or (t - cache_t) >= pd.Timedelta(hours=4):
            cache_t, cache_lv = t, levels_at(top, bot, t)
            if rng is not None:
                cache_lv = [lv * (1 + rng.choice((-1, 1)) * rng.uniform(0.01, 0.03)) for lv in cache_lv]
        trade = None
        for lv in cache_lv:
            if mode in ("bounce", "luck"):
                touched = l[i] <= lv * (1 + NEAR) and c[i - 1] > lv and c[i] > lv          # came down to support, closed above
                touched_r = h[i] >= lv * (1 - NEAR) and c[i - 1] < lv and c[i] < lv        # came up to resistance, closed below
                if touched or touched_r:
                    s = 1 if touched else -1
                    entry = o[i + 1]
                    stop = lv * (1 - s * BEYOND)
                    others = [x for x in cache_lv if (x - entry) * s > 0]
                    tgt_lv = min(others, key=lambda x: abs(x - entry)) if others else None
                    risk = abs(entry - stop)
                    target = entry + s * 2 * risk
                    if tgt_lv is not None and (tgt_lv - entry) * s < 2 * risk:
                        target = tgt_lv
                    if (target - entry) * s < 0.5 * risk:
                        continue
                    trade = (s, entry, stop, target)
                    break
            else:
                if c[i] > lv * (1 + BEYOND) and c[i - 1] <= lv * (1 + BEYOND) and h[i - 4:i].max() < lv * (1 + BEYOND):
                    s, entry = 1, o[i + 1]
                elif c[i] < lv * (1 - BEYOND) and c[i - 1] >= lv * (1 - BEYOND) and l[i - 4:i].min() > lv * (1 - BEYOND):
                    s, entry = -1, o[i + 1]
                else:
                    continue
                stop = lv * (1 - s * BEYOND)
                risk = abs(entry - stop)
                if risk / entry < 0.002:
                    continue
                trade = (s, entry, stop, entry + s * 2 * risk)
                break
        if trade is None:
            i += 1
            continue
        s, entry, stop, target = trade
        risk = abs(entry - stop) / entry
        px, fee_out, j = None, FILL, i + 1
        for j in range(i + 1, min(i + 1 + HOLD_BARS, n)):
            if (l[j] <= stop) if s > 0 else (h[j] >= stop):
                px = min(stop, o[j]) if s > 0 else max(stop, o[j]); break
            if (h[j] >= target) if s > 0 else (l[j] <= target):
                px, fee_out = target, 0.0002; break
        if px is None:
            px = c[j]
        r = ((px - entry) * s / entry - FILL - fee_out) / risk
        out.append({"ts": idx[i + 1], "r": r, "risk_pct": risk * 100})
        i = j + 1
    return out


def _ci(v: np.ndarray, seed=1, block=10, boot=2000) -> tuple[float, float]:
    rng, n, means = random.Random(seed), len(v), []
    for _ in range(boot):
        ix: list[int] = []
        while len(ix) < n:
            st = rng.randrange(0, max(n - block, 1)); ix += range(st, st + block)
        means.append(v[ix[:n]].mean())
    means.sort()
    return means[int(boot * .025)], means[int(boot * .975)]


def summarize(trades: list[dict]) -> dict:
    df = pd.DataFrame(trades)
    daily = df.assign(eq=df["r"] * RISK).groupby(df["ts"].dt.floor("D"))["eq"].sum()
    days = pd.date_range(daily.index.min(), daily.index.max(), freq="D", tz="UTC")
    daily = daily.reindex(days, fill_value=0.0)
    split = int(len(daily) * 0.6); lo, hi = _ci(daily.values)
    return {"trades": int(len(df)), "per_day": round(len(df) / len(daily), 1), "avg_r": round(float(df["r"].mean()), 3),
            "win_pct": round(float((df["r"] > 0).mean() * 100), 1), "median_stop_pct": round(float(df["risk_pct"].median()), 2),
            "day_pct": round(float(daily.mean() * 100), 3), "first_pct": round(float(daily.iloc[:split].mean() * 100), 3),
            "last_pct": round(float(daily.iloc[split:].mean() * 100), 3), "ci95_pct": [round(lo * 100, 3), round(hi * 100, 3)],
            "positive_days_pct": round(float((daily > 0).mean() * 100), 1), "passes": bool(lo > 0)}


def main() -> dict:
    kl = pd.read_pickle(KLINES)
    res = {}
    for mode in ("bounce", "breakout", "luck"):
        trades = []
        for sym, df in kl.items():
            trades += run(df, mode, random.Random(hash(sym) % 997) if mode == "luck" else None)
        res[mode] = summarize(trades)
    json.dump(res, open(OUT, "w"), indent=1)
    return res


if __name__ == "__main__":
    for k, v in main().items():
        print(f"{k:9s} {v['trades']:5d} trades ({v['per_day']}/day) avg {v['avg_r']:+.3f}R win {v['win_pct']}% stop {v['median_stop_pct']}% | "
              f"{v['day_pct']:+.3f}%/day (first {v['first_pct']:+.3f}, last {v['last_pct']:+.3f}) ci95 {v['ci95_pct']} +days {v['positive_days_pct']}% {'PASS' if v['passes'] else ''}")
