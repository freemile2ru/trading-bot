"""Exit lab — how should a trade be managed once it's in? (Prompted by winners turning into losers.)

The live policy until 2026-10-07 ("trail", trading/exits.py) did nothing until +1.5R (TP1): then the stop went to
breakeven-plus-costs and trailed 1R behind the best price, with a max hold by timeframe (72 h on 1h
signals, 7 days on 4h). But the median trade peaks at about +0.6R and only ~30% ever reach +1R — so
most trades that were winning never got protected, and rode back to the original stop. This lab's
result replaced it with "protect" (breakeven at +1R, trail 0.5R, max 24 h).

This replays the SAME entries under many exit rules and compares them:
  * the year-long replay — every autotrader-gated entry from research/setup_lab.py's cached run
    (40 perpetuals, a scan every 4 h; ~22k entries; train = first 60%, test = last 40%)
  * the swing books' actual closed trades, on their real 15-minute paths (smaller, but real)

Each rule is a combination of:
  breakeven  move the stop to entry + costs once the trade has been +X R
  trail      after +A R, keep the stop D R behind the best price
  target     take everything at +T R
  giveback   once the peak is ≥ +1R, exit if the trade gives back G of its peak
  time       close at market after H hours if nothing else has
Evaluation is pessimistic, as in exits.apply_bar: within a bar the stop is checked before anything
favourable, and stop moves take effect from the next bar. Costs: taker fee + slippage both ways.

    ./venv/bin/python -m research.exit_lab
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

SCANS = os.path.join(CACHE_DIR, "setup_results_365d_40c.pkl")
KLINES = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
RESULTS = os.path.join(CACHE_DIR, "exit_lab_results.json")
BAR_S = 900
MAX_HOLD_H = {"15m": 24, "30m": 48, "1h": 72, "4h": 168}       # the live max hold by timeframe
TRAIN_FRAC = 0.6


@dataclass(frozen=True)
class Rule:
    name: str
    be_at: float | None = None          # breakeven once +X R
    trail_at: float | None = None       # start trailing once +A R …
    trail_by: float | None = None       # … D R behind the best price
    target: float | None = None         # exit everything at +T R
    giveback: float | None = None       # once peak ≥ 1R, exit if it falls to (1 − G) × peak
    hours: float | None = None          # time exit; None = the live max hold for the timeframe


RULES = [
    Rule("live: BE+trail from 1.5R", be_at=1.5, trail_at=1.5, trail_by=1.0),
    Rule("stop only (hold to max)"),
    Rule("BE at 1R, trail 1R", be_at=1.0, trail_at=1.0, trail_by=1.0),
    Rule("BE at 0.75R, trail 1R from 1.5R", be_at=0.75, trail_at=1.5, trail_by=1.0),
    Rule("BE at 0.5R, trail 1R from 1.5R", be_at=0.5, trail_at=1.5, trail_by=1.0),
    Rule("BE at 1R, trail 0.5R from 1R", be_at=1.0, trail_at=1.0, trail_by=0.5),
    Rule("BE at 0.5R, trail 0.5R from 1R", be_at=0.5, trail_at=1.0, trail_by=0.5),
    Rule("trail 0.75R from 0.75R", trail_at=0.75, trail_by=0.75),
    Rule("target 1R", target=1.0),
    Rule("target 1.5R", target=1.5),
    Rule("target 2R", target=2.0),
    Rule("BE at 1R, target 2R", be_at=1.0, target=2.0),
    Rule("give back ≤ 50% after 1R", giveback=0.5),
    Rule("give back ≤ 33% after 1R", giveback=0.33),
    Rule("live, max 12 h", be_at=1.5, trail_at=1.5, trail_by=1.0, hours=12),
    Rule("live, max 24 h", be_at=1.5, trail_at=1.5, trail_by=1.0, hours=24),
    Rule("live, max 48 h", be_at=1.5, trail_at=1.5, trail_by=1.0, hours=48),
    Rule("BE 1R + trail 0.5R, max 24 h", be_at=1.0, trail_at=1.0, trail_by=0.5, hours=24),
    Rule("BE 0.5R + target 1.5R, max 48 h", be_at=0.5, target=1.5, hours=48),
    Rule("target 1R, max 24 h", target=1.0, hours=24),
]


# ── Simulation (vectorised per trade) ──────────────────────────────────────────

def paths(o, h, lo, c, entry: float, s: int, r_unit: float):
    """Bar-by-bar favourable / adverse excursion, open and close — all in R."""
    if s > 0:
        return (h - entry) / r_unit, (lo - entry) / r_unit, (o - entry) / r_unit, (c - entry) / r_unit
    return (entry - lo) / r_unit, (entry - h) / r_unit, (entry - o) / r_unit, (entry - c) / r_unit


def simulate(rule: Rule, fav, adv, opn, cls, max_bars: int, cost_r: float) -> tuple[float, int, float]:
    """(net R, bars held, peak R) for one trade under one rule."""
    n = min(len(fav), max_bars)
    if n == 0:
        return np.nan, 0, np.nan
    fav, adv, opn, cls = fav[:n], adv[:n], opn[:n], cls[:n]
    peak = np.maximum.accumulate(fav)
    prev = np.concatenate([[0.0], peak[:-1]])            # what was known when the bar opened
    stop = np.full(n, -1.0)
    if rule.be_at is not None:
        stop = np.where(prev >= rule.be_at, np.maximum(stop, cost_r), stop)
    if rule.trail_at is not None:
        stop = np.where(prev >= rule.trail_at, np.maximum(stop, prev - rule.trail_by), stop)
    if rule.giveback is not None:
        stop = np.where(prev >= 1.0, np.maximum(stop, prev * (1 - rule.giveback)), stop)
    hit_stop = adv <= stop
    hit_tp = (fav >= rule.target) if rule.target is not None else np.zeros(n, bool)
    hit_tp &= ~hit_stop                                   # pessimistic: the stop wins the bar
    first_stop = np.argmax(hit_stop) if hit_stop.any() else n
    first_tp = np.argmax(hit_tp) if hit_tp.any() else n
    i = min(first_stop, first_tp)
    if i == n:                                           # time / max-hold exit at the last close
        return cls[n - 1] - cost_r, n, float(peak[n - 1])
    if first_stop <= first_tp:
        px = min(opn[i], stop[i])                        # a gap through the stop fills at the open
        return px - cost_r, i + 1, float(peak[i])
    return rule.target - cost_r, i + 1, float(peak[i])


# ── Data ───────────────────────────────────────────────────────────────────────

def _replay_trades() -> list[dict]:
    from trading import futures
    scans = pd.read_pickle(SCANS)
    kl = pd.read_pickle(KLINES)
    scans = scans[scans.autotrader_bias.notna()]
    out = []
    for sym, g in scans.groupby("symbol"):
        k = kl.get(sym)
        if k is None:
            continue
        idx = {t: i for i, t in enumerate(k.index)}
        o, h, lo, c = (k[x].values.astype(float) for x in ("open", "high", "low", "close"))
        for r in g.itertuples():
            i = idx.get(r.ts)
            if i is None or i >= len(o) - 4:
                continue
            s = 1 if r.autotrader_bias == "long" else -1
            sl = getattr(r, "sl_long" if s > 0 else "sl_short")
            entry = o[i] * (1 + s * futures.SLIPPAGE)
            r_unit = entry * sl / 100
            end = min(len(o), i + int(168 * 3600 / BAR_S))
            out.append({"ts": r.ts, "symbol": sym, "s": s, "tf": r.best_tf or "1h", "entry": entry, "r_unit": r_unit,
                        "bars": (o[i:end], h[i:end], lo[i:end], c[i:end]),
                        "cost_r": entry * 2 * (futures.TAKER_FEE + futures.SLIPPAGE) / r_unit})
    return out


async def _actual_trades() -> list[dict]:
    """The swing books' closed trades (not Hold-72 pairs, not scalps), on their real 15m paths."""
    from data.derivatives_client import fetch_klines_range
    from trading import futures, overview
    from experiments import books
    from live import swing as live_swing
    wallets = [w for s in ("main", "auto") for w in live_swing.wallets(s)] + list(books.WALLETS.values())
    out, sem = [], asyncio.Semaphore(4)

    async def one(t: dict):
        opened = overview._ts(t.get("opened_at"))
        stop0 = t.get("original_stop_loss") or t.get("stop_loss")
        entry = t.get("initial_entry_price") or t.get("entry_price")
        if not (opened and stop0 and entry) or abs(entry - stop0) <= 0:
            return None
        s = 1 if t["direction"] == "long" else -1
        async with sem:
            rows = await fetch_klines_range(t["symbol"], "15m", int(opened * 1000), int(opened * 1000) + 168 * 3_600_000)
        if len(rows) < 4:
            return None
        a = np.array(rows, dtype=float)
        r_unit = abs(entry - stop0)
        return {"ts": pd.Timestamp(opened, unit="s", tz="UTC"), "symbol": t["symbol"], "s": s,
                "tf": t.get("timeframe") or "1h", "entry": entry, "r_unit": r_unit,
                "bars": (a[:, 1], a[:, 2], a[:, 3], a[:, 4]), "actual_r": t.get("pnl_usd", 0) / (t.get("risk_usd") or 1),
                "cost_r": entry * 2 * (futures.TAKER_FEE + futures.SLIPPAGE) / r_unit}

    closed = [t for w in wallets for t in w.all_trades()["closed"] if t.get("status", "closed") == "closed"]
    for res in await asyncio.gather(*(one(t) for t in closed)):
        if res:
            out.append(res)
    return out


