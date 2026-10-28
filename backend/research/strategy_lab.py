"""Strategy lab — backtests of strategy FAMILIES different from the TA-setup systems.

Everything built so far entered on intraday technical setups, and a year of replay found no edge
in them. These are the slower, portfolio-style strategies with the most support in the crypto
research literature, each tested on the same cached year of 40 USDT perpetuals:

  xsmom    cross-sectional momentum / reversal — every H days, long the k coins with the best
           L-day return and short the k worst (or the reverse), dollar-neutral
  tsmom    time-series trend — each coin long when its L-day trend is up; short (or flat) when
           down; sized to equal volatility; daily rebalance
  funding  funding carry / crowding — every H days, short the k coins whose perpetual funding
           (averaged over L days) is highest and long the k lowest: collects funding and fades
           the crowded side

Method (the same for all three):
  * daily closes (00:00 UTC) from hourly data; costs 0.08% of turnover per side (taker fee +
    slippage); real funding charged on every position (positive rate: longs pay)
  * walk-forward: parameters chosen on the FIRST half of the year only, judged on the SECOND
  * the whole parameter grid is reported on the test half too, so one lucky setting can't pass
    as an edge
  * pre-registered verdict: "promising" only if the chosen parameters' test-half 95% interval
    (moving-block bootstrap, 10-day blocks) of the mean daily return is above zero
Caveat: the 40 coins are today's most liquid perpetuals — a survivorship bias that flatters
long-biased strategies (tsmom long/flat most of all).

    ./venv/bin/python -m research.strategy_lab             # the cached year of hourly data (40 coins)
    ./venv/bin/python -m research.strategy_lab --years 4   # 4 years of daily data (downloaded once, cached):
                                                           # a test half of ~2 years instead of ~6 months
Both print the report and write the JSON the Lab tab shows (the last run wins).
"""
from __future__ import annotations

import itertools
import json
import math
import os
import pickle
import random
import time

import numpy as np
import pandas as pd

CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache")
HISTORY = os.path.join(CACHE, "history_365d_40c.pkl")
RESULTS = os.path.join(CACHE, "strategy_lab_results.json")
COST_PER_SIDE = 0.0008            # taker 0.05% + slippage 0.03%, per unit of notional traded
BLOCK = 10                        # days per bootstrap block (returns are autocorrelated)
SIMS = 2000


# ── Data ───────────────────────────────────────────────────────────────────────

def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily closes and daily summed funding rates, coins as columns."""
    px, fund, _ = pickle.load(open(HISTORY, "rb"))
    closes = pd.DataFrame({s: df["c"] for s, df in px.items()})
    closes = closes.where(closes > 0)
    daily = closes.resample("1D").last()
    funding = pd.DataFrame({s: f for s, f in fund.items()}).resample("1D").sum().reindex(daily.index).fillna(0.0)
    # A coin counts only from its first real price (listings mid-year).
    return daily, funding


def load_long(years: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`years` of daily closes and funding for the same 40 perpetuals, from Binance (cached).
    Coins listed later simply have no data (NaN) before their listing and join when they start."""
    path = os.path.join(CACHE, f"daily_{years}y_40c.pkl")
    if os.path.exists(path):
        return pickle.load(open(path, "rb"))
    import asyncio
    from data.derivatives_client import fetch_funding_range, fetch_klines_range
    symbols = sorted(pickle.load(open(HISTORY, "rb"))[0])
    end = int(time.time() * 1000)
    start = end - years * 365 * 86400 * 1000

    async def grab():
        closes, fund = {}, {}
        for i, s in enumerate(symbols):
            k = await fetch_klines_range(s, "1d", start, end)
            f = await fetch_funding_range(s, start, end)
            closes[s] = pd.Series({pd.Timestamp(r[0], unit="ms", tz="UTC"): r[4] for r in k})
            fund[s] = pd.Series({pd.Timestamp(r["time"], unit="ms", tz="UTC"): r["rate"] for r in f})
            print(f"  {i + 1}/{len(symbols)} {s}: {len(k)} days, {len(f)} funding settlements")
        return closes, fund
    closes, fund = asyncio.run(grab())
    daily = pd.DataFrame(closes).sort_index()
    daily.index = daily.index.normalize()
    funding = pd.DataFrame({s: f for s, f in fund.items() if len(f)}).resample("1D").sum().reindex(daily.index).fillna(0.0)
    pickle.dump((daily, funding), open(path, "wb"))
    return daily, funding


# ── Portfolio accounting ───────────────────────────────────────────────────────

def run(weights: pd.DataFrame, daily: pd.DataFrame, funding: pd.DataFrame) -> pd.Series:
    """Daily net returns of holding `weights` (set at each day's close, held to the next)."""
    rets = daily.pct_change().shift(-1)                    # return earned by today's weights, tomorrow
    w = weights.reindex(daily.index).fillna(0.0)
    gross = (w * rets).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1)
    turnover.iloc[0] = w.iloc[0].abs().sum()               # opening the first positions is a trade too
    fund = (w * funding.shift(-1)).sum(axis=1)             # longs pay positive funding, shorts receive
    return (gross - turnover * COST_PER_SIDE - fund).iloc[:-1].dropna()


