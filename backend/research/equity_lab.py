"""Equity lab — do well-known stock strategies work on the stocks Binance lists, after costs?

Universe: the plain stocks and broad ETFs Binance lists as perpetuals (no leveraged or inverse
products), in two regions — US, and Asia (Hong Kong + Korea). History: 10 years of daily,
dividend-adjusted closes from Yahoo (the perpetuals themselves are only months old, but they track
the stocks within a few bps — see the 2026-10-06 tracking check). A stock joins once it has data.

Strategy families (each returns daily target weights, gross exposure ≤ 1):
  tsmom      each stock long when its L-day trend is up; short or flat when down; equal volatility
  xsmom      cross-sectional momentum: rank on the L-day return skipping the last month, long the k
             best / short the k worst (or long-only the k best), rebalance every H days
  reversal   short-term reversal: long the k biggest L-day losers / short the k biggest winners
  rsi2       pullbacks in uptrends (Connors): long a stock when RSI(2) < X and price > its 200-day
             average, out when it closes above its 5-day average

Costs, two ways — the venue question:
  perp       Binance perpetual: 0.08% of turnover per side (taker + slippage) and funding — longs
             pay each contract's measured average funding (shorts receive it; median of listed
             names where a stock has no funding history)
  shares     real shares (e.g. Alpaca): 0.02% per side (spread), no funding; shorts pay 0.5%/yr
             borrow
Method (as the crypto strategy lab): parameters chosen on the first half of the history only,
judged on the second; the whole grid is shown on the test half too. Pre-registered verdict:
  * market-neutral strategies: "promising" if the test half's 95% moving-block bootstrap interval
    of the mean daily return is above zero
  * long-biased strategies must also BEAT HOLDING the same stocks (equal weight): the interval of
    the daily difference must be above zero — making money in a rising decade isn't an edge
Caveat: today's listed names are survivors — that flatters long-biased results, momentum most.
Two hindsight-free universes are run beside them as the check: the 30 Dow stocks as of 2016-10-01
(GE, Walgreens and IBM included) and the 11 SPDR sector ETFs. Most aren't on Binance (funding for
them uses the listed names' median) — they're there to show whether a strategy survives without
hindsight, not to be traded.

    ./venv/bin/python -m research.equity_lab            # downloads once, cached for a day
"""
from __future__ import annotations

import itertools
import json
import math
import os
import random
import time

import httpx
import numpy as np
import pandas as pd

from research.strategy_lab import BLOCK, SIMS, _rebalanced

CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache")
DATA = os.path.join(CACHE, "equity_daily_10y.pkl")
RESULTS = os.path.join(CACHE, "equity_lab_results.json")
DAYS = 252

US = ["SPY", "QQQ", "IWM", "EWY", "EWZ", "EWJ", "NVDA", "MU", "MRVL", "MSTR", "INTC", "GOOGL", "TSLA", "AMD", "AVGO",
      "AAPL", "WDC", "AMZN", "META", "COIN", "HOOD", "ORCL", "MSFT", "DELL", "MRNA", "GLW", "PLTR", "QCOM", "TSM", "BABA",
      "JPM", "NFLX", "AMAT", "CRWD", "NOK", "SMCI", "LLY", "ASML", "GME", "APP", "WMT", "KLAC", "IBM", "MCD", "NKE",
      "PANW", "LRCX", "HPE", "NOW", "TTWO", "UBER", "CSCO", "SNOW", "DKNG", "NVO", "ZM", "SHOP", "CRM", "RKLB", "LITE",
      "COHR", "CIEN", "TER", "AAOI", "VST", "IONQ", "HIMS", "ARM", "MSTR"]
# Binance perpetual → home listing.
ASIA = {"HK0700USDT": "0700.HK", "HK1810USDT": "1810.HK", "MEITUANUSDT": "3690.HK", "KUAISHOUUSDT": "1024.HK",
        "BYDUSDT": "1211.HK", "POPMARTUSDT": "9992.HK", "HK0992USDT": "0992.HK", "SKHYNIXUSDT": "000660.KS",
        "SAMSUNGUSDT": "005930.KS", "HYUNDAIUSDT": "005380.KS", "NAVERUSDT": "035420.KS",
        "LGELECTRONICSUSDT": "066570.KS", "SAMSUNGEMUSDT": "009150.KS", "HANMIUSDT": "042700.KS", "KODEX200USDT": "069500.KS"}
