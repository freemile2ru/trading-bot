"""Signal scoring — does anything the scanner reads predict what price does next?

Trades are few (a handful a week per system), so judging the scanner by its trades takes months.
Its READINGS are many: every sweep records each coin's conviction, scores and features
(signals.db scan_features — thousands a week). This labels each reading with what the
perpetual actually did over the next 4 h, 24 h and 72 h (and that move minus BTC's), then asks:

  * information coefficient (IC) — rank correlation between an input and the next move,
    computed per UTC day then averaged (readings overlap, so pooling them would overstate
    the evidence); t-stat across days. |IC| ≈ 0.03+ with t > 2 would be a usable signal.
  * conviction buckets — average move in the conviction's direction by conviction strength,
    against the ~16 bps it costs to trade in and out
  * Claude's picks — readings on coins where Claude fired a signal, vs all the others

Runs from the scheduler every hour (label_pending) and on demand:
    ./venv/bin/python -m research.signal_scores          # label what's due, then print the scores
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict

import pandas as pd

from storage import signals as signal_log

logger = logging.getLogger("research.signal_scores")

HORIZONS = {"4h": 4 * 3600, "24h": 24 * 3600, "72h": 72 * 3600}
ROUND_TRIP_BPS = 16.0            # taker 0.05% + slippage 0.03%, each way
MIN_PER_DAY = 10                 # readings needed on a day for that day's IC to count
# Before this many days, one market swing dominates everything (2 days gave "t = 305") — the
# numbers are shown as "collecting" and no t-stat is reported.
MIN_DAYS = 14
BUCKETS = [(0.2, "under 0.2"), (0.35, "0.2 – 0.35"), (0.45, "0.35 – 0.45"), (float("inf"), "0.45 and up")]
INPUTS = ["conviction", "composite", "ta_score", "fa_score"]


# ── Labelling ──────────────────────────────────────────────────────────────────

async def _hourly_closes(symbol: str, start: float, end: float) -> dict[int, float]:
    """Perpetual 1h closes keyed by the bar's open time (epoch seconds)."""
    from data.derivatives_client import fetch_klines_range
    rows = await fetch_klines_range(symbol, "1h", int(start * 1000), int(end * 1000))
    return {r[0] // 1000: r[4] for r in rows}


def _close_before(closes: dict[int, float], t: float) -> float | None:
    """The last completed hourly close at time t."""
    bar = int(t // 3600) * 3600 - 3600
    return closes.get(bar) or closes.get(bar - 3600)


async def label_pending() -> int:
    """Label every reading whose horizons have passed. Returns the number of rows written."""
    now = time.time()
    pending = signal_log.unlabelled_scans(ready_before=now - HORIZONS["4h"] - 3600)
    if not pending:
        return 0
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in pending:
        by_symbol[r["symbol"]].append(r)
    lo, hi = min(r["ts"] for r in pending) - 7200, now
    btc = await _hourly_closes("BTCUSDT", lo, hi)
    written = 0
    for symbol, rows in by_symbol.items():
        try:
            closes = btc if symbol == "BTCUSDT" else await _hourly_closes(symbol, min(r["ts"] for r in rows) - 7200, hi)
        except Exception as e:
            logger.warning("Labelling %s failed: %s", symbol, e)
            continue
        out = []
        for r in rows:
            p0, b0 = _close_before(closes, r["ts"]), _close_before(btc, r["ts"])
            if not p0:
                continue
            vals = {}
            for name, h in HORIZONS.items():
                t = r["ts"] + h
                p, b = (_close_before(closes, t), _close_before(btc, t)) if t <= now - 3600 else (None, None)
                vals[name] = (p / p0 - 1) * 100 if p else None
                vals["xs_" + name] = (vals[name] - (b / b0 - 1) * 100) if p and b and b0 else None
            out.append((r["id"], symbol, r["ts"], p0, vals["4h"], vals["24h"], vals["72h"],
                        vals["xs_4h"], vals["xs_24h"], vals["xs_72h"]))
        signal_log.save_labels(out)
        written += len(out)
    return written


# ── Scoring ────────────────────────────────────────────────────────────────────

def _ic(df: pd.DataFrame, col: str, target: str) -> dict:
    """Mean of daily Spearman correlations, with a t-stat across days. Vectorised: rank within each day, then
    each day's Pearson correlation of the ranks from group sums (was a Python loop over days per input)."""
    d = df[["day", col, target]].dropna()
    if d.empty:
        return {"days": 0}
    g = d.groupby("day")
    ok = (g[col].transform("size") >= MIN_PER_DAY) & (g[col].transform("nunique") > 2)
    d = d[ok]
    if d.empty:
        return {"days": 0}
    r = d.groupby("day")[[col, target]].rank()
    r["day"] = d["day"].values
    r["xy"], r["xx"], r["yy"] = r[col] * r[target], r[col] ** 2, r[target] ** 2
    s_ = r.groupby("day").agg(n=(col, "size"), x=(col, "sum"), y=(target, "sum"), xy=("xy", "sum"), xx=("xx", "sum"), yy=("yy", "sum"))
    cov = s_["xy"] - s_["x"] * s_["y"] / s_["n"]
    vx, vy = s_["xx"] - s_["x"] ** 2 / s_["n"], s_["yy"] - s_["y"] ** 2 / s_["n"]
    daily = (cov / (vx * vy) ** 0.5).replace([float("inf"), float("-inf")], float("nan")).dropna().tolist()
    if len(daily) < 2:
        return {"days": len(daily)}
    m = sum(daily) / len(daily)
    sd = math.sqrt(sum((x - m) ** 2 for x in daily) / (len(daily) - 1))
    t = round(m / (sd / math.sqrt(len(daily))), 2) if sd and len(daily) >= MIN_DAYS else None
    return {"ic": round(m, 4), "t": t, "days": len(daily)}


def _near_fired(df: pd.DataFrame, fired: list[tuple[str, float]], window: float = 4 * 3600) -> pd.Series:
    """Readings taken within `window` after a fired signal on the same coin. Sorted lookup per coin — the
    row-by-row version compared every reading with every signal and took ~110 s at 64k readings."""
    import numpy as np
    by: dict[str, list[float]] = {}
    for sym, t in fired:
        by.setdefault(sym, []).append(t)
    mask = pd.Series(False, index=df.index)
    for sym, g in df.groupby("symbol"):
        times = np.array(sorted(by.get(sym, [])))
        if not len(times):
            continue
        ts = g["ts"].to_numpy(dtype=float)
        i = np.searchsorted(times, ts, side="right") - 1
        ok = i >= 0
        prev = np.where(ok, times[np.clip(i, 0, None)], -np.inf)
        mask.loc[g.index] = ok & (ts - prev <= window)
    return mask


def scores(since: float = 0) -> dict:
    rows = signal_log.labelled_scans(since)
    out: dict = {"readings": len(rows), "round_trip_bps": ROUND_TRIP_BPS, "generated_at": time.time(), "min_days": MIN_DAYS}
    if not rows:
        return {**out, "horizons": {}}
    flat = []
    for r in rows:
        f = r.pop("features") or {}
        flat.append({**r, **{k: v for k, v in f.items() if isinstance(v, (int, float)) and not isinstance(v, bool)},
                     "regime": f.get("regime"), "setups": f.get("setups") or []})
    df = pd.DataFrame(flat)
    df["day"] = (df["ts"] // 86400).astype("int64")             # UTC day number — integer keys group fast
    out["days"] = int(df["day"].nunique())
    out["period"] = [float(df["ts"].min()), float(df["ts"].max())]
    fired = [(f["symbol"], f["fired_at"]) for f in signal_log.recent_fired(10 ** 9, 10 ** 6)]
    feature_cols = [c for c in df.columns if c not in {"id", "ts", "symbol", "day", "regime", "setups",
                                                        "fwd_4h", "fwd_24h", "fwd_72h", "xs_4h", "xs_24h", "xs_72h"}
                    and pd.api.types.is_numeric_dtype(df[c])]
    horizons = {}
    for h in HORIZONS:
        target, excess = f"fwd_{h}", f"xs_{h}"
        labelled = df.dropna(subset=[target])
        if labelled.empty:
            continue
        sign = labelled["conviction"].apply(lambda c: 1 if c > 0 else -1 if c < 0 else 0)
        signed_bps = labelled[target] * sign * 100          # % → bps, in the conviction's direction
        buckets = []
        for (limit, label), prev in zip(BUCKETS, [0.0] + [b[0] for b in BUCKETS[:-1]]):
            m = labelled["conviction"].abs().between(prev, limit, inclusive="left")
            s = signed_bps[m]
            if len(s):
                buckets.append({"bucket": label, "n": int(len(s)), "avg_bps": round(float(s.mean()), 1),
                                "hit_pct": round(float((s > 0).mean() * 100), 1),
                                "after_costs_bps": round(float(s.mean()) - ROUND_TRIP_BPS, 1)})
        inputs = []
        for col in [c for c in INPUTS if c in feature_cols] + sorted(set(feature_cols) - set(INPUTS)):
            ic, xic = _ic(labelled, col, target), _ic(labelled, col, excess)
            if ic.get("days", 0) >= 2:
                inputs.append({"input": col, **ic, "ic_vs_btc": xic.get("ic"), "t_vs_btc": xic.get("t")})
        inputs.sort(key=lambda x: (-(abs(x["t"]) if x.get("t") is not None else -1), -abs(x.get("ic") or 0)))
        by_regime = []
        for reg, g in labelled.assign(signed=signed_bps).groupby("regime"):
            by_regime.append({"regime": reg, "n": int(len(g)), "avg_bps": round(float(g["signed"].mean()), 1),
                              "hit_pct": round(float((g["signed"] > 0).mean() * 100), 1)})
        claude = None
        if fired:
            near = _near_fired(labelled, fired)
            if near.any():
                claude = {"fired_n": int(near.sum()), "fired_avg_bps": round(float(signed_bps[near].mean()), 1),
                          "other_n": int((~near).sum()), "other_avg_bps": round(float(signed_bps[~near].mean()), 1)}
        horizons[h] = {"n": int(len(labelled)), "days": int(labelled["day"].nunique()), "buckets": buckets,
                       "inputs": inputs[:25], "by_regime": by_regime, "claude": claude}
    out["horizons"] = horizons
    return out


if __name__ == "__main__":
    n = asyncio.run(label_pending())
    s = scores()
    print(f"Labelled {n} new readings; {s['readings']} labelled over {s.get('days', 0)} days.")
    for h, d in s["horizons"].items():
        print(f"\n■ {h}: {d['n']} readings over {d['days']} days")
        for b in d["buckets"]:
            print(f"  conviction {b['bucket']:>12}: {b['avg_bps']:+6.1f} bps in its direction ({b['hit_pct']}% right, n={b['n']}), "
                  f"{b['after_costs_bps']:+.1f} after costs")
        for i in d["inputs"][:8]:
            print(f"  IC {i['input']:<26} {i.get('ic', 0):+.3f} (t {i.get('t')}, {i['days']} days) · vs BTC {i.get('ic_vs_btc')}")
