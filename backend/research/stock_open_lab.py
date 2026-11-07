"""Stock perps into the cash open — does the overnight move on a stock perp carry through the first hour, or fade?

Why (IMF, 2026, on tokenized equities: 87–99% of the overnight token move is reflected in the underlying's first five
minutes; Bitquery, Nov 2026: weekend token moves called 54% of Monday opens against a 55% base rate of "up"): the
perp prices the gap before the exchange opens. The open question is what happens next — continuation or fade — and
nobody has measured it on Binance's stock perps, which trade through the night the exchange is shut.

Run: venv/bin/python -m research.stock_open_lab   (cached 15m stock-perp candles, 68 names, Apr–Oct 2026; writes
research_cache/stock_open_results.json)

  gap       the 09:30 ET open against the prior session's 16:00 ET close, on the perp
  rules     "follow": take the gap's direction at the 09:30 bar's close (the first bar is let print), exit at EXIT
            (10:00, 10:30, 16:00 ET); "fade": the opposite. Only gaps of at least MIN_GAP (0.5%, 1%, 2%).
  costs     16 bps a round trip
  judging   the first half of the sessions chooses rule × EXIT × MIN_GAP; the second half judges once with a 95%
            block bootstrap on per-session equal-weight returns. Index ETFs (SPY, QQQ, IWM) are reported apart.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

KLINES = os.path.join(CACHE_DIR, "stock_klines15m.pkl")
OUT = os.path.join(CACHE_DIR, "stock_open_results.json")
ET = "America/New_York"
COST = 0.0016
EXITS = ("10:00", "10:30", "16:00")
MIN_GAPS = (0.005, 0.01, 0.02)
INDEX_ETFS = ("SPYUSDT", "QQQUSDT", "IWMUSDT")


def sessions(df: pd.DataFrame) -> pd.DataFrame:
    """One row per cash session: prior close (16:00 ET), the 09:30 bar's open and close, and the closes at each exit."""
    d = df.copy()
    d.index = d.index.tz_convert(ET)
    d["day"] = d.index.date
    d["hm"] = d.index.strftime("%H:%M")
    out, prev_close = [], None
    for day, g in d.groupby("day"):
        if pd.Timestamp(day).weekday() >= 5:
            continue
        bars = g.set_index("hm")
        if "09:30" in bars.index and "15:45" in bars.index and prev_close is not None:
            row = {"day": str(day), "prev_close": prev_close, "open": float(bars.loc["09:30", "open"]), "first_close": float(bars.loc["09:30", "close"])}
            for ex in EXITS:
                key = "15:45" if ex == "16:00" else ex                       # the 16:00 exit is the last bar's close
                row[f"exit_{ex}"] = float(bars.loc[key, "close"]) if key in bars.index else np.nan
            out.append(row)
        if "15:45" in bars.index:
            prev_close = float(bars.loc["15:45", "close"])
    return pd.DataFrame(out)


def trades(ss: pd.DataFrame, rule: str, exit_: str, min_gap: float) -> pd.DataFrame:
    gap = ss["open"] / ss["prev_close"] - 1
    keep = gap.abs() >= min_gap
    s = np.sign(gap) * (1 if rule == "follow" else -1)
    ret = s * (ss[f"exit_{exit_}"] / ss["first_close"] - 1) - COST
    df = pd.DataFrame({"day": ss["day"], "ret": ret, "gap": gap})[keep]
    return df.dropna()


def _ci(v: np.ndarray, seed=1, block=4, boot=2000) -> tuple[float, float]:
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
    daily = tr.groupby("day")["ret"].mean()
    lo, hi = _ci(daily.values)
    return {"trades": int(len(tr)), "sessions": int(len(daily)), "per_trade_pct": round(float(tr["ret"].mean() * 100), 3),
            "per_session_pct": round(float(daily.mean() * 100), 3), "ci95_session_pct": [round(lo * 100, 3), round(hi * 100, 3)],
            "win_pct": round(float((tr["ret"] > 0).mean() * 100), 1)}


def main() -> dict:
    kl = pd.read_pickle(KLINES)
    ss = {s: sessions(df) for s, df in kl.items()}
    ss = {s: v for s, v in ss.items() if len(v) >= 40}
    all_days = sorted({d for v in ss.values() for d in v["day"]})
    split = all_days[len(all_days) // 2]
    res = {"names": len(ss), "sessions": len(all_days), "period": [all_days[0], all_days[-1]], "split": split, "cost_bps": COST * 1e4}
    # descriptive: gap size and the raw continuation by gap bucket (first hour), whole period
    allg = pd.concat([pd.DataFrame({"gap": v["open"] / v["prev_close"] - 1, "fh": (v["exit_10:30"] / v["first_close"] - 1)}) for v in ss.values()]).dropna()
    allg["bucket"] = pd.cut(allg["gap"].abs(), [0, 0.005, 0.01, 0.02, 1], labels=["<0.5%", "0.5–1%", "1–2%", ">2%"])
    res["by_gap_bucket"] = {str(b): {"n": int(len(g)), "first_hour_in_gap_direction_bps": round(float((np.sign(g["gap"]) * g["fh"]).mean() * 1e4), 1)}
                            for b, g in allg.groupby("bucket", observed=True)}
    for universe, names in (("stocks", [s for s in ss if s not in INDEX_ETFS]), ("index_etfs", [s for s in ss if s in INDEX_ETFS])):
        best, grid = None, []
        for rule in ("follow", "fade"):
            for ex in EXITS:
                for mg in MIN_GAPS:
                    tr = pd.concat([trades(ss[s], rule, ex, mg) for s in names], ignore_index=True)
                    a, b = tr[tr["day"] < split], tr[tr["day"] >= split]
                    g = {"rule": rule, "exit": ex, "min_gap": mg, "first_half": summarize(a), "second_half": summarize(b)}
                    grid.append(g)
                    if g["first_half"].get("trades", 0) >= 30 and (best is None or g["first_half"]["per_session_pct"] > best["first_half"]["per_session_pct"]):
                        best = g
        res[universe] = {"chosen": best, "verdict": "passes" if best and best["second_half"].get("ci95_session_pct", [0])[0] > 0 else "no edge", "grid": grid}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print("names", r["names"], "sessions", r["sessions"], r["period"], "split", r["split"])
    print("first hour in the gap's direction, by gap size:", r["by_gap_bucket"])
    for u in ("stocks", "index_etfs"):
        v = r[u]
        for g in sorted(v["grid"], key=lambda g: -(g["first_half"].get("per_session_pct") or -9))[:6]:
            print(f"[{u}] {g['rule']:6} exit {g['exit']} gap≥{g['min_gap']:.3f} | first {g['first_half'].get('per_session_pct')}%/session ({g['first_half'].get('trades')}) "
                  f"| second {g['second_half'].get('per_session_pct')}% ci {g['second_half'].get('ci95_session_pct')}")
        print(f"[{u}] chosen", {k: v["chosen"][k] for k in ("rule", "exit", "min_gap")} if v["chosen"] else None, "→", v["verdict"])
