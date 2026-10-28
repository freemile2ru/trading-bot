"""Signal rebuild — is there information beyond recent price that predicts the next 24h, coin vs BTC?

Run: venv/bin/python -m research.signal_rebuild_lab   (fetches a year of 4h klines with taker-buy volume and
funding history for 40 perps, cached in research_cache/signal_rebuild_data.pkl; writes
research_cache/signal_rebuild_results.json)

Why (2026-10-24): the scanner's conviction behaves like a 24h momentum reading, and every book trading it
lost. Binance keeps a full history of two things that aren't price: funding rates and the taker-buy share
of volume (who's aggressing). Candidate inputs, all known at the 4h close:

  price      mom_4h, mom_24h, mom_7d (log returns), dist_hi7 / dist_lo7 (from the 7-day high / low)
  activity   vol_7d (realised), volume_z (24h volume vs its 30-day mean)
  flow       taker_24h (taker-buy share of volume − 0.5, last 24h), taker_chg (24h vs 7d)
  funding    fund_now (last settled rate), fund_7d (7-day mean), fund_chg (now − 7d mean)
Target: the next 24h log return minus BTC's. Every 4h, features and target are ranked across coins.

  1. each input's rank IC (mean of per-timestamp Spearman), first half vs second half, t-stat
  2. walk-forward model: ridge on ranked inputs, trained on the trailing 120 days, predicting the next 30;
     OOS rank IC — and the same with momentum alone (≈ today's conviction) and with shuffled targets
  3. portfolio from the model: every 4h, long the top 5 and short the bottom 5 for 24h (6 overlapping
     books), 4 taker fills (8 bps each) per name per round trip; daily net return, 95% block bootstrap
"""
from __future__ import annotations

import asyncio
import json
import os
import random

import httpx
import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

DATA = os.path.join(CACHE_DIR, "signal_rebuild_data.pkl")
OUT = os.path.join(CACHE_DIR, "signal_rebuild_results.json")
FAPI = "https://fapi.binance.com/fapi/v1"
COINS_FROM = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
DAYS = 400
FEATURES = ["mom_4h", "mom_24h", "mom_7d", "dist_hi7", "dist_lo7", "vol_7d", "volume_z", "taker_24h", "taker_chg",
            "fund_now", "fund_7d", "fund_chg"]
TRAIN_DAYS, TEST_DAYS, TOP, COST = 120, 30, 5, 0.0008


async def _get(c: httpx.AsyncClient, path: str, params: dict) -> list:
    for attempt in range(4):
        try:
            r = await c.get(f"{FAPI}/{path}", params=params)
            r.raise_for_status()
            return r.json()
        except Exception:
            await asyncio.sleep(1 + attempt)
    return []


async def fetch() -> dict:
    coins = list(pd.read_pickle(COINS_FROM))
    start = int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=DAYS)).timestamp() * 1000)
    out = {}
    async with httpx.AsyncClient(timeout=20) as c:
        for sym in coins:
            kl, t = [], start
            while True:
                chunk = await _get(c, "klines", {"symbol": sym, "interval": "4h", "startTime": t, "limit": 1500})
                if not chunk:
                    break
                kl += chunk
                if len(chunk) < 1500:
                    break
                t = chunk[-1][0] + 1
            fu, t = [], start
            while True:
                chunk = await _get(c, "fundingRate", {"symbol": sym, "startTime": t, "limit": 1000})
                if not chunk:
                    break
                fu += chunk
                if len(chunk) < 1000:
                    break
                t = chunk[-1]["fundingTime"] + 1
            if not kl:
                continue
            k = pd.DataFrame(kl, columns=["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tbqv", "x"])
            k = k.astype({"o": float, "h": float, "l": float, "c": float, "v": float, "tbv": float})
            k.index = pd.to_datetime(k["ct"] + 1, unit="ms", utc=True)          # stamped at the close
            f = pd.Series({pd.to_datetime(x["fundingTime"], unit="ms", utc=True): float(x["fundingRate"]) for x in fu}, dtype=float)
            out[sym] = {"k": k[["o", "h", "l", "c", "v", "tbv"]], "funding": f.sort_index()}
            await asyncio.sleep(0.2)
    pd.to_pickle(out, DATA)
    return out


