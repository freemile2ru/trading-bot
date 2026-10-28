"""Stock strategies lab — ideas built for stock perpetuals, not ported from crypto.

Our crypto system lost on Binance's stock perps (research/stock_setup_lab.py). These ideas use what
is different about them instead — they trade while the stock exchange is shut, and retail crowds the
popular names (it shows up as funding: longs pay ~5%/yr typically, 14–20% on NVDA or SK Hynix):

  carry      short the k stock perps with the highest trailing funding, long SPY's perp with the same
             notional; rebalance weekly. Collects what crowded longs pay, hedged against the market.
  closed     the perp's move while the exchange was shut (prior close → today's open; Monday includes
             the weekend) vs the session that follows: trade the session AGAINST the closed-hours move
             (overshoot fades) or WITH it (it keeps going); exit at the close
  intraday   intraday momentum: the first 30 minutes of the session (plus the closed-hours move)
             predicts the last 30 minutes — enter 30 min before the close in that direction
  trend      the Hold-72 idea for stocks: daily, long a stock while its 20-day trend is up (or short
             while down), hold until the trend flips, only a wide stop — equal volatility per stock

Data: each US contract's own 15-minute candles since it listed (shared with the stock setup lab),
sessions 13:30–20:00 UTC (DST ignored — at most an hour off around the switch), Asia excluded (too
few names). Costs: 0.08% per side (taker + slippage) and each contract's measured funding for the
hours held. Daily returns; pre-registered verdict as everywhere else: train = first 60% of days,
test = last 40%, and the test half's 95% block-bootstrap interval of the mean daily return must sit
above zero. Six months of data is little — a pass would only earn a paper book, not money.

    ./venv/bin/python -m research.stock_strategies_lab
"""
from __future__ import annotations

import json
import math
import os
import random
import time

import httpx
import numpy as np
import pandas as pd

from research.stock_setup_lab import ASIA_HK, ASIA_KR, KLINES
from research.swing_lab import CACHE_DIR

RESULTS = os.path.join(CACHE_DIR, "stock_strategies_results.json")
COST = 0.0008
OPEN_UTC, CLOSE_UTC = pd.Timedelta(hours=13, minutes=30), pd.Timedelta(hours=20)
BLOCK, SIMS, TRAIN_FRAC = 5, 2000, 0.6
LEVERAGED = {"SOXL", "SOXS", "TQQQ", "SQQQ", "UVXY", "TMF"}


def _funding_rates(syms: list[str]) -> dict[str, pd.Series]:
    out = {}
    for s in syms:
        rows = httpx.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": s, "limit": 1000}, timeout=20).json()
        if isinstance(rows, list) and rows:
            out[s] = pd.Series([float(r["fundingRate"]) for r in rows],
                               index=pd.to_datetime([r["fundingTime"] for r in rows], unit="ms", utc=True))
    return out


def _session_prices(k: pd.DataFrame) -> pd.DataFrame:
    """Per weekday: perp price at the open, +30 min, close −30 min, and the close (UTC)."""
    c = k.close
    days = pd.Series(k.index.normalize().unique())
    days = days[days.dt.weekday < 5]
    rows = []
    for d in days:
        pts = {}
        for name, off in (("open", OPEN_UTC), ("open30", OPEN_UTC + pd.Timedelta(minutes=30)),
                          ("close30", CLOSE_UTC - pd.Timedelta(minutes=30)), ("close", CLOSE_UTC)):
            t = d + off
            pts[name] = c.asof(t) if t >= c.index[0] else np.nan
        rows.append({"day": d, **pts})
    return pd.DataFrame(rows).set_index("day").dropna()


def daily_stats(r: pd.Series, rng: random.Random) -> dict:
    r = r.dropna()
    if len(r) < 20:
        return {"days": len(r)}
    vals, n = r.values, len(r)
    boot = sorted(vals[np.concatenate([np.arange(s, s + BLOCK) for s in (rng.randrange(n - BLOCK + 1)
                                                                         for _ in range(math.ceil(n / BLOCK)))])[:n]].mean()
                  for _ in range(SIMS))
    m, sd = r.mean(), r.std()
    return {"days": n, "mean_bps": round(m * 1e4, 2), "sharpe": round(m / sd * math.sqrt(252), 2) if sd else None,
            "ci_bps": [round(boot[int(.025 * SIMS)] * 1e4, 2), round(boot[int(.975 * SIMS)] * 1e4, 2)],
            "total_pct": round(((1 + r).prod() - 1) * 100, 1), "hit_pct": round((r > 0).mean() * 100, 1)}


def judge(r: pd.Series, rng: random.Random) -> dict:
    r = r.dropna().sort_index()
    cut = r.index[int(len(r) * TRAIN_FRAC)] if len(r) else None
    tr, te = daily_stats(r[r.index < cut], rng), daily_stats(r[r.index >= cut], rng)
    ci = te.get("ci_bps")
    verdict = "insufficient data" if not ci else "PASS" if ci[0] > 0 else "loses" if ci[1] < 0 else "no edge"
    return {"train": tr, "test": te, "all": daily_stats(r, rng), "verdict": verdict}


# ── Strategies ─────────────────────────────────────────────────────────────────