# Hindsight-free checks: universes fixed BEFORE the test, so they include the losers. Today's listed
# names are survivors (chosen because they did well), which flatters momentum most of all.
DOW_2016 = ["AAPL", "AXP", "BA", "CAT", "CSCO", "CVX", "KO", "DD", "XOM", "GE", "GS", "HD", "IBM", "INTC", "JNJ", "JPM",
            "MCD", "MMM", "MRK", "MSFT", "NKE", "PFE", "PG", "TRV", "UNH", "RTX", "VZ", "V", "WMT", "DIS"]   # members on 2016-10-01
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
PERP_COST = 0.0008
SHARE_COST = 0.0002
BORROW_PA = 0.005


# ── Data ───────────────────────────────────────────────────────────────────────

def _yahoo(ticker: str) -> pd.Series | None:
    j = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}", params={"range": "10y", "interval": "1d"},
                  headers={"User-Agent": "Mozilla/5.0"}, timeout=30).json()["chart"]["result"]
    if not j:
        return None
    j = j[0]
    adj = (j["indicators"].get("adjclose") or [{}])[0].get("adjclose") or j["indicators"]["quote"][0]["close"]
    s = pd.Series(adj, index=pd.to_datetime(j["timestamp"], unit="s").normalize(), dtype=float).dropna()
    return s[~s.index.duplicated(keep="last")]


def _funding(perp: str) -> float | None:
    """A perpetual's average funding per trading day (longs pay +), from its settlement history."""
    rows = httpx.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": perp, "limit": 1000}, timeout=20).json()
    if not isinstance(rows, list) or len(rows) < 10:
        return None
    span_days = (rows[-1]["fundingTime"] - rows[0]["fundingTime"]) / 8.64e7
    return sum(float(r["fundingRate"]) for r in rows[1:]) / span_days * 365 / DAYS if span_days > 0 else None


