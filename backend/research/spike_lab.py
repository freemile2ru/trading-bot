"""Spike lab — after a rapid move (ZEC +5% in ten minutes, 2026-11-06 14:40 UTC), is there money in riding it or in
fading it, by enough to pay for the trade?

Why: the reversal lane's ZEC short was stopped out by that burst and the user asked whether a system could profit
from such moves instead. What we knew before this lab: research/continuation_lab.py (1h movers, 4h horizon) lost
after costs at the extremes; research/cascade_lab.py found an IMMEDIATE fade of a liquidation burst loses (the
cascade runs on for about an hour) and a fade entered 60 minutes later made +52 bps a trade on thin data.
Neither looked at the minutes right after a burst on a wide universe. This does, on Binance 1-minute perp bars.

    venv/bin/python -m research.spike_lab            (writes data/research_cache/spike_results.json)

  universe   the COINS most-traded crypto perps on Binance (stock, ETF and commodity perps excluded), DAYS days
  spike      at a minute's close, the last 5 minutes moved ≥ THR % (2 / 3 / 5), at least SIGMA_X × the trailing-day
             standard deviation of 5-minute moves, on ≥ VOL_X × the trailing-day median 5-minute volume; one event
             per coin per COOLDOWN_MIN minutes (the first trigger counts); the BTC move over the same 5 minutes is
             recorded so market-wide bursts can be split from a coin's own
  side       "with" rides the burst's direction, "against" fades it
  delay      enter at the close DELAY minutes after the trigger (0 = the trigger minute's close)
  hold       out at the close HOLD minutes after entry, or at a STOP % disaster stop checked on each minute's
             high/low (a minute that gaps through the stop fills at its open)
  costs      taker fee + slippage each way, as the paper wallet charges — and a burst variant at twice that for
             delay-0 entries, since a market order in a burst fills worse than usual

Reported per variant: events, winners %, average and median bps after costs, t-stat, the two halves of the sample
(by time), the share of coins that are net positive, stops hit, and the split by burst direction (up / down) and by
whether BTC moved too. Also the raw shape: the average move 15 / 60 / 240 minutes after a burst, no rule, no costs.

Added after the first run (2026-11-06, POST-HOC — the first run's grid judged every burst together): the same rules
split by burst direction ("only" = up / down bursts), because the shape table showed up-bursts carrying on while
down-bursts bounced. A split rule that passes is a hypothesis to pre-register and test forward, not a finding:
it was chosen by looking at this data.

Pre-registered verdict (written before the first run): a rule is a candidate only if it has ≥ MIN_EVENTS events,
its average is above zero after costs in BOTH halves, t ≥ 2.5, and at least 60% of the coins with ≥ 5 events are
net positive. A candidate becomes a paper book first (never a prod lane), judged forward like the others. If nothing
passes, the answer to "can we profit from these moves" is no on this data, and the idea is parked.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import time

import httpx
import numpy as np
import pandas as pd

from trading import futures

CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache")
RESULTS = os.path.join(CACHE, "spike_results.json")
COST = futures.TAKER_FEE + futures.SLIPPAGE              # each way
COINS = int(os.getenv("SPIKE_COINS", "40"))
DAYS = int(os.getenv("SPIKE_DAYS", "45"))
THRS = (2.0, 3.0, 5.0)
SIGMA_X, VOL_X, COOLDOWN_MIN, WINDOW = 4.0, 3.0, 60, 1440
SIDES = {"with": 1, "against": -1}
DELAYS = (0, 15, 60)
HOLDS = (15, 60, 240)
STOPS = (None, 0.02, 0.05)
ONLY = ("both", "up", "down")        # which bursts a rule trades: added post-hoc, see the docstring
MIN_EVENTS = 100
FAPI = "https://fapi.binance.com"


async def universe(n: int = COINS) -> list[str]:
    async with httpx.AsyncClient(timeout=30) as c:
        info = (await c.get(f"{FAPI}/fapi/v1/exchangeInfo")).json()
        tick = (await c.get(f"{FAPI}/fapi/v1/ticker/24hr")).json()
    crypto = {s["symbol"] for s in info["symbols"]
              if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
              and s.get("underlyingType") == "COIN"}
    rows = sorted((r for r in tick if r["symbol"] in crypto), key=lambda r: -float(r["quoteVolume"]))
    return [r["symbol"] for r in rows[:n]]


async def bars(symbol: str, days: int = DAYS) -> pd.DataFrame:
    """1-minute bars (o, h, l, c, v), cached for a day."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"spike_{symbol}_1m_{days}d.pkl")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
        return pd.read_pickle(path)
    from data.derivatives_client import fetch_klines_range
    now = int(time.time() * 1000) // 60000 * 60000
    rows = await fetch_klines_range(symbol, "1m", now - days * 86400 * 1000, now)
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v", "q"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df[["o", "h", "l", "c", "v"]].astype(float)
    df = df[~df.index.duplicated()]
    df.to_pickle(path)
    return df


