"""The quarter-hour clock — does order imbalance in the minute after :00/:15/:30/:45 predict the next hours?

Why (Kim & Hansen 2026, arXiv 2607.09426): on six Binance perps, volatility and volume burst at the hour, five-minute
and quarter-hour marks (periodic algorithmic execution), and the order imbalance at those marks predicts returns 4–12
hours ahead; a practitioner's replication found the pattern real but no trade after costs. We have no trade-level
feed for history, so the imbalance is the minute bar's taker-buy share: (taker buy − taker sell) / volume.

Run: venv/bin/python -m research.quarter_hour_lab   (fetches DAYS days of 1m klines with taker volume for the six
coins, cached under research_cache/qh/; writes research_cache/quarter_hour_results.json)

  signal    the imbalance of the minute bar that opens at a quarter-hour mark (and, as the control, at the
            off-mark minutes :07, :22, :37, :52 — if the clock matters, the marks should predict and the controls not)
  event     forward return from the mark minute's close over 1h, 4h, 8h, 12h, signed by the imbalance; the IC
            (rank correlation of imbalance and forward return) and the mean signed return by imbalance tercile
  rule      long/short in the imbalance's direction when |imbalance| ≥ MIN_IMB (0.2, 0.4), one position per coin at a
            time, held HOLD hours (4, 8); costs 16 bps a round trip
  judging   first half of the days chooses MIN_IMB × HOLD; the second half judges once (95% block bootstrap).
"""
from __future__ import annotations

import json
import os
import random
import time

import httpx
import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

CACHE = os.path.join(CACHE_DIR, "qh")
OUT = os.path.join(CACHE_DIR, "quarter_hour_results.json")
FAPI = "https://fapi.binance.com/fapi/v1"
COINS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT")
DAYS = 60
COST = 0.0016
MARKS = (0, 15, 30, 45)
CONTROLS = (7, 22, 37, 52)
HORIZONS_H = (1, 4, 8, 12)
MIN_IMBS = (0.2, 0.4)
HOLDS = (4, 8)


