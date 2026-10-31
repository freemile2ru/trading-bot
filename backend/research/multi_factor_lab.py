"""Multi-factor lab — the three systematic families combined the way quant funds run them.

research/strategy_lab.py tested cross-sectional momentum, time-series trend and funding carry one at a time on
4 years × 40 perpetuals: test-half Sharpe 0.56 / 0.35 / 1.08, none with a 95% interval above zero. What the
systematic desks that trade these actually do is hold all of them at once, weight them by risk, and target a
portfolio volatility — the diversification is the product, not any one signal. This lab tests exactly that.

Method
  * each family's parameters are chosen on the TRAIN half only (as in strategy_lab); the chosen weight frames
    (gross exposure 1 each) are then combined, so turnover nets across families before costs
  * combinations: equal weight; risk parity (each family scaled by 1 / its trailing 60-day return volatility);
    each with and without portfolio volatility targeting (trailing 20-day realised vol scaled to TARGET_VOL, gross
    capped at MAX_GROSS) — every scaling uses only past returns, no look-ahead
  * costs 0.08% per side of turnover, real funding, same block-bootstrap 95% interval on the test half
  * pre-registered verdict (set 2026-10-29): "promising" only if the chosen combination's test-half interval of the
    mean daily return is above zero AND its test Sharpe beats the best single family's; the chosen combination is
    the one with the best TRAIN Sharpe, so the test half never picks it

    ./venv/bin/python -m research.multi_factor_lab --years 4
"""
from __future__ import annotations

import itertools
import json
import os
import random
import time

import numpy as np
import pandas as pd

from research import strategy_lab as SL

RESULTS = os.path.join(SL.CACHE, "multi_factor_results.json")
TARGET_VOL = 0.015            # 1.5% a day on the whole book
MAX_GROSS = 2.0
VOL_WINDOW = 20
RP_WINDOW = 60


def family_weights(daily: pd.DataFrame, funding: pd.DataFrame, half) -> dict[str, dict]:
    """Each family's best parameters on the train half and its weight frame."""
    out = {}
    for key, fam in SL.FAMILIES.items():
        best = None
        for combo in itertools.product(*fam["grid"].values()):
            p = dict(zip(fam["grid"], combo))
            w = fam["fn"](daily, funding, **p)
            r = SL.run(w, daily, funding)
            tr = SL.stats(r[r.index < half])
            if best is None or (tr.get("sharpe") or -99) > (best["train"].get("sharpe") or -99):
                best = {"params": p, "weights": w, "returns": r, "train": tr, "describe": fam["describe"](p)}
        out[key] = best
    return out


def combine(fams: dict[str, dict], mode: str) -> pd.DataFrame:
    frames = [f["weights"] for f in fams.values()]
    if mode == "equal":
        return sum(frames) / len(frames)
    # risk parity: scale each family by 1 / its trailing return volatility (yesterday's, so no look-ahead)
    inv = {k: (1.0 / f["returns"].rolling(RP_WINDOW).std().shift(1)).replace([np.inf, -np.inf], np.nan) for k, f in fams.items()}
    total = sum(inv.values())
    combined = sum(f["weights"].mul(inv[k].reindex(f["weights"].index), axis=0) for k, f in fams.items())
    return combined.div(total.reindex(combined.index), axis=0).fillna(0.0)


def vol_target(w: pd.DataFrame, daily: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
    """Scale the book so its trailing realised volatility sits at TARGET_VOL (yesterday's reading)."""
    r = SL.run(w, daily, funding)
    scale = (TARGET_VOL / r.rolling(VOL_WINDOW).std()).shift(1).clip(upper=MAX_GROSS).reindex(w.index).fillna(1.0)
    return w.mul(scale, axis=0)


def main(years: int | None = 4) -> dict:
    daily, funding = SL.load_long(years) if years else SL.load()
    half = daily.index[len(daily) // 2]
    rng = random.Random(3)
    fams = family_weights(daily, funding, half)
    fam_test = {k: SL.stats(f["returns"][f["returns"].index >= half], rng) for k, f in fams.items()}
    best_single = max(fam_test.values(), key=lambda s: s.get("sharpe") or -99)
    # Diversification evidence: how the families move together on the train half.
    train_rets = pd.DataFrame({k: f["returns"][f["returns"].index < half] for k, f in fams.items()})
    corr = train_rets.corr().round(2).to_dict()

    combos = []
    for mode, vt in itertools.product(("equal", "risk_parity"), (False, True)):
        w = combine(fams, mode)
        if vt:
            w = vol_target(w, daily, funding)
        r = SL.run(w, daily, funding)
        combos.append({"mode": mode, "vol_target": vt, "label": f"{mode.replace('_', ' ')}{' + vol target' if vt else ''}",
                       "train": SL.stats(r[r.index < half]), "test": SL.stats(r[r.index >= half], rng), "series": r})
    chosen = max(combos, key=lambda c: c["train"].get("sharpe") or -99)
    ci = chosen["test"].get("mean_daily_ci_bps") or [0, 0]
    beats = (chosen["test"].get("sharpe") or 0) > (best_single.get("sharpe") or 0)
    status = "promising" if ci[0] > 0 and beats else "fails" if ci[1] < 0 else "no_edge"
    curve = (1 + chosen["series"][chosen["series"].index >= half]).cumprod()
    report = {
        "generated_at": time.time(), "dataset": f"{years} years of daily data", "coins": len(daily.columns),
        "period": [str(daily.index[0].date()), str(daily.index[-1].date())], "split": str(half.date()),
        "families": {k: {"chosen": f["describe"], "train": f["train"], "test": fam_test[k]} for k, f in fams.items()},
        "train_correlations": corr,
        "combinations": [{k: v for k, v in c.items() if k != "series"} for c in combos],
        "chosen": chosen["label"], "status": status, "beats_best_single": beats,
        "best_single_test_sharpe": best_single.get("sharpe"),
        "curve": [{"day": str(d.date()), "equity": round(float(v), 4)} for d, v in curve.items()],
        "rule": "promising only if the train-chosen combination's test-half 95% interval is above zero and its test Sharpe beats the best single family",
    }
    json.dump(report, open(RESULTS, "w"), indent=1, default=str)
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=4)
    rep = main(ap.parse_args().years)
    print(f"{rep['dataset']}, {rep['coins']} coins, {rep['period'][0]} → {rep['period'][1]}, split {rep['split']}")
    print("Families (train-chosen), test half:")
    for k, f in rep["families"].items():
        print(f"  {k:8} {f['chosen'][:60]:60} Sharpe {f['test'].get('sharpe')} ci {f['test'].get('mean_daily_ci_bps')}")
    print("Train correlations:", rep["train_correlations"])
    print("Combinations:")
    for c in rep["combinations"]:
        print(f"  {c['label']:26} train Sharpe {c['train'].get('sharpe')}  test Sharpe {c['test'].get('sharpe')}  "
              f"ret {c['test'].get('total_return_pct')}%  maxDD {c['test'].get('max_drawdown_pct')}%  ci {c['test'].get('mean_daily_ci_bps')}")
    print(f"Chosen on train: {rep['chosen']} → {rep['status']} (beats best single family: {rep['beats_best_single']})")
    print(f"Written to {RESULTS}")
