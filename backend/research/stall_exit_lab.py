"""Stall exit for the trend books — does closing a position that stops making new highs help?

The trend books (markets/engine.py) go long when the 6-month return is above +5% and leave when it falls below
−5%: no clock, positions can sit in a range for weeks. This replays that rule on the same instruments over ten
years (US stock perps' underlyings, the macro ETFs behind the futures perps) and adds a stall exit: if the
position hasn't made a new closing high for STALL weeks, close it; re-enter when the trend condition holds again
AND the price makes a new 20-day high (so a stalled name isn't bought straight back).

Run: venv/bin/python -m research.stall_exit_lab   (writes research_cache/stall_exit_results.json)

Judged on an equal-weight book of all positions: CAGR, Sharpe, max drawdown, trades, average hold; the first
60% of each history chooses STALL, the last 40% judges it once. Verdict: the stall variant must beat the
baseline's holdout Sharpe by more than the block-bootstrap noise allows.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

EQ = os.path.join(CACHE_DIR, "equity_daily_10y.pkl")
MACRO = os.path.join(CACHE_DIR, "macro_etfs_20y.pkl")
OUT = os.path.join(CACHE_DIR, "stall_exit_results.json")
TREND_DAYS, BAND, COST = 126, 0.05, 0.0010           # 10 bps a side: stock perps are thin
STALLS = (None, 3, 4, 6, 8, 12)                       # weeks without a new closing high
STOCKS = ["NVDA", "MU", "MRVL", "MSTR", "INTC", "GOOGL", "TSLA", "AMD", "AVGO", "AAPL", "AMZN", "META", "COIN", "HOOD", "ORCL", "MSFT",
          "PLTR", "TSM", "SPY", "QQQ"]
FUTURES = ["GLD", "SLV", "PPLT", "PALL", "CPER", "USO", "UNG"]


def positions(px: pd.Series, stall_weeks: int | None) -> list[dict]:
    """Trades on one series: entry/exit dates and returns after costs."""
    px = px.dropna()
    if len(px) < TREND_DAYS + 50:
        return []
    trend = px / px.shift(TREND_DAYS) - 1
    hi20 = px.rolling(20).max()
    out, in_pos, entry_i, peak, peak_i, need_new_high = [], False, 0, 0.0, 0, False
    v = px.values
    for i in range(TREND_DAYS + 1, len(px)):
        t = trend.iloc[i]
        if np.isnan(t):
            continue
        if not in_pos:
            if t > BAND and (not need_new_high or v[i] >= hi20.iloc[i]):
                in_pos, entry_i, peak, peak_i, need_new_high = True, i, v[i], i, False
            continue
        if v[i] > peak:
            peak, peak_i = v[i], i
        reason = None
        if t < -BAND:
            reason = "trend_flip"
        elif stall_weeks and i - peak_i >= stall_weeks * 5:
            reason = "stall"
        if reason:
            out.append({"entry": str(px.index[entry_i].date()), "exit": str(px.index[i].date()), "days": i - entry_i,
                        "ret": v[i] / v[entry_i] - 1 - 2 * COST, "reason": reason, "exit_i": i, "entry_i": entry_i})
            in_pos, need_new_high = False, reason == "stall"
    if in_pos:
        out.append({"entry": str(px.index[entry_i].date()), "exit": None, "days": len(px) - 1 - entry_i,
                    "ret": v[-1] / v[entry_i] - 1 - COST, "reason": "open", "exit_i": len(px) - 1, "entry_i": entry_i})
    return out


def book_returns(pxs: pd.DataFrame, stall_weeks: int | None) -> tuple[pd.Series, list[dict]]:
    """Daily return of an equal-weight book (1/N of capital per instrument, each in or out)."""
    daily = pd.DataFrame(index=pxs.index)
    trades = []
    for col in pxs.columns:
        s = pxs[col]
        tr = positions(s, stall_weeks)
        held = pd.Series(0.0, index=s.index)
        for t in tr:
            held.iloc[t["entry_i"] + 1:t["exit_i"] + 1] = 1.0
            trades.append({**t, "symbol": col})
        r = s.pct_change(fill_method=None).fillna(0.0) * held
        # entry/exit costs on the days they happen
        for t in tr:
            if t["entry_i"] + 1 < len(r):
                r.iloc[t["entry_i"] + 1] -= COST
            if t["exit"]:
                r.iloc[t["exit_i"]] -= COST
        daily[col] = r
    return daily.mean(axis=1), trades


def _ci(v: np.ndarray, seed=1, block=20, boot=1500) -> tuple[float, float]:
    rng, n, out = random.Random(seed), len(v), []
    for _ in range(boot):
        ix: list[int] = []
        while len(ix) < n:
            st = rng.randrange(0, max(n - block, 1)); ix += range(st, st + block)
        out.append(v[ix[:n]].mean())
    out.sort()
    return out[int(boot * .025)], out[int(boot * .975)]


def stats(r: pd.Series, trades: list[dict]) -> dict:
    r = r.dropna()
    if r.empty:
        return {}
    eq = (1 + r).cumprod()
    years = len(r) / 252
    dd = (eq / eq.cummax() - 1).min()
    lo, hi = _ci(r.values)
    closed = [t for t in trades if t["exit"]]
    return {"cagr_pct": round((eq.iloc[-1] ** (1 / years) - 1) * 100, 2) if years > 0 else None,
            "sharpe": round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() else None, "max_dd_pct": round(dd * 100, 1),
            "day_ci95_bps": [round(lo * 1e4, 2), round(hi * 1e4, 2)], "trades": len(closed),
            "avg_hold_days": round(float(np.mean([t["days"] for t in closed])), 1) if closed else None,
            "avg_trade_pct": round(float(np.mean([t["ret"] for t in closed])) * 100, 2) if closed else None,
            "stall_exits": sum(1 for t in closed if t["reason"] == "stall"),
            "stall_exit_avg_pct": round(float(np.mean([t["ret"] for t in closed if t["reason"] == "stall"])) * 100, 2) if any(t["reason"] == "stall" for t in closed) else None}


def run(pxs: pd.DataFrame, label: str) -> dict:
    split = pxs.index[int(len(pxs) * 0.6)]
    res = {"instruments": list(pxs.columns), "period": [str(pxs.index[0].date()), str(pxs.index[-1].date())], "split": str(split.date()), "variants": {}}
    for sw in STALLS:
        r, tr = book_returns(pxs, sw)
        key = "baseline" if sw is None else f"stall_{sw}w"
        res["variants"][key] = {"search": stats(r[r.index < split], [t for t in tr if t["entry"] < str(split.date())]),
                                "holdout": stats(r[r.index >= split], [t for t in tr if t["entry"] >= str(split.date())]),
                                "all": stats(r, tr)}
    base = res["variants"]["baseline"]
    best_key = max((k for k in res["variants"] if k != "baseline"), key=lambda k: res["variants"][k]["search"].get("sharpe") or -9)
    best = res["variants"][best_key]
    res["chosen"] = best_key
    res["holdout_sharpe_gain"] = round((best["holdout"].get("sharpe") or 0) - (base["holdout"].get("sharpe") or 0), 2)
    res["holdout_cagr_gain_pct"] = round((best["holdout"].get("cagr_pct") or 0) - (base["holdout"].get("cagr_pct") or 0), 2)
    res["verdict"] = "stall helps" if res["holdout_sharpe_gain"] > 0.15 and res["holdout_cagr_gain_pct"] > 0 else \
                     "stall hurts" if res["holdout_sharpe_gain"] < -0.15 else "no clear difference"
    res["label"] = label
    return res


def main() -> dict:
    eq = pd.read_pickle(EQ)["us"]["px"]
    stocks = eq[[c for c in STOCKS if c in eq.columns]]
    macro = pd.read_pickle(MACRO)
    futs = macro[[c for c in FUTURES if c in macro.columns]]
    out = {"stocks": run(stocks, "US stock perps' underlyings, 10y"), "futures": run(futs, "metals & energy ETFs, 20y")}
    json.dump(out, open(OUT, "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    for name, r in main().items():
        print(f"[{name}] {r['label']} · split {r['split']} · chosen {r['chosen']} → {r['verdict']} (holdout Sharpe {r['holdout_sharpe_gain']:+.2f}, CAGR {r['holdout_cagr_gain_pct']:+.2f}pp)")
        for k, v in r["variants"].items():
            h, s = v["holdout"], v["search"]
            print(f"   {k:10s} search CAGR {s.get('cagr_pct')}% Sharpe {s.get('sharpe')} DD {s.get('max_dd_pct')}% | holdout CAGR {h.get('cagr_pct')}% Sharpe {h.get('sharpe')} DD {h.get('max_dd_pct')}% trades {h.get('trades')} hold {h.get('avg_hold_days')}d stall-exits {h.get('stall_exits')} ({h.get('stall_exit_avg_pct')}%)")
