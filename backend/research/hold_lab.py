"""Hold lab — does following the scanner's conviction for a fixed hold make money?

Replays the setup lab's year of scans (research/setup_lab.py: 40 perpetuals, a full scan every
4 h through the production TA, setups and conviction) and, for every reading with
|conviction| ≥ threshold, takes the conviction's side for a fixed hold with no stop:

  raw          the coin alone
  vs BTC       coin minus BTC over the same hold — what the Hold-72 book trades (experiments/hold72.py)
  vs average   coin minus the average coin at that moment — the cleanest relative-strength read

Costs: 16 bps round trip per leg (taker 0.05% + slippage 0.03%, each way); the hedged variants
pay two legs. Each day's trades are averaged into one daily return so the many overlapping
4-hourly entries count as one observation per day. Pass rule — the strategy lab's: the 95%
moving-block bootstrap interval of the mean daily return must sit above zero (10-day blocks).

    ./venv/bin/python -m research.hold_lab          # needs the setup lab's cached 365-day run
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

RESULTS = os.path.join(CACHE_DIR, "hold_lab_results.json")
SCANS = os.path.join(CACHE_DIR, "setup_results_365d_40c.pkl")
KLINES = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
LEG_COST_BPS = 16.0
THRESHOLDS = (0.35, 0.45)
HOLDS = (24, 72)
BLOCK = 10
BOOT = 2000
TRAIN_FRAC = 0.6


def _closes(kl: dict) -> dict[str, pd.Series]:
    out = {}
    for sym, df in kl.items():
        t = pd.to_datetime(df["open_time"], unit="ms", utc=True) if "open_time" in df else pd.to_datetime(df.index, utc=True)
        s = pd.Series(df["close"].astype(float).values, index=t + pd.Timedelta(minutes=15))
        out[sym] = s[~s.index.duplicated()].sort_index()
    return out


def forward_returns(scans: pd.DataFrame, closes: dict[str, pd.Series], holds=HOLDS) -> pd.DataFrame:
    """Each scan with its forward return (bps) over each hold, raw, vs BTC and vs the average coin."""
    parts = []
    btc = closes.get("BTCUSDT")
    for sym, g in scans.groupby("symbol"):
        c = closes.get(sym)
        if c is None:
            continue
        p0 = c.reindex(g["ts"], method="ffill").values
        cols = {"idx": g.index}
        for h in holds:
            later = g["ts"] + pd.Timedelta(hours=h)
            cols[f"f{h}"] = (c.reindex(later, method="ffill").values / p0 - 1) * 1e4
            if btc is not None:
                b0, b1 = btc.reindex(g["ts"], method="ffill").values, btc.reindex(later, method="ffill").values
                cols[f"btc{h}"] = (b1 / b0 - 1) * 1e4
        parts.append(pd.DataFrame(cols).set_index("idx"))
    df = scans.join(pd.concat(parts))
    for h in holds:
        df[f"vb{h}"] = df[f"f{h}"] - df[f"btc{h}"]
        df[f"va{h}"] = df[f"f{h}"] - df.groupby("ts")[f"f{h}"].transform("mean")
    return df


def block_ci(values: np.ndarray, seed: int = 7) -> tuple[float, float]:
    rng, n = random.Random(seed), len(values)
    if n <= BLOCK:
        return float("nan"), float("nan")
    means = []
    for _ in range(BOOT):
        idx: list[int] = []
        while len(idx) < n:
            st = rng.randrange(0, n - BLOCK)
            idx += range(st, st + BLOCK)
        means.append(values[idx[:n]].mean())
    means.sort()
    return means[int(BOOT * 0.025)], means[int(BOOT * 0.975)]


def evaluate(df: pd.DataFrame, threshold: float, hold: int, variant: str) -> dict:
    """One rule: follow |conviction| ≥ threshold for `hold` hours, measured as `variant`."""
    col, legs = {"raw": (f"f{hold}", 1), "vs_btc": (f"vb{hold}", 2), "vs_average": (f"va{hold}", 2)}[variant]
    sel = df[(df["conviction"].abs() >= threshold) & df[col].notna()]
    if variant == "vs_btc":
        sel = sel[sel["symbol"] != "BTCUSDT"]
    trade = np.sign(sel["conviction"]) * sel[col] - LEG_COST_BPS * legs
    daily = trade.groupby(sel["ts"].dt.floor("D")).mean()
    lo, hi = block_ci(daily.values)
    split = daily.index[int(len(daily) * TRAIN_FRAC)]
    quarters = daily.groupby(daily.index.tz_localize(None).to_period("Q")).mean()
    by_side = {side: round(float(trade[np.sign(sel["conviction"]) == s].mean()), 1)
               for side, s in (("long", 1), ("short", -1)) if (np.sign(sel["conviction"]) == s).any()}
    return {
        "threshold": threshold, "hold_h": hold, "variant": variant, "trades": int(len(sel)), "days": int(len(daily)),
        "mean_bps_day": round(float(daily.mean()), 1), "ci_low": round(lo, 1), "ci_high": round(hi, 1),
        "train_bps_day": round(float(daily[daily.index < split].mean()), 1),
        "test_bps_day": round(float(daily[daily.index >= split].mean()), 1),
        "quarters_positive": int((quarters > 0).sum()), "quarters": int(len(quarters)),
        "hit_pct": round(float((trade > 0).mean() * 100), 1),
        "worst_1pct_bps": round(float(np.percentile(trade, 1)), 0), "by_side_bps": by_side,
        "passed": bool(lo > 0),
    }


def run() -> dict:
    scans, kl = pd.read_pickle(SCANS), pd.read_pickle(KLINES)
    df = forward_returns(scans, _closes(kl))
    rules = [evaluate(df, t, h, v) for t in THRESHOLDS for h in HOLDS for v in ("raw", "vs_btc", "vs_average")]
    out = {"generated_at": pd.Timestamp.now(tz="UTC").timestamp(), "scans": int(len(df)),
           "period": [str(df["ts"].min().date()), str(df["ts"].max().date())],
           "cost_bps_per_leg": LEG_COST_BPS, "rules": rules,
           "book_rule": next(r for r in rules if r["threshold"] == 0.45 and r["hold_h"] == 72 and r["variant"] == "vs_btc")}
    with open(RESULTS, "w") as f:
        json.dump(out, f, indent=1)
    return out


def load() -> dict | None:
    try:
        with open(RESULTS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    res = run()
    print(f"{res['scans']} scans, {res['period'][0]} → {res['period'][1]}, {res['cost_bps_per_leg']:g} bps a leg\n")
    for r in res["rules"]:
        print(f"|conv|≥{r['threshold']} {r['hold_h']:>2}h {r['variant']:10}  {r['mean_bps_day']:+7.1f} bps/day  "
              f"CI [{r['ci_low']:+7.1f}, {r['ci_high']:+7.1f}]  train {r['train_bps_day']:+7.1f} test {r['test_bps_day']:+7.1f}  "
              f"Q+ {r['quarters_positive']}/{r['quarters']}  sides {r['by_side_bps']}  {'PASS' if r['passed'] else 'no'}")
