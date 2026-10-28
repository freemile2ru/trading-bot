"""Edge scan — is there ANY single coin or stock with a consistent edge? Searched honestly.

Searching item by item is how false edges are made: with ~160 items and a dozen rules each, some
will look great by luck. So this scan is built to say how many "passes" luck alone produces:

  1. per item, a small fixed menu of daily rules (trend, mean reversion, buy-the-dip, each long/flat
     or long/short); costs: crypto perps 0.08%/side + their funding, stocks as shares 0.02%/side
  2. the rule is CHOSEN on the first 50% of the item's history (best Sharpe), must stay positive on
     the next 25% (confirm), and is judged ONCE on the final 25% (holdout): it passes only if the
     holdout's 95% bootstrap interval of mean daily return is above zero AND it beats simply holding
     the item over the holdout
  3. the identical procedure runs on SHUFFLED versions of each item's returns (same returns, random
     order — any real pattern destroyed). The count of shuffled passes is the luck baseline; real
     passes only mean something well above it.

    ./venv/bin/python -m research.edge_scan
"""
from __future__ import annotations

import json
import math
import os
import random
import time

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

RESULTS = os.path.join(CACHE_DIR, "edge_scan_results.json")
BLOCK, SIMS = 10, 1000


def rules(px: pd.Series) -> dict[str, pd.Series]:
    """Daily target position in [-1, 1], set at the close and held to the next."""
    r = px.pct_change()
    out = {}
    for lb in (20, 50, 100, 200):
        up = (px > px.rolling(lb).mean()).astype(float)
        out[f"trend {lb}d long/flat"] = up
        out[f"trend {lb}d long/short"] = up * 2 - 1
    for lb, z in ((5, 1.5), (5, 2.0), (10, 1.5), (20, 2.0)):
        ret = px.pct_change(lb)
        zs = (ret - ret.rolling(250).mean()) / ret.rolling(250).std()
        out[f"reversion {lb}d z>{z} both ways"] = (-np.sign(zs) * (zs.abs() > z)).astype(float)
        out[f"buy the {lb}d dip z<-{z}, hold while below 0"] = ((zs < -z).astype(float)
                                                               .where((zs < -z) | (zs > 0)).ffill().fillna(0.0))
    d = r.diff()
    rsi = 100 - 100 / (1 + d.clip(lower=0).ewm(alpha=.5).mean() / (-d.clip(upper=0)).ewm(alpha=.5).mean())
    above = px > px.rolling(200).mean()
    out["RSI2<10 in uptrend"] = (((rsi < 10) & above).astype(float).where(((rsi < 10) & above) | (px > px.rolling(5).mean()))
                                 .ffill().fillna(0.0))
    return {k: v.reindex(px.index).fillna(0.0) for k, v in out.items()}


def backtest(pos: pd.Series, ret: pd.Series, cost: float, carry: pd.Series | None) -> pd.Series:
    nxt = ret.shift(-1)
    pnl = pos * nxt - pos.diff().abs().fillna(pos.abs()) * cost
    if carry is not None:
        pnl -= pos * carry.shift(-1).fillna(0.0)            # longs pay funding, shorts receive
    return pnl.iloc[:-1]


def _sharpe(r: pd.Series) -> float:
    return float(r.mean() / r.std() * math.sqrt(252)) if len(r) > 20 and r.std() > 0 else -9.0


def _ci_low(r: pd.Series, rng: random.Random) -> float:
    v, n = r.values, len(r)
    if n <= BLOCK * 2:
        return -1.0
    boot = sorted(v[np.concatenate([np.arange(s, s + BLOCK) for s in (rng.randrange(n - BLOCK + 1)
                                                                      for _ in range(math.ceil(n / BLOCK)))])[:n]].mean()
                  for _ in range(SIMS))
    return float(boot[int(.025 * SIMS)])


