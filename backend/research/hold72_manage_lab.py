"""Hold-72 management — a 24h cap, a viability watcher, and a stability filter at entry.

Run: venv/bin/python -m research.hold72_manage_lab   (setup lab's cached year of scans + 15m candles;
writes research_cache/hold72_manage_results.json)

Asked 2026-10-23: hold-complete losses are big and slow, so (1) never hold past 24h, (2) keep watching
each pair and exit cleanly once it's no longer viable, (3) don't open unstable pairs at all.
Replays the book's own rule (|conviction| ≥ 0.45, coin vs BTC, 12% disaster stop) over the year,
bar by bar on 15m closes, every variant on IDENTICAL entries:

  hold        72h (live) / 48h / 24h
  watcher     at each later 4-hourly scan of the coin, close the pair if its conviction in the pair's
              direction has fallen below X (0.10 / 0.25) — or flipped against it (< 0)
  stability   skip the entry unless the previous scan (4h earlier) was already ≥ 0.45 the same way
              ("persistent"), or skip the most volatile third of pairs ("calm")
Equity % per pair at the live size (10% legs), 4 taker fills, each day's entries averaged, first 60% vs
last 40%, 95% moving-block bootstrap on the daily difference with the live rule. Filters are judged
per signal (a skipped entry is 0), so they can't win just by trading less unless the skipped trades lose.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.hold72_exits_lab import COST, exit_returns, paths
from research.hold_lab import KLINES, SCANS, _closes
from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "hold72_manage_results.json")
LEG = 0.10
BARS_PER_SCAN = 16                       # 4h of 15m bars


def watcher_exit(P: np.ndarray, meta: pd.DataFrame, scans: pd.DataFrame, below: float, hold_bars: int) -> np.ndarray:
    """Pair return when it's closed at the first later scan whose signed conviction < `below` (else the hold/stop)."""
    conv = scans.set_index(["symbol", "ts"])["conviction"]
    base = exit_returns(P[:, :hold_bars], stop=0.12)
    out = base.copy()
    for i, r in enumerate(meta.itertuples()):
        s = 1 if r.dir == "long" else -1
        # where the 12% stop (if any) fired, so the watcher can't act after it
        hit = np.where(P[i, :hold_bars] <= -0.12)[0]
        stop_bar = hit[0] if len(hit) else hold_bars
        for k in range(1, hold_bars // BARS_PER_SCAN + 1):
            bar = k * BARS_PER_SCAN - 1
            if bar >= stop_bar:
                break
            c = conv.get((r.symbol, r.ts + pd.Timedelta(hours=4 * k)))
            if c is not None and c * s < below:
                out[i] = P[i, bar]
                break
    return out


def _ci(v: np.ndarray, seed=6, block=10, boot=2000) -> tuple[float, float]:
    rng, n, means = random.Random(seed), len(v), []
    for _ in range(boot):
        idx: list[int] = []
        while len(idx) < n:
            st = rng.randrange(0, n - block)
            idx += range(st, st + block)
        means.append(v[idx[:n]].mean())
    means.sort()
    return means[int(boot * .025)], means[int(boot * .975)]


def main() -> dict:
    scans, kl = pd.read_pickle(SCANS), pd.read_pickle(KLINES)
    meta, P, vol = paths(scans, _closes(kl))
    conv = scans.set_index(["symbol", "ts"])["conviction"]
    prev = np.array([conv.get((r.symbol, r.ts - pd.Timedelta(hours=4)), np.nan) for r in meta.itertuples()])
    s = np.where(meta["dir"] == "long", 1.0, -1.0)
    persistent = np.nan_to_num(prev * s) >= 0.45
    calm = vol <= np.quantile(vol, 2 / 3)
    r72 = exit_returns(P, stop=0.12)
    variants = {
        "live: 72h": r72,
        "48h": exit_returns(P[:, :192], stop=0.12),
        "24h": exit_returns(P[:, :96], stop=0.12),
        "72h + watcher < 0.25": watcher_exit(P, meta, scans, 0.25, 288),
        "72h + watcher < 0.10": watcher_exit(P, meta, scans, 0.10, 288),
        "72h + watcher flipped (< 0)": watcher_exit(P, meta, scans, 0.0, 288),
        "24h + watcher < 0.10": watcher_exit(P, meta, scans, 0.10, 96),
        "72h, persistent entries only": np.where(persistent, r72, np.nan),
        "72h, calm entries only": np.where(calm, r72, np.nan),
        "24h, persistent entries only": np.where(persistent, exit_returns(P[:, :96], stop=0.12), np.nan),
    }
    day = meta["ts"].dt.floor("D").values
    base = pd.Series((r72 - COST) * LEG * 100).groupby(day).mean()
    res = {}
    for k, r in variants.items():
        taken = ~np.isnan(r)
        eq = np.where(taken, (np.nan_to_num(r) - COST) * LEG * 100, 0.0)          # skipped entries count 0
        daily = pd.Series(eq).groupby(day).mean()
        diff = (daily - base).values
        split = int(len(daily) * 0.6)
        lo, hi = _ci(diff) if k != "live: 72h" else (0.0, 0.0)
        res[k] = {"taken_pct": round(float(taken.mean() * 100), 1),
                  "eq_pct_per_signal": round(float(eq.mean()), 3),
                  "eq_pct_per_taken": round(float(eq[taken].mean()), 3),
                  "avg_loss_taken": round(float(eq[taken & (eq < 0)].mean()), 3),
                  "worst_1pct": round(float(np.percentile(eq[taken], 1)), 2),
                  "per_day": round(float(daily.mean()), 3),
                  "vs_live": round(float(diff.mean()), 3), "vs_live_first": round(float(diff[:split].mean()), 3),
                  "vs_live_last": round(float(diff[split:].mean()), 3), "vs_live_ci95": [round(lo, 3), round(hi, 3)]}
    out = {"entries": int(len(meta)), "days": int(pd.Series(day).nunique()), "variants": res}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = main()
    print(f"{r['entries']} entries over {r['days']} days (equity % at 10% legs)")
    for k, v in r["variants"].items():
        print(f"  {k:32s} taken {v['taken_pct']:5.1f}% per signal {v['eq_pct_per_signal']:+.3f} per trade {v['eq_pct_per_taken']:+.3f} "
              f"avg loss {v['avg_loss_taken']:+.3f} worst1% {v['worst_1pct']:+.2f} | vs live {v['vs_live']:+.3f}/day "
              f"(first {v['vs_live_first']:+.3f}, last {v['vs_live_last']:+.3f}) ci95 {v['vs_live_ci95']}")