def build(data: dict) -> pd.DataFrame:
    """Long table: (ts, symbol) → features + target, every 4h."""
    btc = data["BTCUSDT"]["k"]["c"]
    parts = []
    for sym, d in data.items():
        if sym == "BTCUSDT":
            continue
        k = d["k"]
        c, lc = k["c"], np.log(k["c"])
        f = pd.DataFrame(index=k.index)
        f["mom_4h"] = lc.diff(1)
        f["mom_24h"] = lc.diff(6)
        f["mom_7d"] = lc.diff(42)
        f["dist_hi7"] = c / k["h"].rolling(42).max() - 1
        f["dist_lo7"] = c / k["l"].rolling(42).min() - 1
        f["vol_7d"] = lc.diff().rolling(42).std()
        f["volume_z"] = k["v"].rolling(6).sum() / k["v"].rolling(180).mean() / 6 - 1
        share = (k["tbv"].rolling(6).sum() / k["v"].rolling(6).sum()) - 0.5
        f["taker_24h"] = share
        f["taker_chg"] = share - ((k["tbv"].rolling(42).sum() / k["v"].rolling(42).sum()) - 0.5)
        fu = d["funding"].reindex(k.index, method="ffill") if len(d["funding"]) else pd.Series(np.nan, index=k.index)
        f["fund_now"] = fu
        f["fund_7d"] = d["funding"].rolling(21).mean().reindex(k.index, method="ffill") if len(d["funding"]) else np.nan
        f["fund_chg"] = f["fund_now"] - f["fund_7d"]
        b = np.log(btc.reindex(k.index))
        f["target"] = (lc.shift(-6) - lc) - (b.shift(-6) - b)
        f["target7"] = (lc.shift(-42) - lc) - (b.shift(-42) - b)
        f["symbol"] = sym
        parts.append(f)
    df = pd.concat(parts).reset_index(names="ts").dropna(subset=FEATURES)
    return df


