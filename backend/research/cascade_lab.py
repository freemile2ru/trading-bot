"""Cascade fade lab — replay the live cascade book's rule on the recorder's own data, with its stop and without.

The cascade book (experiments/cascade.py) fades a 5-minute liquidation burst at a coin's 99th percentile, holds 4 hours,
and carries a 3% disaster stop. Its evidence (2026-10-27, 30 days, 12 coins) was a 4-hour fade with NO stop: +34 bps a
trade, 61% winners. Live (28 trades to 2026-10-30) it is 36% winners at −47 bps a trade, and the 3% stop closed 15 of
the 28 — several after the leg had been +0.6R in its favour. This lab asks which part is wrong: the stop, the hold,
the threshold, or the entry timing — on the recorder's 1-second bars (scalper_market.db, 2026-09-30 →), resampled
to the minute.

  threshold  the live one: per coin, the 99th percentile of trailing-30-day 5-minute buckets that had ANY liquidation
             ("nonempty"); and the same percentile counting the empty buckets too ("all"), which is a higher bar
  entry      the close of the minute after the bucket ends (live), or DELAY minutes later; taker costs each way
  stop       none / 2 / 3 / 5 / 8 % from entry, checked on the minute's high/low (a bar that gaps through fills at its open)
  hold       1 / 2 / 4 / 8 hours, then out at the close
  cooldown   one entry per coin per hour, as live

Reported per variant: events, winners %, average and median bps a trade after costs, t-stat, both halves of the sample,
stops hit %, and the long/short split. Pre-registered rule (2026-10-30, before the first run): the live rule (nonempty,
3% stop, 4h) keeps its book only if its average is positive in BOTH halves; a variant replaces it only if it beats it
on average bps in BOTH halves with at least 100 events; if nothing clears that, the book is retired.

    ./venv/bin/python -m research.cascade_lab
"""
from __future__ import annotations

import itertools
import json
import os
import sqlite3
import time

import numpy as np
import pandas as pd

from scalper import config as C
from trading import futures

CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache")
BARS = os.path.join(CACHE, "cascade_bars_1m.pkl")
RESULTS = os.path.join(CACHE, "cascade_results.json")
COST = futures.TAKER_FEE + futures.SLIPPAGE            # each way, as the paper wallet charges
BUCKET_S, QUANTILE, LOOKBACK_DAYS, COOLDOWN_S, MIN_BUCKETS = 300, 0.99, 30, 3600, 200
STOPS = [None, 0.02, 0.03, 0.05, 0.08]
HOLDS_H = [1, 2, 4, 8]
DELAYS_MIN = [0, 15, 60]
LIVE = ("nonempty", 0.03, 4, 0)


def _con() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{C.MARKET_DB}?mode=ro", uri=True, timeout=30)


def bars_1m() -> dict[str, pd.DataFrame]:
    """Per coin: minute bars (h, l, c) built from the recorder's 1-second bars; cached until the recorder moves on."""
    con = _con()
    try:
        newest = con.execute("SELECT ts FROM bars_1s WHERE symbol=? ORDER BY ts DESC LIMIT 1", (C.SYMBOLS[0],)).fetchone()[0]
        if os.path.exists(BARS):
            cached = pd.read_pickle(BARS)
            if cached.get("_newest", 0) >= newest - 3600:
                return {k: v for k, v in cached.items() if k != "_newest"}
        out: dict = {"_newest": newest}
        for sym in C.SYMBOLS:
            rows = con.execute("SELECT ts / 60 AS m, MAX(h), MIN(l), MAX(ts) FROM bars_1s WHERE symbol=? GROUP BY m", (sym,)).fetchall()
            closes = dict(con.execute("SELECT b.ts / 60, b.c FROM bars_1s b JOIN (SELECT MAX(ts) AS mts FROM bars_1s WHERE symbol=? GROUP BY ts / 60) x "
                                      "ON b.symbol=? AND b.ts = x.mts", (sym, sym)).fetchall())
            df = pd.DataFrame(rows, columns=["m", "h", "l", "last_ts"]).set_index("m")
            df["c"] = [closes.get(m) for m in df.index]
            df = df.dropna()[["h", "l", "c"]]
            df.index = df.index * 60                                   # minute start, epoch seconds
            out[sym] = df
            print(f"  {sym}: {len(df):,} minutes")
        pd.to_pickle(out, BARS)
        return {k: v for k, v in out.items() if k != "_newest"}
    finally:
        con.close()


