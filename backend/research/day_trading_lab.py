"""Day trading — setups that use ONLY the current UTC day's data, flat by the day's end.

Run: venv/bin/python -m research.day_trading_lab   (cached 15m candles, 40 perps, a year;
writes research_cache/day_trading_results.json)

Asked 2026-10-26: make each trade specific to its day — decided on that day's numbers, not long-term
history. Each setup below sees only bars from 00:00 UTC of the trade's day, takes at most one trade per
coin per day, and is out by 23:45 UTC.

  ORB        opening-range breakout: the 00:00–02:00 high/low; first 15m close beyond it → follow it;
             stop at the other side of the range; out at the day's end
  ORB fade   the same break, faded: stop one range-width beyond the entry, target the range's middle
  VWAP fade  after 03:00, a close ≥ 2 standard deviations (of today's closes) from today's VWAP → fade it;
             stop 1.5 sd beyond; target the VWAP
  12h follow at 12:00, the move since 00:00 (if > 0.5 × today's range so far) → follow it; stop at today's
             opposite extreme; out at the day's end
  12h fade   the same move, faded: stop 1× the move beyond; target the day's open
Costs: taker in (0.05% + 0.03% slippage), taker out (maker 0.02% at a limit target). Results in R of each
trade's risk, per signal; daily = sum at 0.5% risk a trade; first 60% vs last 40%; 95% block bootstrap;
luck = the same entries and stops, random direction.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "day_trading_results.json")
KLINES = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
TAKER, MAKER, RISK = 0.0008, 0.0002, 0.005


def _walk(o, h, l, c, i0: int, end: int, s: int, entry: float, stop: float, target: float | None) -> tuple[float, float]:
    """(exit price, exit fee) walking bars i0..end-1 — stop first (pessimistic), then target, else last close."""
    for j in range(i0, end):
        if (l[j] <= stop) if s > 0 else (h[j] >= stop):
            return (min(stop, o[j]) if s > 0 else max(stop, o[j])), TAKER
        if target is not None and ((h[j] >= target) if s > 0 else (l[j] <= target)):
            return target, MAKER
    return c[end - 1], TAKER


def day_trades(df: pd.DataFrame, setup: str, rng: random.Random | None = None) -> list[dict]:
    out = []
    df = df.copy()
    df["day"] = df.index.floor("D")
    for day, g in df.groupby("day"):
        if len(g) < 90:
            continue
        o, h, l, c, v = (g[k].to_numpy() for k in ("open", "high", "low", "close", "volume"))
        n = len(g)
        trade = None
        if setup in ("ORB", "ORB fade"):
            hi, lo = h[:8].max(), l[:8].min()
            width = hi - lo
            for i in range(8, n - 1):
                s = 1 if c[i] > hi else -1 if c[i] < lo else 0
                if not s:
                    continue
                entry = o[i + 1]
                if setup == "ORB":
                    stop, target = (lo if s > 0 else hi), None
                else:
                    s = -s
                    stop, target = entry - s * width, (hi + lo) / 2
                trade = (i + 1, s, entry, stop, target)
                break
        elif setup == "VWAP fade":
            pv = np.cumsum(c * v)
            vwap = pv / np.maximum(np.cumsum(v), 1e-12)
            for i in range(12, n - 1):
                sd = c[: i + 1].std()
                if sd <= 0:
                    continue
                z = (c[i] - vwap[i]) / sd
                if abs(z) >= 2:
                    s = -1 if z > 0 else 1
                    entry = o[i + 1]
                    trade = (i + 1, s, entry, entry - s * 1.5 * sd, vwap[i])
                    break
        elif setup in ("12h follow", "12h fade"):
            i = 47                                                  # the 11:45–12:00 bar
            move = c[i] - o[0]
            rng_so_far = h[: i + 1].max() - l[: i + 1].min()
            if rng_so_far > 0 and abs(move) > 0.5 * rng_so_far:
                s = 1 if move > 0 else -1
                entry = o[i + 1]
                if setup == "12h follow":
                    trade = (i + 1, s, entry, (l[: i + 1].min() if s > 0 else h[: i + 1].max()), None)
                else:
                    s = -s
                    trade = (i + 1, s, entry, entry - s * abs(move), o[0])
        if trade is None:
            continue
        i0, s, entry, stop, target = trade
        if rng is not None:                                          # luck: same entry and stop distance, random side
            dist = abs(entry - stop)
            s = rng.choice((1, -1))
            stop, target = entry - s * dist, (entry + s * abs(target - entry) if target is not None else None)
        risk = abs(entry - stop) / entry
        if risk <= 0 or (target is not None and (target - entry) * s <= 0):
            continue
        px, fee_out = _walk(o, h, l, c, i0, n, s, entry, stop, target)
        r = ((px - entry) * s / entry - TAKER - fee_out) / risk
        out.append({"ts": g.index[i0], "r": r, "risk_pct": risk * 100, "cost_r": (TAKER + fee_out) / risk})
    return out


def _ci(v: np.ndarray, seed=1, block=10, boot=2000) -> tuple[float, float]:
    rng, n, means = random.Random(seed), len(v), []
    for _ in range(boot):
        idx: list[int] = []
        while len(idx) < n:
            st = rng.randrange(0, max(n - block, 1))
            idx += range(st, st + block)
        means.append(v[idx[:n]].mean())
    means.sort()
    return means[int(boot * .025)], means[int(boot * .975)]


def summarize(trades: list[dict]) -> dict:
    df = pd.DataFrame(trades)
    daily = df.assign(eq=df["r"] * RISK).groupby(df["ts"].dt.floor("D"))["eq"].sum()
    days = pd.date_range(daily.index.min(), daily.index.max(), freq="D", tz="UTC")
    daily = daily.reindex(days, fill_value=0.0)
    split = int(len(daily) * 0.6)
    lo, hi = _ci(daily.values)
    return {"trades": int(len(df)), "per_day": round(len(df) / len(daily), 1), "avg_r": round(float(df["r"].mean()), 3),
            "win_pct": round(float((df["r"] > 0).mean() * 100), 1), "median_stop_pct": round(float(df["risk_pct"].median()), 2),
            "median_cost_r": round(float(df["cost_r"].median()), 3), "day_pct": round(float(daily.mean() * 100), 3),
            "first_pct": round(float(daily.iloc[:split].mean() * 100), 3), "last_pct": round(float(daily.iloc[split:].mean() * 100), 3),
            "ci95_pct": [round(lo * 100, 3), round(hi * 100, 3)], "positive_days_pct": round(float((daily > 0).mean() * 100), 1),
            "passes": bool(lo > 0)}


def main() -> dict:
    kl = pd.read_pickle(KLINES)
    res = {}
    for setup in ("ORB", "ORB fade", "VWAP fade", "12h follow", "12h fade"):
        trades = [t for sym, df in kl.items() for t in day_trades(df, setup)]
        res[setup] = summarize(trades)
        luck = [t for sym, df in kl.items() for t in day_trades(df, setup, random.Random(hash(sym) % 1000))]
        res[setup]["luck_avg_r"] = round(float(np.mean([t["r"] for t in luck])), 3)
    json.dump(res, open(OUT, "w"), indent=1)
    return res


if __name__ == "__main__":
    for k, v in main().items():
        print(f"{k:11s} {v['trades']:5d} trades ({v['per_day']}/day) avg {v['avg_r']:+.3f}R (luck {v['luck_avg_r']:+.3f}) win {v['win_pct']}% "
              f"stop {v['median_stop_pct']}% cost {v['median_cost_r']}R | {v['day_pct']:+.3f}%/day (first {v['first_pct']:+.3f}, last {v['last_pct']:+.3f}) "
              f"ci95 {v['ci95_pct']} +days {v['positive_days_pct']}% {'PASS' if v['passes'] else ''}")