def carry(px: pd.DataFrame, fund: dict[str, pd.Series], k: int, lookback_d: int) -> pd.Series:
    """Daily P&L (fraction of the short book) of short top-k funding / long SPY, weekly rebalance."""
    daily = px.resample("1D").last().ffill()
    rets = daily.pct_change()
    fday = pd.DataFrame({s: f.resample("1D").sum() for s, f in fund.items()}).reindex(daily.index).fillna(0.0)
    trail = fday.rolling(lookback_d).mean()
    names = [c for c in daily.columns if c != "SPYUSDT"]
    out, held = [], []
    for i, d in enumerate(daily.index[lookback_d:], start=lookback_d):
        if (i - lookback_d) % 7 == 0:
            row = trail.loc[d, names].dropna()
            row = row[daily.loc[d, row.index].notna()]
            new = list(row.sort_values().index[-k:]) if len(row) >= 2 * k else []
            turnover = len(set(new) ^ set(held)) / max(k, 1) + (0 if held else 1)
            held = new
        else:
            turnover = 0.0
        if not held or i + 1 >= len(daily):
            continue
        nxt = daily.index[i + 1]
        short = -rets.loc[nxt, held].mean() + fday.loc[nxt, held].mean()     # shorts receive funding
        hedge = rets.loc[nxt, "SPYUSDT"] - fday.loc[nxt, "SPYUSDT"]           # the SPY long pays it
        out.append((nxt, (short + hedge) / 2 - turnover * COST))
    return pd.Series(dict(out))


def closed_hours(sess: dict[str, pd.DataFrame], fade: bool, min_move: float) -> pd.Series:
    """Session trade against (fade) or with the perp's move while the exchange was shut."""
    per_day: dict = {}
    for s, d in sess.items():
        prev_close = d.close.shift()
        moved = d.open / prev_close - 1
        sess_ret = d.close / d.open - 1
        sign = -np.sign(moved) if fade else np.sign(moved)
        pnl = (sign * sess_ret - 2 * COST).where(moved.abs() >= min_move)
        for day, v in pnl.dropna().items():
            per_day.setdefault(day, []).append(v)
    return pd.Series({d: np.mean(v) for d, v in per_day.items()})


def intraday(sess: dict[str, pd.DataFrame], include_overnight: bool, only_index: bool) -> pd.Series:
    per_day: dict = {}
    for s, d in sess.items():
        if only_index and s not in ("SPYUSDT", "QQQUSDT", "IWMUSDT"):
            continue
        first = d.open30 / d.open - 1
        if include_overnight:
            first = d.open30 / d.close.shift() - 1
        last = d.close / d.close30 - 1
        pnl = np.sign(first) * last - 2 * COST
        for day, v in pnl.dropna().items():
            per_day.setdefault(day, []).append(v)
    return pd.Series({d: np.mean(v) for d, v in per_day.items()})


def trend(px: pd.DataFrame, fund_bar: dict[str, float], lookback: int, mode: str) -> pd.Series:
    daily = px.resample("1D").last().ffill()
    sig = np.sign(daily.pct_change(lookback))
    if mode == "long_flat":
        sig = sig.clip(lower=0)
    vol = daily.pct_change().rolling(20).std()
    w = (sig * (0.01 / vol).clip(upper=1.0)).div(daily.notna().sum(axis=1).clip(lower=1), axis=0).fillna(0.0)
    rets = daily.pct_change().shift(-1)
    fund_day = pd.Series({s: fund_bar.get(s, 0.0) * 96 for s in daily.columns})
    gross = (w * rets).sum(axis=1)
    cost = w.diff().abs().sum(axis=1) * COST + (w * fund_day).sum(axis=1)
    return (gross - cost).iloc[lookback:-1]


def main() -> dict:
    kl = pd.read_pickle(KLINES)
    us = {s: k for s, k in kl.items() if s not in ASIA_HK | ASIA_KR and s.removesuffix("USDT") not in LEVERAGED}
    px = pd.DataFrame({s: k.close for s, k in us.items()})
    print(f"{len(us)} US stock perps, {px.index[0].date()} → {px.index[-1].date()}", flush=True)
    fund = _funding_rates(list(us))
    fund_bar = {s: (f.iloc[1:].sum() / ((f.index[-1] - f.index[0]).total_seconds() / 900)) if len(f) > 2 else 0.0
                for s, f in fund.items()}
    sess = {s: _session_prices(k) for s, k in us.items()}
    rng = random.Random(4)
    tests = {}
    for k_ in (3, 5, 10):
        for lb in (3, 7, 14):
            tests[f"carry: short top {k_} funding ({lb}d avg) / long SPY, weekly"] = carry(px, fund, k_, lb)
    for fade in (True, False):
        for mm in (0.0, 0.01, 0.02):
            tests[f"closed hours: {'fade' if fade else 'follow'} the overnight/weekend move ≥ {mm:.0%}, session only"] = \
                closed_hours(sess, fade, mm)
    for ov in (False, True):
        for oi in (True, False):
            tests[f"intraday momentum: first 30 min{' + overnight' if ov else ''} → last 30 min, "
                  f"{'index ETFs' if oi else 'all stocks'}"] = intraday(sess, ov, oi)
    for lb in (10, 20, 50):
        for mode in ("long_short", "long_flat"):
            tests[f"trend: {lb}-day, {mode.replace('_', '/')}, equal volatility"] = trend(px, fund_bar, lb, mode)
    out = {"generated_at": time.time(), "stocks": len(us), "period": [str(px.index[0].date()), str(px.index[-1].date())],
           "strategies": []}
    for name, r in tests.items():
        out["strategies"].append({"name": name, **judge(r, rng)})
    json.dump(out, open(RESULTS, "w"), indent=1, default=str)
    pd.set_option("display.width", 250)
    rows = [{"strategy": s["name"], "verdict": s["verdict"], "days": s["all"].get("days"),
             "train bps/day": s["train"].get("mean_bps"), "test bps/day": s["test"].get("mean_bps"),
             "test 95% CI": s["test"].get("ci_bps"), "test Sharpe": s["test"].get("sharpe"),
             "total %": s["all"].get("total_pct")} for s in out["strategies"]]
    print(pd.DataFrame(rows).to_string(index=False))
    return out


if __name__ == "__main__":
    main()
