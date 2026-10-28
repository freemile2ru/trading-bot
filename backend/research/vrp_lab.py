"""Bitcoin / Ether volatility risk premium — is implied volatility reliably above what then happens?

Why: option sellers are paid the gap between implied and realised volatility; in BTC it has been positive most of
the time since 2019 (Deribit). This lab measures it on our own two years of hourly spot + 30-day implied vol
(research_cache/options_730d.pkl: hourly S and iv for BTC and ETH), and prices the simplest harvest — a variance
swap sold at implied, paid at realised — month by month, so the size AND the tail show.

Run: venv/bin/python -m research.vrp_lab   (writes research_cache/vrp_results.json)

  premium     IV(30d, annualised) − realised vol over the NEXT 30 days (close-to-close hourly, annualised)
  var-swap    P&L per $1 of vega-notional ≈ (IV² − RV²) / (2·IV) — what a delta-hedged short straddle earns
              on average, before costs; sampled once a day, held 30 days (overlapping)
  strangle    a defined-risk proxy: short 30-day strangle struck ±1σ_implied, premium ≈ 0.8·IV·√T·S each
              side (Bachelier), paid out against the realised 30-day move, wings at ±2σ cap the loss
Reported: mean premium (vol points), % of days positive, var-swap mean and worst months, strangle hit rate and
average P&L in % of notional, first vs second year.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

DATA = os.path.join(CACHE_DIR, "options_730d.pkl")
OUT = os.path.join(CACHE_DIR, "vrp_results.json")
H = 24 * 30                    # 30 days of hours
FEE = 0.0006                   # Deribit: 0.03% of underlying per option, two legs, open + settle ≈ 6 bps of notional


def realised_next(s: pd.Series, hours: int = H) -> pd.Series:
    """Annualised close-to-close vol over the next `hours` hourly bars."""
    lr = np.log(s).diff()
    fwd = lr[::-1].rolling(hours).std()[::-1].shift(-1)      # std of the next `hours` returns
    return fwd * math.sqrt(24 * 365)


def main() -> dict:
    data = pd.read_pickle(DATA)
    res = {}
    for coin, df in data.items():
        df = df.dropna()
        s, iv = df["S"], df["iv"]
        rv = realised_next(s)
        prem = (iv - rv).dropna()
        daily = prem[prem.index.hour == 0]
        # variance swap sold at IV, settled at RV, per $1 vega-notional
        vs = ((iv ** 2 - rv ** 2) / (2 * iv)).dropna()
        vs_d = vs[vs.index.hour == 0]
        monthly = vs_d.groupby(vs_d.index.to_period("M")).mean()
        # short strangle ±1σ with ±2σ wings, 30 days, premium by Bachelier approx: 0.8·σ·√T·S for an ATM-ish straddle,
        # a 1σ strangle is worth about 0.32·σ·√T·S (both legs); realised payout = max(|move| − 1σ, 0) capped at 1σ
        T = 30 / 365
        sig = iv * math.sqrt(T)
        move = (s.shift(-H) / s - 1)
        premium = 0.32 * sig
        payout = (move.abs() - sig).clip(lower=0).clip(upper=sig)
        pnl = (premium - payout - FEE).dropna()
        pnl_d = pnl[pnl.index.hour == 0]
        half = len(pnl_d) // 2
        res[coin] = {
            "days": int(len(daily)),
            "iv_mean_pct": round(float(iv.mean() * 100), 1), "rv_next30_mean_pct": round(float(rv.mean() * 100), 1),
            "premium_mean_volpts": round(float(daily.mean() * 100), 2), "premium_positive_days_pct": round(float((daily > 0).mean() * 100), 1),
            "premium_first_half_volpts": round(float(daily.iloc[:len(daily) // 2].mean() * 100), 2),
            "premium_second_half_volpts": round(float(daily.iloc[len(daily) // 2:].mean() * 100), 2),
            "varswap_mean_pct_of_vega": round(float(vs_d.mean() * 100), 2),
            "varswap_worst_month_pct": round(float(monthly.min() * 100), 2), "varswap_best_month_pct": round(float(monthly.max() * 100), 2),
            "varswap_months_positive_pct": round(float((monthly > 0).mean() * 100), 1),
            "strangle_mean_pct_notional_30d": round(float(pnl_d.mean() * 100), 3),
            "strangle_annualised_pct": round(float(pnl_d.mean() * 12 * 100), 1),
            "strangle_win_pct": round(float((pnl_d > 0).mean() * 100), 1),
            "strangle_worst_30d_pct": round(float(pnl_d.min() * 100), 2),
            "strangle_first_half_pct": round(float(pnl_d.iloc[:half].mean() * 100), 3), "strangle_second_half_pct": round(float(pnl_d.iloc[half:].mean() * 100), 3),
        }
    json.dump(res, open(OUT, "w"), indent=1)
    return res


if __name__ == "__main__":
    for coin, v in main().items():
        print(f"{coin}: IV {v['iv_mean_pct']}% vs next-30d RV {v['rv_next30_mean_pct']}% → premium {v['premium_mean_volpts']:+.1f} vol pts, "
              f"positive {v['premium_positive_days_pct']}% of days (halves {v['premium_first_half_volpts']:+.1f} / {v['premium_second_half_volpts']:+.1f})")
        print(f"   var-swap {v['varswap_mean_pct_of_vega']:+.2f}% of vega/30d, months +{v['varswap_months_positive_pct']}%, worst {v['varswap_worst_month_pct']}%")
        print(f"   1σ strangle w/ 2σ wings: {v['strangle_mean_pct_notional_30d']:+.3f}% of notional per 30d ≈ {v['strangle_annualised_pct']:+.1f}%/yr, "
              f"win {v['strangle_win_pct']}%, worst {v['strangle_worst_30d_pct']}%, halves {v['strangle_first_half_pct']:+.3f} / {v['strangle_second_half_pct']:+.3f}")
