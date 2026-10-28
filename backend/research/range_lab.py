"""Range trading — buy the bottom of a consolidation, sell the top, aim for the middle (a year of 1h bars).

Run: venv/bin/python -m research.range_lab   (cached 15m candles → 1h; writes research_cache/range_lab_results.json)

Consolidation (causal, at each 1h close): the coin's 3-day high-low width is below its 30-day median width
(RANGE_X) and BTC's 24h range is at or below its usual (the "normal/quiet" market the dashboard shows).
  entry    close in the bottom ZONE of the 3-day range with the bar closing up (a turn) → long at the close with a
           maker limit (2 bps); mirror for a short at the top. Stop STOP_X of the range width beyond the range edge;
           target the range middle (maker). One trade per coin at a time, 24h max hold.
  filters  "+ADX<20" adds a low-trend filter (14-bar ADX on 1h); "+RSI" needs RSI(14) < 35 / > 65 at the edge.
  luck     the same entries with a random direction.
Results in R after costs; daily sums at 0.5% risk; first 60% / last 40%; 95% block bootstrap.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "range_lab_results.json")
KLINES = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
MAKER, TAKER, RISK = 0.0002, 0.0008, 0.005
RANGE_X, ZONE, STOP_X, HOLD = 0.8, 0.2, 0.25, 24


def _adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0); minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff(); g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(); l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def run(h1: pd.DataFrame, btc_quiet: pd.Series, adx_filter=False, rsi_filter=False, rng=None) -> list[dict]:
    hi3, lo3 = h1["high"].rolling(72).max().shift(1), h1["low"].rolling(72).min().shift(1)
    width = (hi3 - lo3) / h1["close"]
    consol = width < RANGE_X * width.rolling(720, min_periods=240).median()
    quiet = btc_quiet.reindex(h1.index).ffill().fillna(False)
    adx, rsi = _adx(h1), _rsi(h1["close"])
    o, h, l, c = (h1[k].values for k in ("open", "high", "low", "close"))
    out, i, n = [], 720, len(h1)
    while i < n - HOLD - 1:
        ok = consol.iloc[i] and quiet.iloc[i] and not np.isnan(hi3.iloc[i])
        if ok and adx_filter:
            ok = adx.iloc[i] < 20
        if not ok:
            i += 1; continue
        top, bot = hi3.iloc[i], lo3.iloc[i]; w = top - bot
        pos = (c[i] - bot) / w if w > 0 else 0.5
        s = 0
        if pos <= ZONE and c[i] > c[i - 1] and (not rsi_filter or rsi.iloc[i] < 35): s = 1
        elif pos >= 1 - ZONE and c[i] < c[i - 1] and (not rsi_filter or rsi.iloc[i] > 65): s = -1
        if not s:
            i += 1; continue
        if rng is not None:
            s = rng.choice((1, -1))
        entry = c[i]                                                   # maker limit at the close
        stop = (bot - STOP_X * w) if s > 0 else (top + STOP_X * w)
        target = bot + w / 2
        if (target - entry) * s <= 0:
            i += 1; continue
        risk = abs(entry - stop) / entry
        px, fee_out, j = None, TAKER, i + 1
        for j in range(i + 1, min(i + 1 + HOLD, n)):
            if (l[j] <= stop) if s > 0 else (h[j] >= stop):
                px = min(stop, o[j]) if s > 0 else max(stop, o[j]); break
            if (h[j] >= target) if s > 0 else (l[j] <= target):
                px, fee_out = target, MAKER; break
        if px is None:
            px = c[j]
        out.append({"ts": h1.index[i], "r": ((px - entry) * s / entry - MAKER - fee_out) / risk, "risk_pct": risk * 100})
        i = j + 1
    return out


def _ci(v: np.ndarray, seed=1, block=10, boot=2000):
    rng, n, means = random.Random(seed), len(v), []
    for _ in range(boot):
        ix: list[int] = []
        while len(ix) < n:
            st = rng.randrange(0, max(n - block, 1)); ix += range(st, st + block)
        means.append(v[ix[:n]].mean())
    means.sort(); return means[int(boot * .025)], means[int(boot * .975)]


def summarize(trades):
    df = pd.DataFrame(trades)
    daily = df.assign(eq=df["r"] * RISK).groupby(df["ts"].dt.floor("D"))["eq"].sum()
    days = pd.date_range(daily.index.min(), daily.index.max(), freq="D", tz="UTC"); daily = daily.reindex(days, fill_value=0.0)
    split = int(len(daily) * 0.6); lo, hi = _ci(daily.values)
    return {"trades": int(len(df)), "per_day": round(len(df) / len(daily), 2), "avg_r": round(float(df["r"].mean()), 3),
            "win_pct": round(float((df["r"] > 0).mean() * 100), 1), "median_stop_pct": round(float(df["risk_pct"].median()), 2),
            "day_pct": round(float(daily.mean() * 100), 3), "first_pct": round(float(daily.iloc[:split].mean() * 100), 3),
            "last_pct": round(float(daily.iloc[split:].mean() * 100), 3), "ci95_pct": [round(lo * 100, 3), round(hi * 100, 3)],
            "positive_days_pct": round(float((daily > 0).mean() * 100), 1), "passes": bool(lo > 0)}


def main():
    kl = pd.read_pickle(KLINES)
    h1s = {s: df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna() for s, df in kl.items()}
    b = h1s["BTCUSDT"]; rng24 = (b["high"].rolling(24).max() - b["low"].rolling(24).min()) / b["close"]
    btc_quiet = rng24 <= rng24.rolling(720, min_periods=240).median()
    res = {}
    for name, kw in (("range edges", {}), ("+ADX<20", {"adx_filter": True}), ("+RSI", {"rsi_filter": True}),
                     ("+ADX<20 +RSI", {"adx_filter": True, "rsi_filter": True})):
        trades = [t for s, h1 in h1s.items() if s != "BTCUSDT" for t in run(h1, btc_quiet, **kw)]
        res[name] = summarize(trades)
    luck = [t for s, h1 in h1s.items() if s != "BTCUSDT" for t in run(h1, btc_quiet, rng=random.Random(hash(s) % 991))]
    res["luck (random side)"] = summarize(luck)
    json.dump(res, open(OUT, "w"), indent=1)
    return res


if __name__ == "__main__":
    for k, v in main().items():
        print(f"{k:20s} {v['trades']:5d} trades ({v['per_day']}/day) avg {v['avg_r']:+.3f}R win {v['win_pct']}% stop {v['median_stop_pct']}% | "
              f"{v['day_pct']:+.3f}%/day (first {v['first_pct']:+.3f}, last {v['last_pct']:+.3f}) ci95 {v['ci95_pct']} +days {v['positive_days_pct']}% {'PASS' if v['passes'] else ''}")