def buckets() -> pd.DataFrame:
    """5-minute liquidation buckets per coin: longs liquidated (SELL side) and shorts liquidated (BUY side), $."""
    con = _con()
    try:
        rows = con.execute("SELECT symbol, CAST(ts / ? AS INTEGER) * ? AS b, side, SUM(notional) FROM liquidations GROUP BY symbol, b, side",
                           (BUCKET_S, BUCKET_S)).fetchall()
    finally:
        con.close()
    df = pd.DataFrame(rows, columns=["symbol", "b", "side", "usd"])
    piv = df.pivot_table(index=["symbol", "b"], columns="side", values="usd", fill_value=0.0).reset_index()
    piv = piv.rename(columns={"SELL": "longs", "BUY": "shorts"})
    for col in ("longs", "shorts"):
        if col not in piv:
            piv[col] = 0.0
    piv["total"] = piv["longs"] + piv["shorts"]
    return piv.sort_values(["symbol", "b"]).reset_index(drop=True)


def events(bk: pd.DataFrame, mode: str) -> pd.DataFrame:
    """Buckets above the coin's trailing 99th percentile (recomputed each day, as the live hourly cache would),
    one entry per coin per hour."""
    out = []
    for sym, g in bk.groupby("symbol"):
        g = g.set_index("b")
        totals = g["total"]
        first = int(totals.index.min())
        day_thr: dict[int, float | None] = {}
        last = -1e18
        for b, row in g.iterrows():
            day = int(b // 86400)
            if day not in day_thr:
                lo = b - LOOKBACK_DAYS * 86400
                hist = totals[(totals.index >= lo) & (totals.index < day * 86400)]
                if mode == "all":
                    n_all = max(int((day * 86400 - max(lo, first)) // BUCKET_S), len(hist))
                    vals = np.concatenate([np.zeros(n_all - len(hist)), hist.values])
                else:
                    vals = hist.values
                vals = np.sort(vals)
                day_thr[day] = float(vals[int(QUANTILE * len(vals)) - 1]) if len(vals) >= MIN_BUCKETS else None
            thr = day_thr[day]
            if thr is None or row["total"] <= thr or b - last < COOLDOWN_S:
                continue
            last = b
            out.append({"symbol": sym, "b": int(b), "longs": row["longs"], "shorts": row["shorts"], "thr": thr,
                        "side": 1 if row["longs"] > row["shorts"] else -1})          # fade: long after longs were flushed
    return pd.DataFrame(out)


def leg(k: pd.DataFrame, t0: int, side: int, stop: float | None, hold_h: float) -> tuple[float, str] | None:
    """Signed net return of a fade entered at the close of minute t0, walked minute by minute."""
    if t0 not in k.index:
        return None
    i = k.index.get_loc(t0)
    end_ts = t0 + int(hold_h * 3600)
    entry = k["c"].iloc[i] * (1 + side * COST)
    stop_px = entry * (1 - side * stop) if stop else None
    j = i + 1
    n = len(k)
    while j < n and k.index[j] <= end_ts:
        h, l = k["h"].iloc[j], k["l"].iloc[j]
        if stop_px is not None and ((l <= stop_px) if side > 0 else (h >= stop_px)):
            prev_c = k["c"].iloc[j - 1]
            fill = min(stop_px, prev_c) if side > 0 else max(stop_px, prev_c)      # a gap through the stop fills worse
            return side * (fill * (1 - side * COST) / entry - 1), "stop"
        j += 1
    if j - 1 <= i or k.index[j - 1] < end_ts - 600:
        return None                                                               # the path isn't recorded to the end
    exit_px = k["c"].iloc[j - 1] * (1 - side * COST)
    return side * (exit_px / entry - 1), "hold"


def simulate(ev: pd.DataFrame, bars: dict[str, pd.DataFrame], stop, hold_h, delay_min) -> dict | None:
    legs = []
    for r in ev.itertuples():
        k = bars.get(r.symbol)
        if k is None:
            continue
        t0 = r.b + BUCKET_S + 60 * (1 + delay_min)                                 # the minute after the bucket closes (+ delay)
        res = leg(k, t0 - 60, r.side, stop, hold_h)
        if res is None:
            continue
        legs.append({"ts": t0, "symbol": r.symbol, "side": r.side, "bps": res[0] * 1e4, "how": res[1]})
    if len(legs) < 20:
        return None
    L = pd.DataFrame(legs).sort_values("ts")
    half = len(L) // 2
    x = L["bps"].values
    t = float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0
    longs, shorts = L[L.side > 0], L[L.side < 0]
    return {"stop": stop, "hold_h": hold_h, "delay_min": delay_min, "events": int(len(L)),
            "win_pct": round(float((x > 0).mean() * 100), 1), "avg_bps": round(float(x.mean()), 1), "median_bps": round(float(np.median(x)), 1),
            "t": round(t, 2), "first_half": round(float(x[:half].mean()), 1), "second_half": round(float(x[half:].mean()), 1),
            "worst_bps": round(float(x.min()), 0), "stops_hit_pct": round(float((L.how == "stop").mean() * 100), 1),
            "longs": {"n": int(len(longs)), "avg_bps": round(float(longs.bps.mean()), 1) if len(longs) else None},
            "shorts": {"n": int(len(shorts)), "avg_bps": round(float(shorts.bps.mean()), 1) if len(shorts) else None},
            "per_day": round(len(L) / max((L.ts.max() - L.ts.min()) / 86400, 1), 1)}


def label(v: dict) -> str:
    return f"{v['mode']:8} " + (f"stop {int(v['stop'] * 100)}%" if v["stop"] else "no stop ") + f" · hold {v['hold_h']}h · +{v['delay_min']}m"


def main() -> dict:
    t0 = time.time()
    bars = bars_1m()
    bk = buckets()
    variants = []
    for mode in ("nonempty", "all"):
        ev = events(bk, mode)
        if ev.empty:
            continue
        grid = itertools.product(STOPS, HOLDS_H, [0]) if mode == "nonempty" else [(0.03, 4, 0), (None, 4, 0)]
        for stop, hold_h, delay in grid:
            v = simulate(ev, bars, stop, hold_h, delay)
            if v:
                variants.append({"mode": mode, **v})
        if mode == "nonempty":
            for delay in DELAYS_MIN[1:]:
                for stop in (0.03, None):
                    v = simulate(ev, bars, stop, 4, delay)
                    if v:
                        variants.append({"mode": mode, **v})
    live = next((v for v in variants if (v["mode"], v["stop"], v["hold_h"], v["delay_min"]) == LIVE), None)
    keep_live = bool(live and live["first_half"] > 0 and live["second_half"] > 0)
    better = [v for v in variants if live and v is not live and v["events"] >= 100
              and v["first_half"] > live["first_half"] and v["second_half"] > live["second_half"] and v["avg_bps"] > 0]
    chosen = max(better, key=lambda v: v["avg_bps"]) if better else None
    if chosen and not (chosen["first_half"] > 0 and chosen["second_half"] > 0):
        chosen = None
    verdict = (f"replace the live rule with {label(chosen)}" if chosen else
               "keep the live rule" if keep_live else "retire the cascade book — the live rule is not positive in both halves and no variant clears the bar")
    report = {"coins": len(bars), "days": round((max(k.index.max() for k in bars.values()) - min(k.index.min() for k in bars.values())) / 86400, 1),
              "cost_each_way": COST, "live": live, "variants": sorted(variants, key=lambda v: -v["avg_bps"]), "chosen": chosen,
              "verdict": verdict, "rule": "live rule stays only if positive in both halves; a replacement must beat it in both halves with ≥100 events",
              "seconds": round(time.time() - t0)}
    json.dump(report, open(RESULTS, "w"), indent=1, default=str)
    return report


if __name__ == "__main__":
    rep = main()
    print(f"\n{rep['coins']} coins, {rep['days']} days, cost {rep['cost_each_way'] * 1e4:.0f} bps each way. Live rule = nonempty · stop 3% · hold 4h\n")
    print(f"{'variant':40} {'n':>5} {'/day':>5} {'win%':>5} {'avg':>7} {'med':>7} {'t':>5} {'1st':>7} {'2nd':>7} {'worst':>6} {'stop%':>5} {'long':>12} {'short':>12}")
    for v in rep["variants"]:
        print(f"{label(v):40} {v['events']:5} {v['per_day']:5} {v['win_pct']:5} {v['avg_bps']:7.1f} {v['median_bps']:7.1f} {v['t']:5.2f} {v['first_half']:7.1f} "
              f"{v['second_half']:7.1f} {v['worst_bps']:6.0f} {v['stops_hit_pct']:5} {str(v['longs']['n']) + ' @ ' + str(v['longs']['avg_bps']):>12} "
              f"{str(v['shorts']['n']) + ' @ ' + str(v['shorts']['avg_bps']):>12}")
    print(f"\nVerdict: {rep['verdict']}\nWritten to {RESULTS} in {rep['seconds']}s")