def load(c: httpx.Client, sym: str) -> pd.DataFrame:
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{sym}_1m_{DAYS}d.pkl")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 24 * 3600:
        return pd.read_pickle(p)
    end = int(time.time() // 60 * 60 * 1000)
    t, rows = end - DAYS * 86400 * 1000, []
    while t < end:
        try:
            r = c.get(f"{FAPI}/klines", params={"symbol": sym, "interval": "1m", "startTime": t, "endTime": end, "limit": 1500}, timeout=30).json()
        except Exception:
            time.sleep(1)
            continue
        if not isinstance(r, list) or not r:
            break
        rows += r
        t = int(r[-1][0]) + 60_000
        if len(r) < 1500:
            break
        time.sleep(0.1)
    df = pd.DataFrame({"o": [float(k[1]) for k in rows], "c": [float(k[4]) for k in rows], "v": [float(k[5]) for k in rows],
                       "buy": [float(k[9]) for k in rows]}, index=pd.to_datetime([k[0] for k in rows], unit="ms", utc=True))
    df = df[~df.index.duplicated()].sort_index()
    pd.to_pickle(df, p)
    return df


def events(df: pd.DataFrame, minutes: tuple) -> pd.DataFrame:
    """One row per mark minute: the imbalance of that bar and the forward returns from its close."""
    m = df[df.index.minute.isin(minutes)].copy()
    m["imb"] = np.where(m["v"] > 0, (2 * m["buy"] - m["v"]) / m["v"].replace(0, np.nan), np.nan)
    c = df["c"]
    for h in HORIZONS_H:
        fwd = c.reindex(m.index + pd.Timedelta(hours=h))
        m[f"fwd_{h}h"] = fwd.values / m["c"].values - 1
    return m.dropna(subset=["imb"])


def ic_table(ev: pd.DataFrame) -> dict:
    out = {}
    for h in HORIZONS_H:
        col = ev[[f"fwd_{h}h", "imb"]].dropna()
        if len(col) < 50:
            continue
        ic = col["imb"].rank().corr(col[f"fwd_{h}h"].rank())
        signed = np.sign(col["imb"]) * col[f"fwd_{h}h"]
        terc = pd.qcut(col["imb"], 3, labels=["sell", "flat", "buy"])
        out[f"{h}h"] = {"n": int(len(col)), "ic": round(float(ic), 4), "signed_bps": round(float(signed.mean() * 1e4), 2),
                        "t": round(float(signed.mean() / (signed.std(ddof=1) / np.sqrt(len(signed)))), 2),
                        "by_tercile_bps": {str(k): round(float(v.mean() * 1e4), 2) for k, v in col[f"fwd_{h}h"].groupby(terc, observed=True)}}
    return out


def rule_trades(df: pd.DataFrame, ev: pd.DataFrame, min_imb: float, hold_h: int) -> pd.DataFrame:
    """Non-overlapping: a position opens at a mark whose |imbalance| clears the bar, nothing else opens until it closes."""
    c = df["c"]
    out, busy_until = [], None
    for ts, r in ev.iterrows():
        if busy_until is not None and ts < busy_until:
            continue
        if abs(r["imb"]) < min_imb:
            continue
        exit_ts = ts + pd.Timedelta(hours=hold_h)
        px = c.get(exit_ts)
        if px is None or np.isnan(px):
            continue
        s = 1 if r["imb"] > 0 else -1
        out.append((ts, s * (px / r["c"] - 1) - COST))
        busy_until = exit_ts
    return pd.DataFrame(out, columns=["ts", "ret"])


def _ci(v: np.ndarray, seed=1, block=6, boot=2000) -> tuple[float, float]:
    rng, n, means = random.Random(seed), len(v), []
    if n == 0:
        return 0.0, 0.0
    for _ in range(boot):
        ix: list[int] = []
        while len(ix) < n:
            st = rng.randrange(0, max(n - block, 1)); ix += range(st, st + block)
        means.append(v[ix[:n]].mean())
    means.sort()
    return means[int(boot * .025)], means[int(boot * .975)]


def summarize(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0}
    lo, hi = _ci(tr["ret"].values)
    return {"trades": int(len(tr)), "per_trade_bps": round(float(tr["ret"].mean() * 1e4), 2), "ci95_bps": [round(lo * 1e4, 2), round(hi * 1e4, 2)],
            "win_pct": round(float((tr["ret"] > 0).mean() * 100), 1), "sum_pct": round(float(tr["ret"].sum() * 100), 2)}


def main() -> dict:
    data = {}
    with httpx.Client() as c:
        for s in COINS:
            data[s] = load(c, s)
    idx = data["BTCUSDT"].index
    split = idx[len(idx) // 2]
    res = {"coins": list(COINS), "days": DAYS, "period": [str(idx[0]), str(idx[-1])], "split": str(split), "cost_bps": COST * 1e4}
    # the clock itself: volume and |return| at the marks vs other minutes
    allm = pd.concat([pd.DataFrame({"min": d.index.minute, "v": d["v"] / d["v"].mean(), "absret": (d["c"] / d["o"] - 1).abs()}) for d in data.values()])
    by_min = allm.groupby("min").agg(v=("v", "mean"), absret=("absret", "mean"))
    res["clock"] = {"marks_volume_x": round(float(by_min.loc[list(MARKS), "v"].mean() / by_min.drop(list(MARKS))["v"].mean()), 3),
                    "marks_absret_x": round(float(by_min.loc[list(MARKS), "absret"].mean() / by_min.drop(list(MARKS))["absret"].mean()), 3),
                    "controls_volume_x": round(float(by_min.loc[list(CONTROLS), "v"].mean() / by_min.drop(list(CONTROLS))["v"].mean()), 3)}
    ev_marks = pd.concat([events(d, MARKS).assign(sym=s) for s, d in data.items()])
    ev_ctl = pd.concat([events(d, CONTROLS).assign(sym=s) for s, d in data.items()])
    res["marks"] = {"all": ic_table(ev_marks), "first_half": ic_table(ev_marks[ev_marks.index < split]), "second_half": ic_table(ev_marks[ev_marks.index >= split])}
    res["controls"] = {"all": ic_table(ev_ctl)}
    best, grid = None, []
    for mi in MIN_IMBS:
        for hold in HOLDS:
            tr = pd.concat([rule_trades(d, events(d, MARKS), mi, hold) for d in data.values()], ignore_index=True)
            a, b = tr[tr["ts"] < split], tr[tr["ts"] >= split]
            g = {"min_imb": mi, "hold_h": hold, "first_half": summarize(a), "second_half": summarize(b)}
            grid.append(g)
            if g["first_half"].get("trades", 0) >= 30 and (best is None or g["first_half"]["per_trade_bps"] > best["first_half"]["per_trade_bps"]):
                best = g
    res["rule"] = {"chosen": best, "grid": grid, "verdict": "passes" if best and best["second_half"].get("ci95_bps", [0])[0] > 0 else "no edge"}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print("period", r["period"], "clock:", r["clock"])
    for part in ("all", "first_half", "second_half"):
        print(f"marks [{part}]:", {h: (v["ic"], v["signed_bps"], v["t"]) for h, v in r["marks"][part].items()})
    print("controls [all]:", {h: (v["ic"], v["signed_bps"], v["t"]) for h, v in r["controls"]["all"].items()})
    for g in r["rule"]["grid"]:
        print(f"rule |imb|≥{g['min_imb']} hold {g['hold_h']}h | first {g['first_half'].get('per_trade_bps')} bps ({g['first_half'].get('trades')}) "
              f"| second {g['second_half'].get('per_trade_bps')} bps ci {g['second_half'].get('ci95_bps')}")
    print("→", r["rule"]["verdict"])