def stats(r: pd.Series, rng: random.Random | None = None) -> dict:
    r = r.dropna()
    if len(r) < 20:
        return {"days": len(r)}
    m, sd = r.mean(), r.std()
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    out = {"days": len(r), "total_return_pct": round((eq.iloc[-1] - 1) * 100, 2),
           "annual_return_pct": round(((1 + m) ** 365 - 1) * 100, 1), "sharpe": round(m / sd * math.sqrt(365), 2) if sd else None,
           "max_drawdown_pct": round(dd * 100, 1), "t_stat": round(m / (sd / math.sqrt(len(r))), 2) if sd else None,
           "win_days_pct": round((r > 0).mean() * 100, 1), "mean_daily_bps": round(m * 1e4, 2)}
    if rng:
        vals, n = r.values, len(r)
        starts = n - BLOCK + 1
        boot = []
        for _ in range(SIMS):
            idx = np.concatenate([np.arange(s, s + BLOCK) for s in (rng.randrange(starts) for _ in range(math.ceil(n / BLOCK)))])[:n]
            boot.append(vals[idx].mean())
        boot.sort()
        out["mean_daily_ci_bps"] = [round(float(boot[int(0.025 * SIMS)]) * 1e4, 2), round(float(boot[int(0.975 * SIMS)]) * 1e4, 2)]
    return out


# ── Strategies: each returns daily target weights (gross exposure 1) ──────────

def _rebalanced(score: pd.DataFrame, start: int, hold: int, k: int) -> pd.DataFrame:
    """Long the k highest scores, short the k lowest, set on rebalance days and held until the
    next one. Whole rows are set on rebalance days (zeros included), then carried forward — so a
    coin that drops out of the selection is closed at that rebalance, not held over."""
    w = pd.DataFrame(np.nan, index=score.index, columns=score.columns)
    for i in range(start, len(score), hold):
        row = score.iloc[i].dropna()
        if len(row) < 2 * k:
            continue
        ranked = row.sort_values()
        target = pd.Series(0.0, index=score.columns)
        target[ranked.index[-k:]] = 0.5 / k
        target[ranked.index[:k]] = -0.5 / k
        w.iloc[i] = target.values
    return w.ffill().fillna(0.0)


def xsmom(daily: pd.DataFrame, funding: pd.DataFrame, lookback: int, hold: int, k: int, direction: int) -> pd.DataFrame:
    return _rebalanced(daily.pct_change(lookback) * direction, lookback, hold, k)


COIN_DAILY_VOL_TARGET = 0.025     # each coin's position sized to ~2.5% daily volatility, capped at its 1/N share


def tsmom(daily: pd.DataFrame, funding: pd.DataFrame, lookback: int, mode: str) -> pd.DataFrame:
    """Each coin gets a fixed 1/N risk budget — long when its trend is up, short (or flat) when
    down, cash when it has no position. (Normalising the book to be always fully invested put
    everything into the one or two coins still trending — a 73% drawdown on 4 years.)"""
    trend = np.sign(daily.pct_change(lookback))
    if mode == "long_flat":
        trend = trend.clip(lower=0)
    vol = daily.pct_change().rolling(20).std()
    listed = daily.notna().sum(axis=1).clip(lower=1)
    size = (COIN_DAILY_VOL_TARGET / vol).clip(upper=1.0).div(listed, axis=0)
    return (trend * size).where(daily.notna()).fillna(0.0)


def funding_carry(daily: pd.DataFrame, funding: pd.DataFrame, lookback: int, hold: int, k: int) -> pd.DataFrame:
    avg = funding.rolling(lookback).mean().where(daily.notna())
    return _rebalanced(-avg, lookback, hold, k)             # long the lowest funding, short the highest


FAMILIES = {
    "xsmom": {
        "title": "Cross-sectional momentum / reversal",
        "fn": xsmom,
        "grid": {"lookback": [3, 7, 14, 30], "hold": [1, 3, 7], "k": [3, 5, 8], "direction": [1, -1]},
        "describe": lambda p: f"{'momentum' if p['direction'] > 0 else 'reversal'}: rank on {p['lookback']}-day return, "
                              f"long best {p['k']} / short worst {p['k']}, rebalance every {p['hold']}d",
    },
    "tsmom": {
        "title": "Time-series trend following",
        "fn": tsmom,
        "grid": {"lookback": [10, 20, 50, 100], "mode": ["long_short", "long_flat"]},
        "describe": lambda p: f"{p['lookback']}-day trend, {'long or short' if p['mode'] == 'long_short' else 'long or flat'}, "
                              f"equal volatility per coin, daily",
    },
    "funding": {
        "title": "Funding carry (fade the crowded side)",
        "fn": funding_carry,
        "grid": {"lookback": [1, 3, 7], "hold": [1, 3, 7], "k": [3, 5, 8]},
        "describe": lambda p: f"short {p['k']} highest / long {p['k']} lowest {p['lookback']}-day average funding, "
                              f"rebalance every {p['hold']}d",
    },
}