# ── Evaluation ─────────────────────────────────────────────────────────────────

def _ci(daily: np.ndarray, rng: random.Random, block: int = 10, sims: int = 2000) -> list[float]:
    n = len(daily)
    if n <= block:
        return [float("nan")] * 2
    boot = sorted(daily[np.concatenate([np.arange(st, st + block) for st in (rng.randrange(n - block + 1)
                                                                              for _ in range(math.ceil(n / block)))])[:n]].mean()
                  for _ in range(sims))
    return [round(float(boot[int(.025 * sims)]), 3), round(float(boot[int(.975 * sims)]), 3)]


def evaluate(trades: list[dict], cut=None) -> list[dict]:
    rng = random.Random(9)
    rows = []
    for rule in RULES:
        res = []
        for t in trades:
            fav, adv, opn, cls = paths(*t["bars"], t["entry"], t["s"], t["r_unit"])
            hours = rule.hours or MAX_HOLD_H.get(t["tf"], 72)
            r, held, peak = simulate(rule, fav, adv, opn, cls, int(hours * 3600 / BAR_S), t["cost_r"])
            res.append((t["ts"], r, held, peak))
        df = pd.DataFrame(res, columns=["ts", "r", "bars", "peak"]).dropna()
        daily = df.groupby(df.ts.dt.floor("D")).r.mean()
        row = {"rule": rule.name, **{k: v for k, v in asdict(rule).items() if k != "name"}, "trades": len(df),
               "avg_r": round(df.r.mean(), 3), "win_pct": round((df.r > 0).mean() * 100, 1),
               "avg_hold_h": round(df.bars.mean() * BAR_S / 3600, 1),
               # "Won then lost": was ≥ +1R at some point, closed below zero.
               "won_then_lost_pct": round(((df.peak >= 1) & (df.r < 0)).mean() * 100, 1),
               "daily_ci_r": _ci(daily.values, rng)}
        if cut is not None:
            row["train_r"] = round(df[df.ts < cut].r.mean(), 3)
            row["test_r"] = round(df[df.ts >= cut].r.mean(), 3)
            tdaily = df[df.ts >= cut].groupby(df.ts.dt.floor("D")).r.mean()
            row["test_ci_r"] = _ci(tdaily.values, rng)
        rows.append(row)
    return rows