def _ranked(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return df.groupby("ts")[cols].rank(pct=True) - 0.5


def ic_table(df: pd.DataFrame) -> dict:
    d = df.dropna(subset=["target"]).copy()
    r = _ranked(d, FEATURES + ["target"])
    r["ts"] = d["ts"].values
    out = {}
    ts_sorted = sorted(r["ts"].unique())
    half = ts_sorted[len(ts_sorted) // 2]
    for f in FEATURES:
        per = r.groupby("ts").apply(lambda g: g[f].corr(g["target"]) if len(g) > 8 else np.nan).dropna()
        daily = per.groupby(per.index.floor("D")).mean()
        out[f] = {"ic": round(float(per.mean()), 4), "t_daily": round(float(daily.mean() / daily.std() * np.sqrt(len(daily))), 2),
                  "first": round(float(per[per.index < half].mean()), 4), "second": round(float(per[per.index >= half].mean()), 4)}
    return out


def walk_forward(df: pd.DataFrame, cols: list[str], shuffle: bool = False, seed: int = 0) -> pd.DataFrame:
    """Out-of-sample model scores per (ts, symbol), refit every TEST_DAYS on the trailing TRAIN_DAYS."""
    d = df.dropna(subset=["target"]).copy()
    X = _ranked(d, cols)
    y = (d.groupby("ts")["target"].rank(pct=True) - 0.5).values
    if shuffle:
        y = np.random.default_rng(seed).permutation(y)
    ts = d["ts"]
    start, end = ts.min() + pd.Timedelta(days=TRAIN_DAYS), ts.max()
    preds = []
    t0 = start
    while t0 < end:
        t1 = t0 + pd.Timedelta(days=TEST_DAYS)
        # train on targets that were fully known before the test window (24h purge)
        tr = (ts >= t0 - pd.Timedelta(days=TRAIN_DAYS)) & (ts < t0 - pd.Timedelta(hours=24))
        te = (ts >= t0) & (ts < t1)
        if tr.sum() > 500 and te.any():
            A = X[tr].values
            w = np.linalg.solve(A.T @ A + 10.0 * np.eye(A.shape[1]), A.T @ y[tr.values])
            preds.append(pd.DataFrame({"ts": ts[te].values, "symbol": d.loc[te, "symbol"].values,
                                       "score": X[te].values @ w, "target": d.loc[te, "target"].values}))
        t0 = t1
    return pd.concat(preds) if preds else pd.DataFrame()


def evaluate(p: pd.DataFrame) -> dict:
    ic = p.groupby("ts").apply(lambda g: g["score"].rank().corr(g["target"].rank()) if len(g) > 8 else np.nan).dropna()
    rets = []
    for t, g in p.groupby("ts"):
        if len(g) < 2 * TOP + 2:
            continue
        g = g.sort_values("score")
        gross = (g["target"].tail(TOP).mean() - g["target"].head(TOP).mean()) / 2      # half the book each side
        rets.append((t, gross - 2 * COST))                                            # in and out, each side
    s = pd.Series(dict(rets))
    # Six overlapping 24h books, each 1/6 of capital: a day's book return is the MEAN of the day's cohorts'
    # 24h returns (an earlier version divided by 6 again, understating every figure 6×).
    daily = s.groupby(s.index.floor("D")).mean()
    rng, n, means = random.Random(3), len(daily), []
    for _ in range(2000):
        idx: list[int] = []
        while len(idx) < n:
            st = rng.randrange(0, max(n - 10, 1))
            idx += range(st, st + 10)
        means.append(daily.values[idx[:n]].mean())
    means.sort()
    half = len(daily) // 2
    return {"oos_ic": round(float(ic.mean()), 4), "ic_t": round(float(ic.mean() / ic.std() * np.sqrt(len(ic))), 2),
            "days": int(len(daily)), "net_day_pct": round(float(daily.mean() * 100), 4),
            "first_half_pct": round(float(daily.iloc[:half].mean() * 100), 4), "second_half_pct": round(float(daily.iloc[half:].mean() * 100), 4),
            "ci95_pct": [round(means[50] * 100, 4), round(means[1949] * 100, 4)], "positive_days_pct": round(float((daily > 0).mean() * 100), 1),
            "passes": bool(means[50] > 0)}


def weekly(df: pd.DataFrame, score_col: str | None = None, preds: pd.DataFrame | None = None, sign: float = 1.0) -> dict:
    """Slow version: once a week (Mondays 00:00), long the TOP best and short the TOP worst by score, hold 7
    days. One round trip a week per name: 16 bps per side a week instead of a day."""
    d = df.dropna(subset=["target7"]).copy()
    if preds is not None:
        pr = preds[["ts", "symbol", "score"]].copy()
        pr["ts"] = pd.to_datetime(pr["ts"], utc=True)
        d = d.merge(pr, on=["ts", "symbol"])
    else:
        d["score"] = sign * d[score_col]
    d = d[(d["ts"].dt.dayofweek == 0) & (d["ts"].dt.hour == 0)]
    rets = []
    for t, g in d.groupby("ts"):
        if len(g) < 2 * TOP + 2:
            continue
        g = g.sort_values("score")
        rets.append((t, (g["target7"].tail(TOP).mean() - g["target7"].head(TOP).mean()) / 2 - 2 * COST))
    w = pd.Series(dict(rets)).sort_index()
    half = len(w) // 2
    rng, means = random.Random(4), []
    for _ in range(2000):
        means.append(np.mean(rng.choices(list(w.values), k=len(w))))
    means.sort()
    return {"weeks": int(len(w)), "net_week_pct": round(float(w.mean() * 100), 3), "first_half": round(float(w.iloc[:half].mean() * 100), 3),
            "second_half": round(float(w.iloc[half:].mean() * 100), 3), "positive_weeks_pct": round(float((w > 0).mean() * 100), 1),
            "ci95_pct": [round(means[50] * 100, 3), round(means[1949] * 100, 3)], "passes": bool(means[50] > 0)}


def main(refetch: bool = False) -> dict:
    data = pd.read_pickle(DATA) if os.path.exists(DATA) and not refetch else asyncio.run(fetch())
    df = build(data)
    res = {"rows": int(len(df)), "coins": int(df["symbol"].nunique()),
           "period": [str(df["ts"].min().date()), str(df["ts"].max().date())], "ic": ic_table(df)}
    res["model_all"] = evaluate(walk_forward(df, FEATURES))
    res["model_no_price"] = evaluate(walk_forward(df, ["vol_7d", "volume_z", "taker_24h", "taker_chg", "fund_now", "fund_7d", "fund_chg"]))
    res["momentum_only"] = evaluate(walk_forward(df, ["mom_24h"]))
    res["luck"] = [evaluate(walk_forward(df, FEATURES, shuffle=True, seed=s))["net_day_pct"] for s in range(5)]
    res["weekly_low_vol"] = weekly(df, "vol_7d", sign=-1.0)                 # long calm coins, short wild ones
    res["weekly_near_high"] = weekly(df, "dist_hi7")
    res["weekly_model"] = weekly(df, preds=walk_forward(df, FEATURES))
    res["weekly_luck"] = [weekly(df, preds=walk_forward(df, FEATURES, shuffle=True, seed=s))["net_week_pct"] for s in range(5)]
    json.dump(res, open(OUT, "w"), indent=1)
    return res


if __name__ == "__main__":
    r = main()
    print(f"{r['rows']} rows · {r['coins']} coins · {r['period']}")
    print("Single inputs (rank IC vs next-24h coin-minus-BTC):")
    for f, v in sorted(r["ic"].items(), key=lambda kv: -abs(kv[1]["ic"])):
        print(f"  {f:10s} ic {v['ic']:+.4f} t {v['t_daily']:+5.2f} | first {v['first']:+.4f} second {v['second']:+.4f}")
    for k in ("model_all", "model_no_price", "momentum_only"):
        v = r[k]
        print(f"{k:15s} OOS IC {v['oos_ic']:+.4f} (t {v['ic_t']:+.2f}) · net {v['net_day_pct']:+.4f}%/day (first {v['first_half_pct']:+.4f}, "
              f"second {v['second_half_pct']:+.4f}) ci95 {v['ci95_pct']} · +days {v['positive_days_pct']}% {'PASS' if v['passes'] else ''}")
    print("luck (shuffled targets), net %/day:", r["luck"])
    for k in ("weekly_low_vol", "weekly_near_high", "weekly_model"):
        v = r[k]
        print(f"{k:17s} {v['weeks']} weeks · net {v['net_week_pct']:+.3f}%/week (first {v['first_half']:+.3f}, second {v['second_half']:+.3f}) "
              f"ci95 {v['ci95_pct']} · +weeks {v['positive_weeks_pct']}% {'PASS' if v['passes'] else ''}")
    print("weekly luck, net %/week:", r["weekly_luck"])