def scan_item(px: pd.Series, cost: float, carry: pd.Series | None, rng: random.Random) -> dict:
    px = px.dropna()
    if len(px) < 500:
        return {"skipped": "under 500 days"}
    ret = px.pct_change()
    a, b = px.index[len(px) // 2], px.index[int(len(px) * .75)]
    cands = {k: backtest(p, ret, cost, carry) for k, p in rules(px).items()}
    best = max(cands, key=lambda k: _sharpe(cands[k][cands[k].index < a]))
    r = cands[best]
    conf, hold = r[(r.index >= a) & (r.index < b)], r[r.index >= b]
    hold_bh = ret.shift(-1)[(ret.index >= b)].iloc[:-1]
    if carry is not None:
        hold_bh = hold_bh - carry.shift(-1).reindex(hold_bh.index).fillna(0.0)
    ok = conf.mean() > 0 and _ci_low(hold, rng) > 0 and hold.mean() > hold_bh.mean()
    return {"rule": best, "search_sharpe": round(_sharpe(r[r.index < a]), 2), "confirm_sharpe": round(_sharpe(conf), 2),
            "holdout_sharpe": round(_sharpe(hold), 2), "holdout_annual_pct": round(float(hold.mean() * 252 * 100), 1),
            "holdout_hold_annual_pct": round(float(hold_bh.mean() * 252 * 100), 1),
            "holdout_from": str(b.date()), "pass": bool(ok)}


def shuffled(px: pd.Series, rng: np.random.Generator) -> pd.Series:
    r = px.pct_change().dropna().values.copy()
    rng.shuffle(r)
    return pd.Series(px.iloc[0] * np.concatenate([[1.0], np.cumprod(1 + r)]), index=px.index[:len(r) + 1])


def main(shuffles: int = 3) -> dict:
    perp, funding = pd.read_pickle(os.path.join(CACHE_DIR, "daily_4y_40c.pkl"))
    eq = pd.read_pickle(os.path.join(CACHE_DIR, "equity_daily_10y.pkl"))
    items = [(f"crypto:{c}", perp[c], 0.0008, funding[c]) for c in perp.columns]
    seen = set()
    for region in ("us", "asia", "dow_2016", "us_sectors"):
        for t in eq[region]["px"].columns:
            if t not in seen:
                seen.add(t)
                items.append((f"{region}:{t}", eq[region]["px"][t], 0.0002, None))
    rng, nrng = random.Random(11), np.random.default_rng(11)
    real, fake = [], []
    for name, px, cost, carry in items:
        res = scan_item(px, cost, carry, rng)
        if "skipped" in res:
            continue
        real.append({"item": name, **res})
        for _ in range(shuffles):
            fake.append(scan_item(shuffled(px.dropna(), nrng), cost, None if carry is None else carry * 0, rng).get("pass", False))
    n = len(real)
    passes = [x for x in real if x["pass"]]
    luck_rate = float(np.mean(fake)) if fake else float("nan")
    out = {"generated_at": time.time(), "items": n, "passes": len(passes), "luck_rate_pct": round(luck_rate * 100, 2),
           "expected_by_luck": round(luck_rate * n, 1), "passed": passes, "all": real}
    json.dump(out, open(RESULTS, "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    t0 = time.time()
    res = main()
    print(f"{res['items']} items scanned ({time.time() - t0:.0f}s). Passed: {res['passes']}. "
          f"Luck baseline (shuffled data, same procedure): {res['luck_rate_pct']}% → ~{res['expected_by_luck']} passes expected by chance.\n")
    for x in sorted(res["passed"], key=lambda x: -x["holdout_sharpe"]):
        print(f"  PASS {x['item']:22} {x['rule']:38} search Sharpe {x['search_sharpe']:5} confirm {x['confirm_sharpe']:5} "
              f"HOLDOUT {x['holdout_sharpe']:5} ({x['holdout_annual_pct']}%/yr vs holding {x['holdout_hold_annual_pct']}%/yr, from {x['holdout_from']})")
