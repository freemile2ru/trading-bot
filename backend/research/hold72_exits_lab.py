"""Hold-72 exits — can the disaster stop and the 72h close lose less without killing the edge?

Run: venv/bin/python -m research.hold72_exits_lab   (needs the setup lab's cached 365-day scans and
15m candles; writes data/research_cache/hold72_exits_results.json)

Live (2026-10-07…19), 6 disaster stops at −12% of a 15%-of-equity leg cost ~$190 each — nearly all
the book's loss — and some pairs were well up before closing at 72h for a loss (STRK +32.9% → −7.5%).
This replays the book's own rule (|conviction| ≥ 0.45, coin vs BTC, 72h) over the year of scans,
bar by bar on 15m closes, under different exits, with every variant seeing IDENTICAL entries:

  stops        fixed −6 / −8 / −10 / −12 (live) / −15 / −20%, none
  vol stop     k × the pair's own 72h volatility (measured on the 3 days BEFORE entry), with the
               leg sized so a stop-out costs the same 1.8% of equity as the live book's
  protection   breakeven once +X%, trailing Y% behind the best, a take-profit
Exits fill at the 15m close that crosses the level (so gaps through the stop are paid in full).
Costs: 4 taker fills × 8 bps. Funding is left out (both legs pay/receive; small over 72h).

Judged in % of EQUITY per pair (leg size × pair return), each day's entries averaged into one
day (the 4-hourly entries overlap), paired against the live rule: train (first 60%) vs test,
and a 95% moving-block bootstrap on the daily difference.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.hold_lab import KLINES, SCANS, _closes
from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "hold72_exits_results.json")
MIN_CONV = 0.45
BARS = 72 * 4                     # 15m bars in 72h
COST = 4 * 8 / 1e4                # four taker fills
LEG = 0.15                        # live leg size, fraction of equity
RISK = LEG * 0.12                 # equity lost at the live disaster stop
TRAIN = 0.6
BLOCK, BOOT = 10, 2000


def paths(scans: pd.DataFrame, closes: dict[str, pd.Series]) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """Each qualifying entry's pair-return path over 72h (fractions), and its pre-entry 72h vol."""
    btc = closes["BTCUSDT"]
    grid = btc.index
    sel = scans[(scans["conviction"].abs() >= MIN_CONV) & (scans["symbol"] != "BTCUSDT")].copy()
    rows, mats, vols = [], [], []
    for sym, g in sel.groupby("symbol"):
        c = closes.get(sym)
        if c is None:
            continue
        c = c.reindex(grid, method="ffill")
        lc, lb = np.log(c.values), np.log(btc.values)
        for idx, r in g.iterrows():
            i = grid.searchsorted(r["ts"])
            if i < 3 * 96 or i + BARS >= len(grid) or np.isnan(lc[i]):
                continue
            s = 1.0 if r["conviction"] > 0 else -1.0
            seg_c, seg_b = c.values[i:i + BARS + 1], btc.values[i:i + BARS + 1]
            path = s * (seg_c[1:] / seg_c[0] - 1) - s * (seg_b[1:] / seg_b[0] - 1)
            if np.isnan(path).any():
                continue
            pre = np.diff(lc[i - 3 * 96:i + 1] - lb[i - 3 * 96:i + 1])
            vols.append(np.nanstd(pre) * np.sqrt(BARS))
            mats.append(path)
            rows.append({"ts": r["ts"], "symbol": sym, "dir": "long" if s > 0 else "short", "conv": r["conviction"]})
    return pd.DataFrame(rows), np.array(mats), np.array(vols)


def exit_returns(P: np.ndarray, stop=None, be_at=None, trail=None, trail_from=None, tp=None) -> np.ndarray:
    """Pair return (before costs) for each path under an exit rule. Levels are fractions per path or scalars."""
    n, T = P.shape
    out = P[:, -1].copy()
    done = np.zeros(n, bool)
    best = np.full(n, -np.inf)
    floor = np.full(n, -np.inf) if stop is None else -np.broadcast_to(np.asarray(stop, float), (n,)).copy()
    for t in range(T):
        x = P[:, t]
        live = ~done
        hit = live & (x <= floor)
        out[hit] = x[hit]
        done |= hit
        if tp is not None:
            h = ~done & (x >= tp)
            out[h] = x[h]
            done |= h
        best = np.maximum(best, x)
        if be_at is not None:
            floor = np.where(~done & (best >= be_at), np.maximum(floor, 0.0), floor)
        if trail is not None:
            start = trail_from if trail_from is not None else 0.0
            floor = np.where(~done & (best >= start), np.maximum(floor, best - trail), floor)
    return out


def _block_ci(v: np.ndarray, seed=5) -> tuple[float, float]:
    rng, n = random.Random(seed), len(v)
    means = []
    for _ in range(BOOT):
        idx: list[int] = []
        while len(idx) < n:
            s = rng.randrange(0, n - BLOCK)
            idx += range(s, s + BLOCK)
        means.append(v[idx[:n]].mean())
    means.sort()
    return means[int(BOOT * .025)], means[int(BOOT * .975)]