async def load(symbols: list[str]) -> dict[str, pd.DataFrame]:
    sem = asyncio.Semaphore(3)
    out: dict[str, pd.DataFrame] = {}

    async def one(s: str) -> None:
        async with sem:
            t0 = time.time()
            try:
                df = await bars(s)
                if len(df) > WINDOW * 2:
                    out[s] = df
                print(f"  {s}: {len(df)} bars from {df.index[0].date()} ({time.time() - t0:.0f}s)", flush=True)
            except Exception as e:
                print(f"  {s}: FAILED {e}", flush=True)
    await asyncio.gather(*(one(s) for s in symbols))
    return out


def events(df: pd.DataFrame, btc: pd.DataFrame, thr_pct: float) -> pd.DataFrame:
    """Burst triggers under the threshold: one per coin per cooldown, with the forward shape (no costs)."""
    c = df["c"]
    ret5 = c / c.shift(5) - 1
    sigma = ret5.rolling(WINDOW, min_periods=WINDOW // 2).std().shift(1)
    vol5 = df["v"].rolling(5).sum()
    vmed = vol5.rolling(WINDOW, min_periods=WINDOW // 2).median().shift(1)
    hit = (ret5.abs() >= thr_pct / 100) & (ret5.abs() >= SIGMA_X * sigma) & (vol5 >= VOL_X * vmed)
    bret5 = (btc["c"] / btc["c"].shift(5) - 1).reindex(df.index)
    rows, last = [], -10 ** 12
    idx = np.flatnonzero(hit.values)
    ts = df.index.asi8 // 10 ** 9
    for i in idx:
        if ts[i] - last < COOLDOWN_MIN * 60 or i + 240 >= len(df):
            continue
        last = ts[i]
        d = 1 if ret5.iloc[i] > 0 else -1
        b = bret5.iloc[i]
        rows.append({"i": int(i), "ts": int(ts[i]), "dir": d, "move_pct": round(float(ret5.iloc[i] * 100), 2),
                     "btc_pct": round(float(b * 100), 2) if b == b else 0.0,
                     "market_wide": bool(b == b and abs(b) >= abs(ret5.iloc[i]) / 3),
                     "fwd15": float(c.iloc[i + 15] / c.iloc[i] - 1) * d, "fwd60": float(c.iloc[i + 60] / c.iloc[i] - 1) * d,
                     "fwd240": float(c.iloc[i + 240] / c.iloc[i] - 1) * d})
    return pd.DataFrame(rows)


def leg(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, i: int, side: int, delay: int, hold: int,
        stop: float | None, cost: float) -> tuple[float, str] | None:
    e = i + delay
    if e + hold >= len(c):
        return None
    entry = c[e] * (1 + side * cost)
    if stop is not None:
        sp = entry * (1 - side * stop)
        hh, ll, oo = h[e + 1:e + hold + 1], l[e + 1:e + hold + 1], o[e + 1:e + hold + 1]
        crossed = np.flatnonzero(ll <= sp) if side > 0 else np.flatnonzero(hh >= sp)
        if len(crossed):
            j = crossed[0]
            fill = min(oo[j], sp) if side > 0 else max(oo[j], sp)
            return side * (fill * (1 - side * cost) / entry - 1), "stop"
    return side * (c[e + hold] * (1 - side * cost) / entry - 1), "hold"


def _stats(L: pd.DataFrame) -> dict:
    x = L["bps"].values
    half = L["ts"].median()
    first, second = L[L.ts <= half]["bps"], L[L.ts > half]["bps"]
    by_coin = L.groupby("symbol")["bps"].agg(["sum", "count"])
    by_coin = by_coin[by_coin["count"] >= 5]
    t = float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0
    out = {"events": int(len(x)), "win_pct": round(float((x > 0).mean() * 100), 1), "avg_bps": round(float(x.mean()), 1),
           "median_bps": round(float(np.median(x)), 1), "t": round(t, 2),
           "first_half": round(float(first.mean()), 1) if len(first) else None, "second_half": round(float(second.mean()), 1) if len(second) else None,
           "coins_positive_pct": round(float((by_coin["sum"] > 0).mean() * 100), 0) if len(by_coin) else None, "coins_judged": int(len(by_coin)),
           "worst_bps": round(float(x.min()), 0), "stops_hit_pct": round(float((L.how == "stop").mean() * 100), 1)}
    for name, m in (("up_bursts", L.dir > 0), ("down_bursts", L.dir < 0), ("market_wide", L.market_wide), ("own_move", ~L.market_wide)):
        sub = L[m]["bps"]
        out[name] = {"n": int(len(sub)), "avg_bps": round(float(sub.mean()), 1) if len(sub) else None}
    return out


def passes(v: dict) -> bool:
    return (v["events"] >= MIN_EVENTS and (v["first_half"] or 0) > 0 and (v["second_half"] or 0) > 0 and v["t"] >= 2.5
            and (v["coins_positive_pct"] or 0) >= 60)


def label(v: dict) -> str:
    return (f"≥{v['thr']:g}% {v.get('only', 'both'):4} · {v['side']} · +{v['delay']}m · hold {v['hold']}m · "
            f"stop {'none' if v['stop'] is None else f'{v['stop'] * 100:g}%'}{' · burst cost' if v.get('burst_cost') else ''}")


def run(data: dict[str, pd.DataFrame]) -> dict:
    btc = data["BTCUSDT"]
    arrays = {s: (df["o"].values, df["h"].values, df["l"].values, df["c"].values) for s, df in data.items()}
    ev_by_thr: dict[float, pd.DataFrame] = {}
    for thr in THRS:
        frames = []
        for s, df in data.items():
            e = events(df, btc, thr)
            if len(e):
                e["symbol"] = s
                frames.append(e)
        ev_by_thr[thr] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    shape = []
    for thr, E in ev_by_thr.items():
        if not len(E):
            continue
        for name, m in (("all", E.index == E.index), ("up", E.dir > 0), ("down", E.dir < 0), ("market_wide", E.market_wide), ("own_move", ~E.market_wide)):
            sub = E[m]
            if len(sub):
                shape.append({"thr": thr, "bursts": name, "n": int(len(sub)), "per_day": round(len(sub) / DAYS, 1),
                              "avg_move_pct": round(float(sub.move_pct.abs().mean()), 2),
                              "fwd15_bps": round(float(sub.fwd15.mean() * 1e4), 1), "fwd60_bps": round(float(sub.fwd60.mean() * 1e4), 1),
                              "fwd240_bps": round(float(sub.fwd240.mean() * 1e4), 1),
                              "continued_60_pct": round(float((sub.fwd60 > 0).mean() * 100), 0)})
    variants = []
    for thr, only, (side_name, side), delay, hold, stop in itertools.product(THRS, ONLY, SIDES.items(), DELAYS, HOLDS, STOPS):
        E = ev_by_thr[thr]
        if only == "up":
            E = E[E.dir > 0]
        elif only == "down":
            E = E[E.dir < 0]
        if not len(E):
            continue
        for burst_cost in ((False, True) if delay == 0 else (False,)):
            cost = COST * (2 if burst_cost else 1)
            legs = []
            for r in E.itertuples():
                o, h, l, c = arrays[r.symbol]
                res = leg(o, h, l, c, r.i, side * r.dir, delay, hold, stop, cost)
                if res:
                    legs.append({"ts": r.ts, "symbol": r.symbol, "dir": r.dir, "market_wide": r.market_wide, "bps": res[0] * 1e4, "how": res[1]})
            if len(legs) < 20:
                continue
            v = {"thr": thr, "only": only, "side": side_name, "delay": delay, "hold": hold, "stop": stop, "burst_cost": burst_cost, **_stats(pd.DataFrame(legs))}
            v["passes"] = passes(v)
            v["post_hoc"] = only != "both"
            variants.append(v)
    variants.sort(key=lambda v: -v["avg_bps"])
    candidates = [v for v in variants if v["passes"] and not v["post_hoc"]]
    post_hoc = [v for v in variants if v["passes"] and v["post_hoc"]]
    return {"generated_at": time.time(), "coins": sorted(data), "days": DAYS, "cost_each_way": COST, "sigma_x": SIGMA_X, "vol_x": VOL_X,
            "cooldown_min": COOLDOWN_MIN, "events_by_thr": {str(k): int(len(v)) for k, v in ev_by_thr.items()},
            "shape": shape, "variants": variants, "candidates": candidates, "post_hoc_passes": post_hoc,
            "verdict": ("candidate(s) for a paper book: " + "; ".join(label(v) for v in candidates[:5]) if candidates
                        else "no pre-registered rule passes: nothing here pays for the trade in both halves across coins")
            + (f"; {len(post_hoc)} post-hoc side-split rule(s) pass the same bar and would need their own forward test: "
               + "; ".join(label(v) for v in post_hoc[:5]) if post_hoc else "; no side-split rule passes either")}


def report(rep: dict) -> None:
    print(f"\n{len(rep['coins'])} coins · {rep['days']} days · cost {rep['cost_each_way'] * 1e4:.0f} bps each way · events {rep['events_by_thr']}\n")
    print("Shape after a burst (no rule, no costs; signed in the burst's direction, so + = continued):")
    for s in rep["shape"]:
        print(f"  ≥{s['thr']:g}% {s['bursts']:12} n={s['n']:5} ({s['per_day']}/day) avg move {s['avg_move_pct']:.2f}%  "
              f"+15m {s['fwd15_bps']:+7.1f}  +60m {s['fwd60_bps']:+7.1f}  +240m {s['fwd240_bps']:+7.1f} bps  continued at 60m {s['continued_60_pct']:.0f}%")
    print(f"\n{'variant':62} {'n':>5} {'win%':>5} {'avg':>7} {'med':>7} {'t':>5} {'1st':>7} {'2nd':>7} {'coins+':>6} {'stops':>5} {'up':>12} {'down':>12} {'own':>12}")
    for v in rep["variants"][:30]:
        print(f"{label(v):62} {v['events']:5} {v['win_pct']:5} {v['avg_bps']:7.1f} {v['median_bps']:7.1f} {v['t']:5.2f} {v['first_half'] or 0:7.1f} "
              f"{v['second_half'] or 0:7.1f} {v['coins_positive_pct'] or 0:6.0f} {v['stops_hit_pct']:5} "
              f"{str(v['up_bursts']['n']) + ' @ ' + str(v['up_bursts']['avg_bps']):>12} {str(v['down_bursts']['n']) + ' @ ' + str(v['down_bursts']['avg_bps']):>12} "
              f"{str(v['own_move']['n']) + ' @ ' + str(v['own_move']['avg_bps']):>12}{'  PASS (post-hoc)' if v['passes'] and v['post_hoc'] else '  PASS' if v['passes'] else ''}")
    print("\nVerdict:", rep["verdict"])


async def main() -> dict:
    syms = await universe()
    print(f"universe: {len(syms)} crypto perps by 24h volume", flush=True)
    data = await load(syms)
    rep = run(data)
    json.dump(rep, open(RESULTS, "w"), indent=1, default=str)
    report(rep)
    return rep


if __name__ == "__main__":
    asyncio.run(main())