def load(refresh: bool = False) -> dict:
    if os.path.exists(DATA) and not refresh and time.time() - os.path.getmtime(DATA) < 86_400:
        cached = pd.read_pickle(DATA)
        if {"dow_2016", "us_sectors"} <= set(cached):
            return cached
    out = {}
    regions = (("us", {f"{t}USDT": t for t in dict.fromkeys(US)}), ("asia", ASIA),
               ("dow_2016", {f"{t}USDT": t for t in DOW_2016}), ("us_sectors", {f"{t}USDT": t for t in SECTORS}))
    for region, names in regions:
        closes, funding = {}, {}
        for perp, ticker in names.items():
            try:
                s = _yahoo(ticker)
                if s is not None and len(s) > 250:
                    closes[ticker] = s
                    funding[ticker] = _funding(perp)
            except Exception as e:
                print(f"  {ticker}: {e}")
            time.sleep(0.2)
        px = pd.DataFrame(closes).sort_index()
        px = px[px.notna().sum(axis=1) >= max(3, len(px.columns) // 4)]       # trading days of that market
        known = [f for f in funding.values() if f is not None]
        med = float(np.median(known)) if known else 0.0
        out[region] = {"px": px, "funding": {t: (f if f is not None else med) for t, f in funding.items()},
                       "funding_median_pa": med * DAYS}
    pd.to_pickle(out, DATA)
    return out


# ── Backtest ───────────────────────────────────────────────────────────────────

def run(w: pd.DataFrame, px: pd.DataFrame, venue: str, funding: dict) -> pd.Series:
    """Daily net returns of weights set at each close and held to the next."""
    rets = px.pct_change(fill_method=None).shift(-1)
    w = w.reindex(px.index).fillna(0.0)
    gross = (w * rets.fillna(0.0)).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1)
    turnover.iloc[0] = w.iloc[0].abs().sum()
    if venue == "perp":
        f = pd.Series(funding).reindex(px.columns).fillna(0.0)
        carry = (w * f).sum(axis=1)                           # longs pay, shorts receive
        cost = turnover * PERP_COST + carry
    else:
        cost = turnover * SHARE_COST + w.clip(upper=0).abs().sum(axis=1) * BORROW_PA / DAYS
    return (gross - cost).iloc[:-1]


def stats(r: pd.Series, rng: random.Random | None = None) -> dict:
    r = r.dropna()
    if len(r) < 40:
        return {"days": len(r)}
    m, sd = r.mean(), r.std()
    eq = (1 + r).cumprod()
    out = {"days": len(r), "total_return_pct": round((eq.iloc[-1] - 1) * 100, 1),
           "annual_return_pct": round(((1 + m) ** DAYS - 1) * 100, 1),
           "sharpe": round(m / sd * math.sqrt(DAYS), 2) if sd else None,
           "max_drawdown_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
           "mean_daily_bps": round(m * 1e4, 2), "t_stat": round(m / (sd / math.sqrt(len(r))), 2) if sd else None}
    if rng:
        out["mean_daily_ci_bps"] = _ci(r, rng)
    return out


def _ci(r: pd.Series, rng: random.Random) -> list[float]:
    vals, n = r.values, len(r)
    starts = n - BLOCK + 1
    boot = sorted(vals[np.concatenate([np.arange(s, s + BLOCK) for s in (rng.randrange(starts) for _ in range(math.ceil(n / BLOCK)))])[:n]].mean()
                  for _ in range(SIMS))
    return [round(float(boot[int(0.025 * SIMS)]) * 1e4, 2), round(float(boot[int(0.975 * SIMS)]) * 1e4, 2)]


# ── Strategies ─────────────────────────────────────────────────────────────────

VOL_TARGET = 0.01                 # each stock sized to ~1% daily volatility, capped at its 1/N share


def _per_stock_size(px: pd.DataFrame) -> pd.DataFrame:
    vol = px.pct_change(fill_method=None).rolling(20).std()
    listed = px.notna().sum(axis=1).clip(lower=1)
    return (VOL_TARGET / vol).clip(upper=1.0).div(listed, axis=0)


def tsmom(px: pd.DataFrame, lookback: int, mode: str) -> pd.DataFrame:
    trend = np.sign(px.pct_change(lookback, fill_method=None))
    if mode == "long_flat":
        trend = trend.clip(lower=0)
    return (trend * _per_stock_size(px)).where(px.notna()).fillna(0.0)


def xsmom(px: pd.DataFrame, lookback: int, hold: int, k: int, mode: str) -> pd.DataFrame:
    score = px.shift(21) / px.shift(lookback) - 1                    # skip the most recent month
    w = _rebalanced(score, lookback, hold, k)
    if mode == "long_only":
        w = (w.clip(lower=0) * 2)                                    # the long half, fully invested
    return w


def reversal(px: pd.DataFrame, lookback: int, hold: int, k: int) -> pd.DataFrame:
    return _rebalanced(-px.pct_change(lookback, fill_method=None), lookback, hold, k)


def rsi2(px: pd.DataFrame, entry: int) -> pd.DataFrame:
    d = px.diff()
    up, down = d.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / down.replace(0, np.nan))
    trend_ok = px > px.rolling(200).mean()
    enter, leave = (rsi < entry) & trend_ok, px > px.rolling(5).mean()
    state = pd.DataFrame(np.nan, index=px.index, columns=px.columns)
    state[enter] = 1.0
    state[leave & ~enter] = 0.0
    held = state.ffill().fillna(0.0)
    listed = px.notna().sum(axis=1).clip(lower=1)
    return held.div(listed, axis=0)


FAMILIES = {
    "tsmom": {"title": "Trend following (each stock)", "fn": tsmom, "neutral": lambda p: p["mode"] == "long_short",
              "grid": {"lookback": [50, 100, 200, 252], "mode": ["long_short", "long_flat"]},
              "describe": lambda p: f"{p['lookback']}-day trend, {'long or short' if p['mode'] == 'long_short' else 'long or flat'}, equal volatility"},
    "xsmom": {"title": "Cross-sectional momentum", "fn": xsmom, "neutral": lambda p: p["mode"] == "long_short",
              "grid": {"lookback": [63, 126, 252], "hold": [21, 63], "k": [5, 10], "mode": ["long_short", "long_only"]},
              "describe": lambda p: f"rank on {p['lookback']}-day return (skip last month), "
                                    f"{'long best / short worst' if p['mode'] == 'long_short' else 'long the best'} {p['k']}, every {p['hold']}d"},
    "reversal": {"title": "Short-term reversal", "fn": reversal, "neutral": lambda p: True,
                 "grid": {"lookback": [5, 10, 21], "hold": [5, 10], "k": [3, 5, 10]},
                 "describe": lambda p: f"long {p['k']} biggest {p['lookback']}-day losers / short {p['k']} winners, every {p['hold']}d"},
    "rsi2": {"title": "Pullbacks in uptrends (RSI-2)", "fn": rsi2, "neutral": lambda p: False,
             "grid": {"entry": [5, 10, 20]},
             "describe": lambda p: f"long when RSI(2) < {p['entry']} above the 200-day average, out above the 5-day average"},
}