def verdict(test: dict) -> tuple[str, str]:
    ci = test.get("mean_daily_ci_bps")
    if not ci:
        return "insufficient", "Not enough data"
    if ci[0] > 0:
        return "promising", "Promising — worth a paper test"
    if ci[1] < 0:
        return "fails", "Loses money out of sample"
    return "no_edge", "No edge shown"


def main(years: int | None = None) -> dict:
    daily, funding = load_long(years) if years else load()
    half = daily.index[len(daily) // 2]
    rng = random.Random(3)
    btc = daily["BTCUSDT"].pct_change().dropna()
    report = {"generated_at": time.time(), "dataset": f"{years} years of daily data" if years else "1 year of hourly data",
              "period": [str(daily.index[0].date()), str(daily.index[-1].date())],
              "split": str(half.date()), "coins": len(daily.columns), "cost_per_side_pct": COST_PER_SIDE * 100,
              "benchmark_btc_test": stats(btc[btc.index >= half]), "families": []}
    for key, fam in FAMILIES.items():
        grid = [dict(zip(fam["grid"], combo)) for combo in itertools.product(*fam["grid"].values())]
        rows = []
        for p in grid:
            r = run(fam["fn"](daily, funding, **p), daily, funding)
            rows.append({"params": p, "train": stats(r[r.index < half]), "test": stats(r[r.index >= half]), "series": r})
        # Chosen on the training half only.
        best = max(rows, key=lambda x: x["train"].get("sharpe") if x["train"].get("sharpe") is not None else -99)
        best["test"] = stats(best["series"][best["series"].index >= half], rng)
        status, label = verdict(best["test"])
        test_sharpes = [x["test"].get("sharpe") for x in rows if x["test"].get("sharpe") is not None]
        curve = (1 + best["series"][best["series"].index >= half]).cumprod()
        report["families"].append({
            "key": key, "title": fam["title"], "chosen": fam["describe"](best["params"]), "params": best["params"],
            "train": best["train"], "test": best["test"], "status": status, "verdict": label,
            "grid_size": len(rows),
            "grid_test_positive_pct": round(sum(1 for s in test_sharpes if s > 0) / len(test_sharpes) * 100, 1) if test_sharpes else None,
            "grid_test_median_sharpe": round(float(np.median(test_sharpes)), 2) if test_sharpes else None,
            "grid": sorted(({"setting": fam["describe"](x["params"]), "train_sharpe": x["train"].get("sharpe"),
                             "test_sharpe": x["test"].get("sharpe"), "test_return_pct": x["test"].get("total_return_pct")}
                            for x in rows), key=lambda g: -(g["train_sharpe"] or -99))[:12],
            "curve": [{"day": str(d.date()), "equity": round(float(v), 4)} for d, v in curve.items()],
        })
    btc_curve = (1 + btc[btc.index >= half]).cumprod()
    report["benchmark_btc_curve"] = [{"day": str(d.date()), "equity": round(float(v), 4)} for d, v in btc_curve.items()]
    json.dump(report, open(RESULTS, "w"), indent=1, default=str)
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=None, help="use N years of daily data instead of the cached hourly year")
    rep = main(ap.parse_args().years)
    print(f"Period {rep['period'][0]} → {rep['period'][1]}, {rep['coins']} coins, train/test split {rep['split']}, "
          f"cost {rep['cost_per_side_pct']:.2f}% per side")
    b = rep["benchmark_btc_test"]
    print(f"BTC buy & hold, test half: {b.get('total_return_pct')}% (Sharpe {b.get('sharpe')})\n")
    for f in rep["families"]:
        t, tr = f["test"], f["train"]
        print(f"■ {f['title']} — {f['verdict']}")
        print(f"  chosen on train: {f['chosen']}")
        print(f"  train: Sharpe {tr.get('sharpe')}, {tr.get('total_return_pct')}%   test: Sharpe {t.get('sharpe')}, "
              f"{t.get('total_return_pct')}%, max DD {t.get('max_drawdown_pct')}%, t {t.get('t_stat')}, "
              f"mean {t.get('mean_daily_bps')} bps/day, 95% CI {t.get('mean_daily_ci_bps')}")
        print(f"  whole grid on test: {f['grid_test_positive_pct']}% of {f['grid_size']} settings positive, "
              f"median Sharpe {f['grid_test_median_sharpe']}\n")
    print(f"Written to {RESULTS}")
