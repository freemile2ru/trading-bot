"""Quiet markets — is there a strategy that makes money when the market goes quiet?

Run: venv/bin/python -m research.quiet_market_lab   (cached 15m candles, 40 perps, a year;
writes research_cache/quiet_market_results.json)

Asked 2026-10-23: detect quiet markets and trade them, with bigger positions when the swing range is
small. The trap with that: fees are paid on NOTIONAL, risk on the stop — a tight range means a tight
stop, a big position, and costs that eat a large share of each R. So everything here is in R after
costs, sized to a fixed risk per trade.

Quiet (causal, at each 15m close): the coin's 24h high-low range is under 0.7× its median of the last
30 days, and BTC's is too.

  A  range fade     in a quiet market, a close ≥ 2 standard deviations from its 24h mean: fade it.
                    Enter next open (taker), stop 1.5 sd beyond entry, target the 24h mean (maker
                    limit), give up after 12h. One position per coin at a time.
  A' controls       the same fade in all markets, and only in non-quiet markets — does quiet matter?
  A~ luck           random direction, random quiet bars, the same stop/target geometry
  B  squeeze break  after a quiet stretch, follow a close beyond the prior 24h high/low (stop mid-range,
                    target 2× the range, 24h) — vs the same breakout at any time
  D  24h reversal   every 4h: long the 3 coins down most over 24h, short the 3 up most, hold 24h
                    (equal notional, market-neutral) — in quiet markets vs all
Costs: taker 0.05% + 0.03% slippage, maker 0.02%. Daily = sum of the day's trades at 0.5% risk each
(A) / equal-weight book return (D). First 60% vs last 40%; 95% moving-block bootstrap; luck baseline.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "quiet_market_results.json")
KLINES = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
DAY, MONTH = 96, 96 * 30
QUIET_X = 0.7
Z_IN, STOP_SD, HOLD = 2.0, 1.5, 48
TAKER, MAKER = 0.0005 + 0.0003, 0.0002
RISK = 0.005


def features(df: pd.DataFrame) -> pd.DataFrame:
    h, l, c = df["high"], df["low"], df["close"]
    rng = (h.rolling(DAY).max() - l.rolling(DAY).min()) / c
    med = rng.rolling(MONTH, min_periods=DAY * 10).median()
    mean, sd = c.rolling(DAY).mean(), c.rolling(DAY).std()
    return pd.DataFrame({"o": df["open"], "h": h, "l": l, "c": c, "rng": rng, "quiet": rng < QUIET_X * med,
                         "z": (c - mean) / sd, "mean": mean, "sd": sd, "ret24": c / c.shift(DAY) - 1})


def fade_trades(f: pd.DataFrame, btc_quiet: pd.Series, regime: str, rng_: random.Random | None = None) -> list[dict]:
    """Range-fade trades for one coin. regime: quiet / all / busy / luck."""
    o, h, l, c = f["o"].values, f["h"].values, f["l"].values, f["c"].values
    z, mean, sd = f["z"].values, f["mean"].values, f["sd"].values
    quiet = (f["quiet"] & btc_quiet.reindex(f.index).fillna(False)).values
    idx, out, i, n = f.index, [], DAY * 10, len(f)
    while i < n - HOLD - 1:
        if np.isnan(z[i]) or np.isnan(sd[i]) or sd[i] <= 0:
            i += 1
            continue
        if regime == "luck":
            fire = quiet[i] and rng_.random() < 0.01
            s = rng_.choice((1, -1))
        else:
            ok = {"quiet": quiet[i], "all": True, "busy": not quiet[i]}[regime]
            fire = ok and abs(z[i]) >= Z_IN
            s = -1 if z[i] > 0 else 1
        if not fire:
            i += 1
            continue
        entry = o[i + 1]
        stop = entry - s * STOP_SD * sd[i]
        target = mean[i] if regime != "luck" else entry + s * abs(entry - mean[i] if not np.isnan(mean[i]) else sd[i])
        risk_frac = abs(entry - stop) / entry
        if (target - entry) * s <= 0 or risk_frac <= 0:
            i += 1
            continue
        exit_px, exit_cost, j = None, TAKER, i + 1
        for j in range(i + 1, min(i + 1 + HOLD, n)):
            if (l[j] <= stop) if s > 0 else (h[j] >= stop):
                exit_px = min(stop, o[j]) if s > 0 else max(stop, o[j])
                break
            if (h[j] >= target) if s > 0 else (l[j] <= target):
                exit_px, exit_cost = target, MAKER
                break
        if exit_px is None:
            exit_px = c[j]
        cost = (TAKER + exit_cost)
        r = ((exit_px - entry) * s / entry - cost) / risk_frac
        out.append({"ts": idx[i + 1], "r": r, "risk_frac": risk_frac, "cost_r": cost / risk_frac, "win": r > 0})
        i = j + 1
    return out


def breakout_trades(f: pd.DataFrame, btc_quiet: pd.Series, need_quiet: bool, target_x: float = 2.0) -> list[dict]:
    """B  squeeze breakout: after a quiet stretch, a 15m close beyond the prior 24h high/low — follow it.
    Stop at the middle of that range, target target_x × the range beyond the break, give up after 24h."""
    o, h, l, c = f["o"].values, f["h"].values, f["l"].values, f["c"].values
    hi24 = f["h"].rolling(DAY).max().shift(1).values
    lo24 = f["l"].rolling(DAY).min().shift(1).values
    quiet = (f["quiet"].shift(1) & btc_quiet.reindex(f.index).fillna(False).shift(1)).fillna(False).values
    idx, out, i, n = f.index, [], DAY * 10, len(f)
    while i < n - 2 * HOLD - 1:
        if np.isnan(hi24[i]) or (need_quiet and not quiet[i]):
            i += 1
            continue
        s = 1 if c[i] > hi24[i] else -1 if c[i] < lo24[i] else 0
        if not s:
            i += 1
            continue
        entry, width = o[i + 1], hi24[i] - lo24[i]
        stop = (hi24[i] + lo24[i]) / 2
        target = entry + s * target_x * width
        risk_frac = abs(entry - stop) / entry
        if (entry - stop) * s <= 0 or risk_frac <= 0:
            i += 1
            continue
        exit_px, exit_cost, j = None, TAKER, i + 1
        for j in range(i + 1, min(i + 1 + 2 * HOLD, n)):
            if (l[j] <= stop) if s > 0 else (h[j] >= stop):
                exit_px = min(stop, o[j]) if s > 0 else max(stop, o[j])
                break
            if (h[j] >= target) if s > 0 else (l[j] <= target):
                exit_px, exit_cost = target, MAKER
                break
        if exit_px is None:
            exit_px = c[j]
        cost = TAKER + exit_cost
        r = ((exit_px - entry) * s / entry - cost) / risk_frac
        out.append({"ts": idx[i + 1], "r": r, "risk_frac": risk_frac, "cost_r": cost / risk_frac, "win": r > 0})
        i = j + 1
    return out


def reversal_book(feats: dict[str, pd.DataFrame], btc_quiet: pd.Series, only_quiet: bool) -> pd.Series:
    """Every 4h, long the 3 biggest 24h losers and short the 3 biggest winners for 24h; daily book return."""
    closes = pd.DataFrame({s: f["c"] for s, f in feats.items()})
    ret24 = closes / closes.shift(DAY) - 1
    fwd = closes.shift(-DAY) / closes - 1
    rows = []
    for t in closes.index[DAY * 10:-DAY:16]:
        if only_quiet and not btc_quiet.get(t, False):
            continue
        r = ret24.loc[t].dropna()
        if len(r) < 12:
            continue
        lo, hi = r.nsmallest(3).index, r.nlargest(3).index
        f = fwd.loc[t]
        gross = (f[lo].mean() - f[hi].mean()) / 2
        rows.append((t, gross - 2 * TAKER))           # in and out on both sides, half the book each
    s = pd.Series(dict(rows))
    # 6 overlapping 24h books, each 1/6 of capital: the day's book return is the mean of its cohorts' returns
    # (an earlier version divided by 6 again, understating it 6× — the sign, and the verdict, were unchanged).
    return s.groupby(s.index.floor("D")).mean()


def _ci(v: np.ndarray, seed=2, block=10, boot=2000) -> tuple[float, float]:
    rng, n, means = random.Random(seed), len(v), []
    if n <= block:
        return float("nan"), float("nan")
    for _ in range(boot):
        idx: list[int] = []
        while len(idx) < n:
            st = rng.randrange(0, n - block)
            idx += range(st, st + block)
        means.append(v[idx[:n]].mean())
    means.sort()
    return means[int(boot * .025)], means[int(boot * .975)]


def _summary(daily: pd.Series, trades: list[dict] | None = None) -> dict:
    daily = daily.sort_index()
    split = int(len(daily) * 0.6)
    lo, hi = _ci(daily.values)
    out = {"days": int(len(daily)), "mean_day_pct": round(float(daily.mean() * 100), 4),
           "positive_days_pct": round(float((daily > 0).mean() * 100), 1),
           "first_pct": round(float(daily.iloc[:split].mean() * 100), 4), "last_pct": round(float(daily.iloc[split:].mean() * 100), 4),
           "ci95_pct": [round(lo * 100, 4), round(hi * 100, 4)], "passes": bool(lo > 0)}
    if trades is not None:
        rs = np.array([t["r"] for t in trades])
        out.update({"trades": len(trades), "per_day": round(len(trades) / max(len(daily), 1), 1), "avg_r": round(float(rs.mean()), 3),
                    "win_pct": round(float((rs > 0).mean() * 100), 1),
                    "median_stop_pct": round(float(np.median([t["risk_frac"] for t in trades]) * 100), 2),
                    "median_cost_r": round(float(np.median([t["cost_r"] for t in trades])), 3)})
    return out


def main() -> dict:
    kl = pd.read_pickle(KLINES)
    feats = {s: features(df) for s, df in kl.items() if len(df) > MONTH * 2}
    btc_quiet = feats["BTCUSDT"]["quiet"]
    res = {"coins": len(feats), "quiet_share_pct": round(float(btc_quiet.mean() * 100), 1)}
    rng_ = random.Random(5)
    for regime in ("quiet", "all", "busy", "luck"):
        trades = [t for s, f in feats.items() if s != "BTCUSDT" for t in fade_trades(f, btc_quiet, regime, rng_)]
        df = pd.DataFrame(trades)
        daily = (df.assign(eq=df["r"] * RISK).groupby(df["ts"].dt.floor("D"))["eq"].sum())
        days = pd.date_range(daily.index.min(), daily.index.max(), freq="D", tz="UTC")
        res[f"A fade · {regime}"] = _summary(daily.reindex(days, fill_value=0.0), trades)
    for need_quiet in (True, False):
        trades = [t for s_, f in feats.items() if s_ != "BTCUSDT" for t in breakout_trades(f, btc_quiet, need_quiet)]
        df = pd.DataFrame(trades)
        daily = (df.assign(eq=df["r"] * RISK).groupby(df["ts"].dt.floor("D"))["eq"].sum())
        days = pd.date_range(daily.index.min(), daily.index.max(), freq="D", tz="UTC")
        res[f"B breakout · {'after quiet' if need_quiet else 'any'}"] = _summary(daily.reindex(days, fill_value=0.0), trades)
    for only_quiet in (True, False):
        res[f"D 24h reversal · {'quiet' if only_quiet else 'all'}"] = _summary(reversal_book(feats, btc_quiet, only_quiet))
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print(f"{r['coins']} coins · BTC quiet {r['quiet_share_pct']}% of the time")
    for k, v in r.items():
        if not isinstance(v, dict):
            continue
        extra = (f" trades {v['trades']} ({v['per_day']}/day) avg {v['avg_r']:+.3f}R win {v['win_pct']}% stop {v['median_stop_pct']}% "
                 f"cost {v['median_cost_r']}R |") if "trades" in v else ""
        print(f"  {k:24s}{extra} day {v['mean_day_pct']:+.4f}% (+days {v['positive_days_pct']}%) first {v['first_pct']:+.4f} "
              f"last {v['last_pct']:+.4f} ci95 {v['ci95_pct']} {'PASS' if v['passes'] else ''}")