def main() -> dict:
    t0 = time.time()
    replay = _replay_trades()
    cut = pd.Series([t["ts"] for t in replay]).quantile(TRAIN_FRAC)
    rep = evaluate(replay, cut)
    actual = asyncio.run(_actual_trades())
    act = evaluate(actual)
    actual_live_r = round(float(np.mean([t["actual_r"] for t in actual])), 3) if actual else None
    out = {"generated_at": time.time(), "replay": {"trades": len(replay), "split": str(cut.date()), "rules": rep},
           "actual": {"trades": len(actual), "booked_avg_r": actual_live_r, "rules": act}}
    json.dump(out, open(RESULTS, "w"), indent=1, default=str)
    pd.set_option("display.width", 250)
    cols = ["rule", "trades", "avg_r", "train_r", "test_r", "test_ci_r", "win_pct", "won_then_lost_pct", "avg_hold_h"]
    print(f"REPLAY — {len(replay)} autotrader-gated entries, 40 perpetuals, test from {cut.date()} ({time.time() - t0:.0f}s)")
    print(pd.DataFrame(rep)[cols].sort_values("test_r", ascending=False).to_string(index=False))
    print(f"\nACTUAL — {len(actual)} closed swing trades re-run on their real paths (booked: {actual_live_r}R avg)")
    print(pd.DataFrame(act)[["rule", "trades", "avg_r", "daily_ci_r", "win_pct", "won_then_lost_pct", "avg_hold_h"]]
          .sort_values("avg_r", ascending=False).to_string(index=False))
    return out


if __name__ == "__main__":
    main()