def verdict(test: dict, excess: dict | None, neutral: bool) -> tuple[str, str]:
    ci = test.get("mean_daily_ci_bps")
    if not ci:
        return "insufficient", "Not enough data"
    if ci[1] < 0:
        return "fails", "Loses money out of sample"
    if neutral:
        return ("promising", "Promising — worth a paper test") if ci[0] > 0 else ("no_edge", "No edge shown")
    eci = (excess or {}).get("mean_daily_ci_bps")
    if ci[0] > 0 and eci and eci[0] > 0:
        return "promising", "Promising — beats holding the same stocks"
    return "no_edge", "Makes money, but no better than holding" if ci[0] > 0 else "No edge shown"


def main(refresh: bool = False) -> dict:
    data = load(refresh)
    report = {"generated_at": time.time(), "regions": []}
    for region, d in data.items():
        px, funding = d["px"], d["funding"]
        half = px.index[len(px) // 2]
        hold_all = px.notna().astype(float).div(px.notna().sum(axis=1).clip(lower=1), axis=0)
        reg = {"region": region, "stocks": len(px.columns), "period": [str(px.index[0].date()), str(px.index[-1].date())],
               "split": str(half.date()), "funding_median_pa_pct": round(d["funding_median_pa"] * 100, 1), "venues": {}}
        for venue in ("perp", "shares"):
            rng = random.Random(5)
            bench = run(hold_all, px, venue, funding)
            out = {"benchmark_test": stats(bench[bench.index >= half], rng), "families": []}
            for key, fam in FAMILIES.items():
                rows = []
                for combo in itertools.product(*fam["grid"].values()):
                    p = dict(zip(fam["grid"], combo))
                    r = run(fam["fn"](px, **p), px, venue, funding)
                    rows.append({"params": p, "train": stats(r[r.index < half]), "series": r})
                best = max(rows, key=lambda x: x["train"].get("sharpe") if x["train"].get("sharpe") is not None else -99)
                r_test = best["series"][best["series"].index >= half]
                test = stats(r_test, rng)
                neutral = fam["neutral"](best["params"])
                excess = None if neutral else stats(r_test - bench.reindex(r_test.index).fillna(0.0), rng)
                status, label = verdict(test, excess, neutral)
                test_sharpes = [stats(x["series"][x["series"].index >= half]).get("sharpe") for x in rows]
                test_sharpes = [s for s in test_sharpes if s is not None]
                curve = (1 + r_test).cumprod()
                out["families"].append({
                    "key": key, "title": fam["title"], "chosen": fam["describe"](best["params"]), "params": best["params"],
                    "market_neutral": neutral, "train": best["train"], "test": test, "excess_vs_holding": excess,
                    "status": status, "verdict": label, "grid_size": len(rows),
                    "grid_test_positive_pct": round(sum(s > 0 for s in test_sharpes) / len(test_sharpes) * 100, 1) if test_sharpes else None,
                    "grid_test_median_sharpe": round(float(np.median(test_sharpes)), 2) if test_sharpes else None,
                    "curve": [{"day": str(k.date()), "equity": round(float(v), 4)} for k, v in curve.iloc[::5].items()],
                })
            reg["venues"][venue] = out
        report["regions"].append(reg)
    json.dump(report, open(RESULTS, "w"), indent=1, default=str)
    return report


def load_results() -> dict | None:
    try:
        return json.load(open(RESULTS))
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    rep = main(ap.parse_args().refresh)
    for reg in rep["regions"]:
        print(f"\n════ {reg['region'].upper()}: {reg['stocks']} stocks, {reg['period'][0]} → {reg['period'][1]}, "
              f"test from {reg['split']}, perp funding median {reg['funding_median_pa_pct']}%/yr for longs")
        for venue, v in reg["venues"].items():
            b = v["benchmark_test"]
            print(f"  ── {venue}: hold everything (test) Sharpe {b.get('sharpe')}, {b.get('annual_return_pct')}%/yr, DD {b.get('max_drawdown_pct')}%")
            for f in v["families"]:
                t, ex = f["test"], f["excess_vs_holding"] or {}
                print(f"   ■ {f['title']:32} {f['verdict']}")
                print(f"       {f['chosen']}  |  test Sharpe {t.get('sharpe')}, {t.get('annual_return_pct')}%/yr, DD {t.get('max_drawdown_pct')}%, "
                      f"CI {t.get('mean_daily_ci_bps')} bps/day" + (f", vs holding CI {ex.get('mean_daily_ci_bps')}" if ex else "")
                      + f"  |  grid: {f['grid_test_positive_pct']}% of {f['grid_size']} positive on test")