def main() -> dict:
    scans, kl = pd.read_pickle(SCANS), pd.read_pickle(KLINES)
    meta, P, vol = paths(scans, _closes(kl))
    day = meta["ts"].dt.floor("D")
    k_vol = lambda k: np.clip(k * vol, 0.04, 0.30)
    variants = {
        "live: stop 12%": dict(r=exit_returns(P, stop=0.12), leg=LEG),
        "no stop": dict(r=exit_returns(P), leg=LEG),
        "stop 6%": dict(r=exit_returns(P, stop=0.06), leg=LEG),
        "stop 8%": dict(r=exit_returns(P, stop=0.08), leg=LEG),
        "stop 10%": dict(r=exit_returns(P, stop=0.10), leg=LEG),
        "stop 15%": dict(r=exit_returns(P, stop=0.15), leg=LEG),
        "stop 20%": dict(r=exit_returns(P, stop=0.20), leg=LEG),
        # Same 1.8%-of-equity loss at the stop as live, but the stop sits at k× the pair's own volatility.
        **{f"vol stop {k}σ, risk-sized": dict(r=exit_returns(P, stop=k_vol(k)), leg=np.clip(RISK / k_vol(k), 0.03, 0.30))
           for k in (1.5, 2.0, 2.5)},
        "stop 12% + breakeven at +5%": dict(r=exit_returns(P, stop=0.12, be_at=0.05), leg=LEG),
        "stop 12% + breakeven at +8%": dict(r=exit_returns(P, stop=0.12, be_at=0.08), leg=LEG),
        "stop 12% + trail 6% from +6%": dict(r=exit_returns(P, stop=0.12, trail=0.06, trail_from=0.06), leg=LEG),
        "stop 12% + trail 10% from +10%": dict(r=exit_returns(P, stop=0.12, trail=0.10, trail_from=0.10), leg=LEG),
        "stop 12% + take profit 15%": dict(r=exit_returns(P, stop=0.12, tp=0.15), leg=LEG),
        "vol stop 2σ risk-sized + trail 10% from +10%": dict(
            r=exit_returns(P, stop=k_vol(2.0), trail=0.10, trail_from=0.10), leg=np.clip(RISK / k_vol(2.0), 0.03, 0.30)),
    }
    base = None
    res = {}
    for name, v in variants.items():
        eq = (v["r"] - COST) * v["leg"] * 100                       # % of equity per pair
        daily = pd.Series(eq).groupby(day.values).mean()
        if base is None:
            base = daily
        diff = (daily - base).values
        split = int(len(daily) * TRAIN)
        lo, hi = _block_ci(diff) if name != "live: stop 12%" else (0.0, 0.0)
        dlo, dhi = _block_ci(daily.values)
        stopped = (v["r"] <= -np.broadcast_to(0.0, v["r"].shape) - 0.04).mean() if False else None
        res[name] = {
            "eq_pct_per_pair": round(float(eq.mean()), 3), "per_day": round(float(daily.mean()), 3), "ci_day": [round(dlo, 3), round(dhi, 3)],
            "train": round(float(daily.iloc[:split].mean()), 3), "test": round(float(daily.iloc[split:].mean()), 3),
            "vs_live": round(float(diff.mean()), 3), "vs_live_train": round(float(diff[:split].mean()), 3),
            "vs_live_test": round(float(diff[split:].mean()), 3), "vs_live_ci": [round(lo, 3), round(hi, 3)],
            "worst_pair_eq_pct": round(float(eq.min()), 2), "worst_1pct_eq_pct": round(float(np.percentile(eq, 1)), 2),
            "hit_pct": round(float((eq > 0).mean() * 100), 1),
            "by_side": {s: round(float(eq[(meta["dir"] == s).values].mean()), 3) for s in ("long", "short")},
        }
    out = {"entries": int(len(meta)), "days": int(day.nunique()), "period": [str(meta["ts"].min().date()), str(meta["ts"].max().date())],
           "median_72h_vol_pct": round(float(np.median(vol) * 100), 1), "variants": res}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = main()
    print(f"{r['entries']} entries over {r['days']} days {r['period']} · median pair 72h vol {r['median_72h_vol_pct']}%")
    print(f"{'variant':46s} {'eq%/pair':>8s} {'/day':>7s} {'train':>7s} {'test':>7s} | {'vs live':>7s} {'tr':>6s} {'te':>6s} {'ci95':>16s} | worst1% worst  hit")
    for k, v in r["variants"].items():
        print(f"{k:46s} {v['eq_pct_per_pair']:+8.3f} {v['per_day']:+7.3f} {v['train']:+7.3f} {v['test']:+7.3f} | "
              f"{v['vs_live']:+7.3f} {v['vs_live_train']:+6.3f} {v['vs_live_test']:+6.3f} {str(v['vs_live_ci']):>16s} | "
              f"{v['worst_1pct_eq_pct']:+6.2f} {v['worst_pair_eq_pct']:+6.2f} {v['hit_pct']:5.1f}")
