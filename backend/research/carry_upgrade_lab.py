"""Carry upgrade — can the one proven edge earn more: other venues' funding, quarterly basis, rotating coins?

Run: venv/bin/python -m research.carry_upgrade_lab   (public APIs: Binance, Bybit, OKX; cached under
research_cache/carry_upgrade/; writes research_cache/carry_upgrade_results.json)

  A  cross-venue funding   BTC/ETH/SOL/XRP/DOGE/BNB funding on Binance, Bybit and OKX for the last year.
                           Strategies on each coin: short the perp where trailing-7d funding is highest (long spot),
                           switching venue only when another beats it by SWITCH_EDGE; and a perp-perp spread
                           (long the lowest-funding venue, short the highest). Switch cost: 16 bps a leg pair.
  B  quarterly basis       BTC/ETH current-quarter futures vs the perp, daily since 2023: the annualised basis you
                           could LOCK by selling the quarterly instead of the perp, against the funding the perp
                           actually paid over the same days.
  C  rotating majors       funding on the 12 largest coins from the year of 4h data: weekly, hold the top-3 by
                           trailing-14d funding (if > 0) vs always BTC+ETH. Switch cost 32 bps (4 fills).
All figures are annualised % on notional, before the spot leg's own small costs; nothing here uses leverage.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import httpx
import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "carry_upgrade_results.json")
CACHE = os.path.join(CACHE_DIR, "carry_upgrade")
COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
DAYS = 365
SWITCH_COST = 0.0016
SWITCH_EDGE = 0.10           # a venue must beat the current one by 10%/yr (trailing) before we move
MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT", "LINKUSDT", "LTCUSDT",
          "AVAXUSDT", "DOTUSDT", "BCHUSDT"]


def _cached(name: str, loader):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 6 * 3600:
        return pd.read_pickle(p)
    v = loader()
    pd.to_pickle(v, p)
    return v


def binance_funding(c: httpx.Client, coin: str, start_ms: int) -> pd.Series:
    out, t = {}, start_ms
    while True:
        r = c.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": f"{coin}USDT", "startTime": t, "limit": 1000}).json()
        if not isinstance(r, list) or not r:
            break
        for x in r:
            out[x["fundingTime"]] = float(x["fundingRate"])
        if len(r) < 1000:
            break
        t = r[-1]["fundingTime"] + 1
        time.sleep(0.1)
    return pd.Series(out, dtype=float)


def bybit_funding(c: httpx.Client, coin: str, start_ms: int) -> pd.Series:
    out, end = {}, int(time.time() * 1000)
    while end > start_ms:
        r = c.get("https://api.bybit.com/v5/market/funding/history",
                  params={"category": "linear", "symbol": f"{coin}USDT", "startTime": start_ms, "endTime": end, "limit": 200}).json()
        rows = (r.get("result") or {}).get("list") or []
        if not rows:
            break
        for x in rows:
            out[int(x["fundingRateTimestamp"])] = float(x["fundingRate"])
        end = min(int(x["fundingRateTimestamp"]) for x in rows) - 1
        time.sleep(0.1)
    return pd.Series(out, dtype=float)


def okx_funding(c: httpx.Client, coin: str, start_ms: int) -> pd.Series:
    out, after = {}, None
    while True:
        params = {"instId": f"{coin}-USDT-SWAP", "limit": 100}
        if after:
            params["after"] = after
        r = c.get("https://www.okx.com/api/v5/public/funding-rate-history", params=params).json()
        rows = r.get("data") or []
        if not rows:
            break
        for x in rows:
            out[int(x["fundingTime"])] = float(x.get("realizedRate") or x["fundingRate"])
        after = min(int(x["fundingTime"]) for x in rows)
        if after < start_ms:
            break
        time.sleep(0.15)
    return pd.Series(out, dtype=float)


def _daily(series: pd.Series) -> pd.Series:
    """Funding per UTC day (sum of settlements), as a fraction."""
    s = series.sort_index()
    idx = pd.to_datetime(s.index, unit="ms", utc=True).floor("D")
    return s.groupby(idx).sum()


def cross_venue(c: httpx.Client) -> dict:
    start_ms = int((time.time() - DAYS * 86400) * 1000)
    res = {}
    for coin in COINS:
        v = _cached(f"funding_{coin}.pkl", lambda: {"binance": binance_funding(c, coin, start_ms),
                                                   "bybit": bybit_funding(c, coin, start_ms),
                                                   "okx": okx_funding(c, coin, start_ms)})
        df = pd.DataFrame({k: _daily(s) for k, s in v.items() if len(s)}).dropna()
        if len(df) < 60:
            continue
        ann = df.mean() * 365 * 100
        trail = df.rolling(7).mean() * 365
        # best venue with hysteresis
        venue, earned, switches = None, [], 0
        for d, row in trail.iterrows():
            if row.isna().any():
                earned.append(0.0)
                continue
            best = row.idxmax()
            if venue is None or row[best] - row[venue] > SWITCH_EDGE:
                if venue is not None:
                    switches += 1
                venue = best
            earned.append(df.loc[d, venue] - (SWITCH_COST if switches and earned and venue != getattr(cross_venue, "_prev", venue) else 0))
            cross_venue._prev = venue
        best_ser = pd.Series(earned, index=df.index)
        # perp-perp spread: long lowest, short highest trailing venue
        spread_rows, prev_pair, sw = [], None, 0
        for d, row in trail.iterrows():
            if row.isna().any():
                spread_rows.append(0.0)
                continue
            pair = (row.idxmin(), row.idxmax())
            cost = 0.0
            if pair != prev_pair and prev_pair is not None:
                sw += 1
                cost = 2 * SWITCH_COST
            prev_pair = pair
            spread_rows.append(df.loc[d, pair[1]] - df.loc[d, pair[0]] - cost)
        spread = pd.Series(spread_rows, index=df.index)
        half = len(df) // 2
        res[coin] = {"days": int(len(df)), "venue_annual_pct": {k: round(float(x), 2) for k, x in ann.items()},
                     "best_venue_annual_pct": round(float(best_ser.mean() * 365 * 100), 2), "venue_switches": switches,
                     "spread_annual_pct": round(float(spread.mean() * 365 * 100), 2), "spread_switches": sw,
                     "spread_first_half": round(float(spread.iloc[:half].mean() * 365 * 100), 2),
                     "spread_second_half": round(float(spread.iloc[half:].mean() * 365 * 100), 2),
                     "spread_positive_days_pct": round(float((spread > 0).mean() * 100), 1)}
    return res


def _expiry(d: pd.Timestamp) -> pd.Timestamp:
    """The current quarterly's delivery: the last Friday of Mar/Jun/Sep/Dec, 08:00 UTC, at or after d."""
    for q in range(0, 3):
        m = ((d.month - 1) // 3 + 1 + q) * 3
        y = d.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        last = pd.Timestamp(year=y, month=m, day=28, hour=8, tz="UTC") + pd.offsets.MonthEnd(0)
        last = last.replace(hour=8)
        while last.weekday() != 4:
            last -= pd.Timedelta(days=1)
        if last > d + pd.Timedelta(days=1):
            return last
    return last


def quarterly_basis(c: httpx.Client) -> dict:
    res = {}
    for coin in ("BTC", "ETH"):
        def load(coin=coin):
            out = {}
            for ct in ("CURRENT_QUARTER", "PERPETUAL"):
                rows, t = [], int(pd.Timestamp("2023-01-01", tz="UTC").timestamp() * 1000)
                while True:
                    r = c.get("https://fapi.binance.com/fapi/v1/continuousKlines",
                              params={"pair": f"{coin}USDT", "contractType": ct, "interval": "1d", "startTime": t, "limit": 1500}).json()
                    if not isinstance(r, list) or not r:
                        break
                    rows += r
                    if len(r) < 1500:
                        break
                    t = r[-1][0] + 1
                out[ct] = pd.Series({pd.Timestamp(x[0], unit="ms", tz="UTC"): float(x[4]) for x in rows})
            f = binance_funding(c, coin, int(pd.Timestamp("2023-01-01", tz="UTC").timestamp() * 1000))
            return out["CURRENT_QUARTER"], out["PERPETUAL"], _daily(f)
        q, p, fund = _cached(f"quarterly_{coin}.pkl", load)
        df = pd.DataFrame({"q": q, "p": p, "fund": fund}).dropna()
        days_left = np.array([(_expiry(d) - d).total_seconds() / 86400 for d in df.index])
        basis = np.log(df["q"] / df["p"]).values
        df["basis_ann"] = np.where(days_left > 3, basis * 365 / np.maximum(days_left, 3), np.nan)
        # realised funding (annualised) over the same remaining life of that contract
        fwd = []
        for i, d in enumerate(df.index):
            n = int(max(min(days_left[i], 92), 1))
            fwd.append(df["fund"].iloc[i:i + n].mean() * 365 if i + n <= len(df) else np.nan)
        df["funding_fwd_ann"] = fwd
        both = df.dropna(subset=["basis_ann", "funding_fwd_ann"])
        by_year = {str(y): {"basis_pct": round(float(g["basis_ann"].mean() * 100), 2), "funding_pct": round(float(g["funding_fwd_ann"].mean() * 100), 2)}
                   for y, g in both.groupby(both.index.year)}
        res[coin] = {"days": int(len(both)), "basis_annual_pct": round(float(both["basis_ann"].mean() * 100), 2),
                     "funding_annual_pct": round(float(both["funding_fwd_ann"].mean() * 100), 2),
                     "basis_beats_funding_pct_days": round(float((both["basis_ann"] > both["funding_fwd_ann"]).mean() * 100), 1),
                     "by_year": by_year, "basis_now_pct": round(float(df["basis_ann"].dropna().iloc[-1] * 100), 2),
                     "funding_last30d_pct": round(float(df["fund"].iloc[-30:].mean() * 365 * 100), 2)}
    return res


def rotation() -> dict:
    data = pd.read_pickle(os.path.join(CACHE_DIR, "signal_rebuild_data.pkl"))
    daily = pd.DataFrame({s: _daily(data[s]["funding"]) for s in MAJORS if s in data and len(data[s]["funding"])}).dropna(how="all")
    daily = daily.fillna(0.0)
    trail = daily.rolling(14).mean() * 365
    weeks = [d for d in daily.index if d.weekday() == 0]
    out = {}
    for name, k in (("top-3 by trailing funding (if > 0)", 3), ("top-1", 1)):
        held, earned, switches = set(), [], 0
        for d in daily.index:
            if d in weeks and not trail.loc[d].isna().all():
                row = trail.loc[d]
                new = set(row[row > 0].nlargest(k).index)
                switches += len(new - held)
                cost = len(new - held) * SWITCH_COST / max(len(new), 1)
                held = new
            else:
                cost = 0.0
            earned.append((daily.loc[d, list(held)].mean() if held else 0.0) - cost)
        s = pd.Series(earned, index=daily.index)
        half = len(s) // 2
        out[name] = {"annual_pct": round(float(s.mean() * 365 * 100), 2), "switches": switches,
                     "first_half": round(float(s.iloc[:half].mean() * 365 * 100), 2), "second_half": round(float(s.iloc[half:].mean() * 365 * 100), 2)}
    base = daily[["BTCUSDT", "ETHUSDT"]].mean(axis=1)
    out["always BTC+ETH"] = {"annual_pct": round(float(base.mean() * 365 * 100), 2), "switches": 0,
                             "first_half": round(float(base.iloc[:len(base)//2].mean() * 365 * 100), 2),
                             "second_half": round(float(base.iloc[len(base)//2:].mean() * 365 * 100), 2)}
    out["_per_coin_annual_pct"] = {s: round(float(daily[s].mean() * 365 * 100), 2) for s in daily.columns}
    out["_days"] = int(len(daily))
    return out


def main() -> dict:
    c = httpx.Client(timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    res = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "A_cross_venue": cross_venue(c), "B_quarterly_basis": quarterly_basis(c), "C_rotation": rotation()}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print("A. Cross-venue funding (annualised % on notional)")
    for coin, v in r["A_cross_venue"].items():
        print(f"  {coin:5s} {v['days']}d  venues {v['venue_annual_pct']}  best-venue {v['best_venue_annual_pct']:+.2f}% ({v['venue_switches']} switches)  "
              f"perp-perp spread {v['spread_annual_pct']:+.2f}% ({v['spread_switches']} switches, halves {v['spread_first_half']:+.2f}/{v['spread_second_half']:+.2f}, +days {v['spread_positive_days_pct']}%)")
    print("B. Quarterly basis vs funding (annualised %)")
    for coin, v in r["B_quarterly_basis"].items():
        print(f"  {coin}: {v['days']}d  basis {v['basis_annual_pct']:+.2f}% vs realised funding {v['funding_annual_pct']:+.2f}%  basis higher on {v['basis_beats_funding_pct_days']}% of days  "
              f"by year {v['by_year']}  NOW basis {v['basis_now_pct']:+.2f}% vs funding(30d) {v['funding_last30d_pct']:+.2f}%")
    print("C. Rotating majors (annualised %, funding only)")
    for k, v in r["C_rotation"].items():
        if not k.startswith("_"):
            print(f"  {k:36s} {v['annual_pct']:+.2f}% ({v['switches']} switches; halves {v['first_half']:+.2f}/{v['second_half']:+.2f})")
    print("  per coin:", r["C_rotation"]["_per_coin_annual_pct"])
