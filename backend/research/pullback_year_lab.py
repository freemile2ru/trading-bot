"""Pullback entries on the year — does resting a limit 0.5R below the signal hold up out of sample?

Run: venv/bin/python -m research.pullback_year_lab   (needs the setup lab's cached 365-day scans
and 15m candles; writes data/research_cache/pullback_year_results.json)

research/confirm_entry_lab.py found, on our own 197 live decisions, that a limit 0.5R against the
signal price (2h to fill, original stop kept) turned −0.12R per signal into +0.04R — but the gain
shrank from the older half (+0.25R) to the newer (+0.07R), it assumed a touch fills, and it was the
best of 16 variants. This is the out-of-sample check: a year of replayed scans (40 perps, every 4h),
every scanner candidate (scanner_signal), on 15m candles:

  market   enter at the scan's close, taker 8 bps + slippage each way
  pullback limit at entry ∓ x × stop distance, must TRADE THROUGH by 2 bps within W hours to fill,
           maker 2 bps in, taker 8 bps out; original stop kept (so risk is (1−x) of the distance)
Exits: the live protect policy (exits.apply_bar). Results in R of each trade's own risk, unfilled
signals 0, each day's signals averaged; 60/40 train/test; 95% moving-block bootstrap on the paired
daily difference; per quarter.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.hold_lab import KLINES, SCANS
from research.swing_lab import CACHE_DIR
from trading import exits

OUT = os.path.join(CACHE_DIR, "pullback_year_results.json")
TAKER, MAKER, SLIP, THROUGH = 0.0005, 0.0002, 0.0003, 0.0002
BARS_MAX = 24 * 4 + 8 * 4          # protect's 24h hold plus the longest fill window


def _frames(kl: dict) -> dict[str, pd.DataFrame]:
    out = {}
    for sym, df in kl.items():
        d = df.copy()
        if "open_time" in d:
            d.index = pd.to_datetime(d["open_time"], unit="ms", utc=True)
        d.index = pd.to_datetime(d.index, utc=True)
        out[sym] = d[["open", "high", "low", "close"]].astype(float).sort_index()
    return out


def simulate(bars: np.ndarray, t0: float, s: int, sig: float, sl_pct: float, x: float, w_bars: int) -> float | None:
    """R of one signal (0 if the limit never fills). bars: rows of (end_ts, o, h, l, c) from the signal on."""
    dist = sig * sl_pct / 100
    stop = sig - s * dist
    i0, entry = 0, sig * (1 + s * SLIP)
    fee_in = TAKER
    if x > 0:
        limit = sig - s * x * dist
        entry = None
        for i in range(min(w_bars, len(bars))):
            _, o, h, l, c = bars[i]
            if (l <= limit * (1 - THROUGH)) if s > 0 else (h >= limit * (1 + THROUGH)):
                entry = min(limit, o) if s > 0 else max(limit, o)
                i0 = i + 1
                break
        if entry is None:
            return 0.0
        if (entry - stop) * s <= 0:
            return 0.0
        fee_in = MAKER
    r_unit = abs(entry - stop)
    sim = {"id": "x", "symbol": "X", "direction": "long" if s > 0 else "short", "entry_price": entry, "stop_loss": stop,
           "take_profit_1": entry + s * 50 * r_unit, "take_profit_2": entry + s * 100 * r_unit, "quantity": 1.0, "timeframe": "1h"}
    exits.stamp(sim, "protect", now=bars[i0 - 1][0] if i0 else t0)
    for end, o, h, l, c in bars[i0:]:
        hit = exits.apply_bar(sim, o, h, l, c, end)
        if hit:
            px = hit[1] * (1 - s * SLIP)
            cost = (fee_in + TAKER) * entry
            return ((px - entry) * s - cost) / r_unit
    return None


def _ci(v: np.ndarray, seed=3, block=10, boot=2000) -> tuple[float, float]:
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
    scans, frames = pd.read_pickle(SCANS), _frames(pd.read_pickle(KLINES))
    sig = scans[scans["scanner_signal"].astype(bool)].copy()
    variants = {"market": (0.0, 0), **{f"pullback {x}R / {w}h": (x, w * 4) for x in (0.25, 0.5) for w in (2, 6)}}
    rows = []
    for _, r in sig.iterrows():
        f = frames.get(r["symbol"])
        if f is None:
            continue
        s = 1 if r["conviction"] > 0 else -1
        sl = r["sl_long"] if s > 0 else r["sl_short"]
        if not sl or np.isnan(sl):
            continue
        i = f.index.searchsorted(r["ts"])
        seg = f.iloc[i:i + BARS_MAX]
        if len(seg) < BARS_MAX or i == 0:
            continue
        price = float(f["close"].iloc[i - 1])
        bars = np.column_stack([(seg.index + pd.Timedelta(minutes=15)).map(pd.Timestamp.timestamp).values,
                                seg[["open", "high", "low", "close"]].values])
        t0 = r["ts"].timestamp()
        out = {"ts": r["ts"], "dir": "long" if s > 0 else "short"}
        for k, (x, w) in variants.items():
            out[k] = simulate(bars, t0, s, price, sl, x, w)
        if None not in out.values():
            rows.append(out)
    df = pd.DataFrame(rows)
    day = df["ts"].dt.floor("D")
    split = sorted(day.unique())[int(day.nunique() * 0.6)]
    res = {}
    for k in variants:
        daily = df.groupby(day)[k].mean()
        diff = (df[k] - df["market"]).groupby(day).mean()
        lo, hi = _ci(diff.values) if k != "market" else (0.0, 0.0)
        filled = df[k] != 0.0
        q = df.groupby(df["ts"].dt.tz_localize(None).dt.to_period("Q"))[k].mean()
        res[k] = {"per_signal_r": round(float(df[k].mean()), 3), "fill_pct": round(float(filled.mean() * 100), 1),
                  "per_trade_r": round(float(df.loc[filled, k].mean()), 3), "win_pct": round(float((df.loc[filled, k] > 0).mean() * 100), 1),
                  "train_day": round(float(daily[daily.index < split].mean()), 3), "test_day": round(float(daily[daily.index >= split].mean()), 3),
                  "vs_market_day": round(float(diff.mean()), 3), "vs_market_train": round(float(diff[diff.index < split].mean()), 3),
                  "vs_market_test": round(float(diff[diff.index >= split].mean()), 3), "vs_market_ci95": [round(lo, 3), round(hi, 3)],
                  "quarters": {str(p): round(float(v), 3) for p, v in q.items()},
                  "by_side": {d: round(float(df.loc[df["dir"] == d, k].mean()), 3) for d in ("long", "short")}}
    out = {"signals": int(len(df)), "days": int(day.nunique()), "period": [str(df["ts"].min().date()), str(df["ts"].max().date())],
           "variants": res}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = main()
    print(f"{r['signals']} scanner candidates over {r['days']} days {r['period']}")
    for k, v in r["variants"].items():
        print(f"  {k:22s} per signal {v['per_signal_r']:+.3f}R fill {v['fill_pct']:5.1f}% per trade {v['per_trade_r']:+.3f} win {v['win_pct']}% | "
              f"train {v['train_day']:+.3f} test {v['test_day']:+.3f} | vs market {v['vs_market_day']:+.3f} "
              f"(tr {v['vs_market_train']:+.3f} te {v['vs_market_test']:+.3f}) ci95 {v['vs_market_ci95']} | {v['by_side']}")
        print(f"      quarters: {v['quarters']}")
