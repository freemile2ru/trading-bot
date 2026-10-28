"""TradFi perps — structural edges in Binance's stock and commodity perpetuals.

Run: venv/bin/python -m research.tradfi_perp_lab   (fetches 1h klines, premium-index klines and funding for
every stock/commodity perp since listing; caches under research_cache/tradfi/; writes research_cache/tradfi_perp_results.json)

These perps trade 24/7 while the stock or commodity is closed, and there is no spot leg on Binance to arbitrage
their funding or their premium to the index. Three questions:
  A  funding spread   is funding across them dispersed and persistent? A weekly book: long the 5 lowest-funding
                      perps (trailing 14d), short the 5 highest — earns the funding spread plus relative drift.
                      Costs 16 bps a leg per switch.
  B  premium fade     when the perp trades well above its index (premium > X), does it come back down — and is that
                      different when the underlying market is open vs closed (the index is stale off-hours)?
                      Trade: short above +X / long below −X, out when the premium is within 5 bps or after 24h;
                      16 bps a round trip.
  C  session drift    mean perp return by session — US cash hours, US closed weekdays, weekend — with t-stats, and
                      the cost of a daily "hold only overnight" rotation.
All returns are on the perp itself. First 60% vs last 40% and bootstrap ranges where it matters.
"""
from __future__ import annotations

import json
import math
import os
import random
import time

import httpx
import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "tradfi_perp_results.json")
CACHE = os.path.join(CACHE_DIR, "tradfi")
FAPI = "https://fapi.binance.com/fapi/v1"
FILL = 0.0008
PREMIUM_X = (0.003, 0.005, 0.010)


def tradfi_symbols(c: httpx.Client) -> dict[str, str]:
    """Binance's TradFi perps (contractType TRADIFI_PERPETUAL): symbol → underlying type (EQUITY, KR_EQUITY, HK_EQUITY,
    JP_EQUITY, COMMODITY, …). Pre-IPO and pre-market contracts are left out."""
    info = c.get(f"{FAPI}/exchangeInfo").json()
    return {s["symbol"]: s.get("underlyingType") or "EQUITY" for s in info["symbols"]
            if s["contractType"] == "TRADIFI_PERPETUAL" and s["status"] == "TRADING"
            and not any("pre" in x.lower() for x in (s.get("underlyingSubType") or []))}


# Cash-session windows in UTC minutes (start, end) by underlying type; commodities trade nearly round the clock on weekdays.
SESSIONS = {"EQUITY": (13 * 60 + 30, 20 * 60), "KR_EQUITY": (0, 6 * 60 + 30), "HK_EQUITY": (90, 8 * 60), "JP_EQUITY": (0, 6 * 60),
            "EU_EQUITY": (8 * 60, 16 * 60 + 30), "UK_EQUITY": (8 * 60, 16 * 60 + 30)}


def _page(c: httpx.Client, path: str, params: dict, start_ms: int, key: str = "startTime", stamp=lambda r: r[0]) -> list:
    rows, t = [], start_ms
    while True:
        try:
            r = c.get(f"{FAPI}/{path}", params={**params, key: t, "limit": 1000}).json()
        except Exception:
            break
        if not isinstance(r, list) or not r:
            break
        rows += r
        if len(r) < 1000:
            break
        t = stamp(r[-1]) + 1
        time.sleep(0.12)
    return rows


def load(c: httpx.Client, sym: str) -> dict | None:
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{sym}.pkl")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 12 * 3600:
        return pd.read_pickle(p)
    start = int((time.time() - 400 * 86400) * 1000)
    kl = _page(c, "klines", {"symbol": sym, "interval": "1h"}, start)
    if len(kl) < 24 * 20:
        return None
    pk = _page(c, "premiumIndexKlines", {"symbol": sym, "interval": "1h"}, start)
    fr = _page(c, "fundingRate", {"symbol": sym}, start, stamp=lambda r: r["fundingTime"])
    idx = pd.to_datetime([k[6] + 1 for k in kl], unit="ms", utc=True)
    df = pd.DataFrame({"o": [float(k[1]) for k in kl], "c": [float(k[4]) for k in kl], "v": [float(k[7]) for k in kl]}, index=idx)
    prem = pd.Series({pd.Timestamp(k[6] + 1, unit="ms", tz="UTC"): float(k[4]) for k in pk})
    df["prem"] = prem.reindex(df.index)
    fund = pd.Series({pd.Timestamp(int(x["fundingTime"]), unit="ms", tz="UTC"): float(x["fundingRate"]) for x in fr})
    out = {"df": df, "fund": fund.sort_index()}
    pd.to_pickle(out, p)
    return out


def _session(ts: pd.DatetimeIndex, kind: str) -> np.ndarray:
    """'open' = the underlying's cash session (US 13:30–20:00 UTC for stocks; COMEX/NYMEX ~ weekdays for commodities),
    'closed' = weekday off-hours, 'weekend'."""
    wd, mins = ts.dayofweek.values, (ts.hour * 60 + ts.minute).values
    weekend = (wd == 5) | ((wd == 6) & (mins < 23 * 60)) | ((wd == 4) & (mins >= 21 * 60))
    if kind == "COMMODITY":
        opened = ~weekend
    else:
        a, b = SESSIONS.get(kind, SESSIONS["EQUITY"])
        opened = (wd < 5) & (mins > a) & (mins <= b)
    return np.where(weekend, "weekend", np.where(opened, "open", "closed"))


def _ci(v: np.ndarray, seed=2, block=5, boot=2000) -> tuple[float, float]:
    rng, n, means = random.Random(seed), len(v), []
    if n <= block:
        return float("nan"), float("nan")
    for _ in range(boot):
        idx: list[int] = []
        while len(idx) < n:
            s = rng.randrange(0, n - block)
            idx += range(s, s + block)
        means.append(v[idx[:n]].mean())
    means.sort()
    return means[int(boot * .025)], means[int(boot * .975)]


MIN_DAILY_VOLUME = 3e6          # median daily quote volume — the thin perps have the wildest funding and no exit


def funding_spread(data: dict[str, dict]) -> dict:
    liquid = {s: d for s, d in data.items() if len(d["fund"]) > 30 and d["df"]["v"].resample("D").sum().median() >= MIN_DAILY_VOLUME}
    daily = pd.DataFrame({s: d["fund"].groupby(d["fund"].index.floor("D")).sum() for s, d in liquid.items()})
    data = liquid
    px = pd.DataFrame({s: d["df"]["c"].resample("D").last() for s, d in data.items()}).reindex(daily.index).ffill()
    ret = px.pct_change(fill_method=None)
    ann = daily.mean() * 365 * 100
    half = len(daily) // 2
    a, b = daily.iloc[:half].mean(), daily.iloc[half:].mean()
    both = ann.dropna().index
    persist = float(a[both].rank().corr(b[both].rank())) if len(both) > 4 else None
    trail = daily.rolling(14).mean() * 365
    held_l, held_s, rows, fund_only, switches = set(), set(), [], [], 0
    for d in daily.index:
        if d.weekday() == 0 and not trail.loc[d].isna().all():
            row = trail.loc[d].dropna()
            k = min(5, len(row) // 3) or 1
            new_l, new_s = set(row.nsmallest(k).index), set(row.nlargest(k).index)
            sw = len(new_l - held_l) + len(new_s - held_s)
            switches += sw
            cost = sw * 2 * FILL / max(len(new_l) + len(new_s), 1)
            held_l, held_s = new_l, new_s
        else:
            cost = 0.0
        if not held_l:
            rows.append(0.0)
            fund_only.append(0.0)
            continue
        # a short RECEIVES positive funding, a long receives negative funding (sign fixed 2026-10-27)
        fund_leg = (daily.loc[d, list(held_s)].mean() - daily.loc[d, list(held_l)].mean()) / 2
        drift = (ret.loc[d, list(held_l)].mean() - ret.loc[d, list(held_s)].mean()) / 2 if d in ret.index else 0.0
        rows.append(fund_leg + (0 if np.isnan(drift) else drift) - cost)
        fund_only.append(fund_leg)
    s = pd.Series(rows, index=daily.index)
    h = len(s) // 2
    lo, hi = _ci(s.values)
    return {"days": int(len(daily)), "perps": int(len(ann.dropna())),
            "funding_annual_pct": {k: round(float(v), 2) for k, v in ann.dropna().sort_values().items()},
            "dispersion_pct": round(float(ann.dropna().std()), 2), "persistence_rank_corr": round(persist, 2) if persist is not None else None,
            "spread_book": {"annual_pct": round(float(s.mean() * 365 * 100), 2), "funding_only_annual_pct": round(float(np.mean(fund_only) * 365 * 100), 2), "first_half": round(float(s.iloc[:h].mean() * 365 * 100), 2),
                            "second_half": round(float(s.iloc[h:].mean() * 365 * 100), 2), "switches": switches,
                            "ci95_annual_pct": [round(lo * 365 * 100, 2), round(hi * 365 * 100, 2)], "positive_days_pct": round(float((s > 0).mean() * 100), 1)}}


def premium_fade(data: dict[str, dict], kinds: dict[str, str]) -> dict:
    res = {}
    for x in PREMIUM_X:
        trades = []
        for sym, d in data.items():
            df = d["df"].dropna(subset=["prem"])
            if len(df) < 500:
                continue
            sess = _session(df.index, kinds[sym])
            prem, c, o = df["prem"].values, df["c"].values, df["o"].values
            i = 0
            while i < len(df) - 25:
                if abs(prem[i]) < x:
                    i += 1
                    continue
                s = -1 if prem[i] > 0 else 1
                entry = o[i + 1]
                j = i + 1
                while j < min(i + 25, len(df) - 1) and abs(prem[j]) > 0.0005:
                    j += 1
                r = (c[j] - entry) * s / entry - 2 * FILL
                trades.append({"sym": sym, "ts": df.index[i], "session": sess[i], "r_bps": r * 1e4, "hours": j - i})
                i = j + 1
        if not trades:
            continue
        t = pd.DataFrame(trades)
        out = {}
        for sess, g in t.groupby("session"):
            v = g["r_bps"].values
            half = len(g) // 2
            out[sess] = {"n": int(len(g)), "mean_bps": round(float(v.mean()), 1), "hit_pct": round(float((v > 0).mean() * 100), 1),
                         "t": round(float(v.mean() / (v.std() / math.sqrt(len(v)))), 2) if len(v) > 2 and v.std() else None,
                         "first_half": round(float(v[:half].mean()), 1), "second_half": round(float(v[half:].mean()), 1),
                         "avg_hours": round(float(g["hours"].mean()), 1)}
        v = t["r_bps"].values
        out["all"] = {"n": int(len(t)), "mean_bps": round(float(v.mean()), 1), "hit_pct": round(float((v > 0).mean() * 100), 1),
                      "t": round(float(v.mean() / (v.std() / math.sqrt(len(v)))), 2) if v.std() else None}
        res[f"|premium| > {x * 100:.1f}%"] = out
    return res


def session_drift(data: dict[str, dict], kinds: dict[str, str]) -> dict:
    res = {}
    for kind in sorted(set(kinds.values())):
        rows = {"open": [], "closed": [], "weekend": []}
        daily_overnight = []
        for sym, d in data.items():
            if kinds[sym] != kind:
                continue
            df = d["df"]
            r = np.log(df["c"] / df["o"]).values
            sess = _session(df.index, kind)
            for k in rows:
                rows[k] += list(r[sess == k])
            # a day's "closed" return (weekday off-hours), for the overnight-rotation cost check
            s = pd.Series(np.where(sess == "closed", r, 0.0), index=df.index)
            daily_overnight.append(s.groupby(s.index.floor("D")).sum())
        out = {}
        for k, v in rows.items():
            v = np.array(v)
            if len(v) < 50:
                continue
            out[k] = {"hours": int(len(v)), "mean_bps_per_hour": round(float(v.mean() * 1e4), 2),
                      "t": round(float(v.mean() / (v.std() / math.sqrt(len(v)))), 2),
                      "annualised_pct": round(float(v.mean() * len(v) / max(len(v) / (24 * 365 / 1.0), 1e-9) / 1), 2) if False else None}
        ov = pd.concat(daily_overnight, axis=1).mean(axis=1) if daily_overnight else pd.Series(dtype=float)
        ov = ov[ov != 0]
        if len(ov) > 20:
            out["overnight_rotation"] = {"days": int(len(ov)), "gross_bps_per_night": round(float(ov.mean() * 1e4), 1),
                                         "cost_bps_per_night": round(2 * FILL * 1e4, 1),
                                         "net_annual_pct": round(float((ov.mean() - 2 * FILL) * 252 * 100), 2)}
        res[kind] = out
    return res


def main() -> dict:
    c = httpx.Client(timeout=25)
    kinds = tradfi_symbols(c)
    data = {}
    for sym in kinds:
        d = load(c, sym)
        if d is not None:
            data[sym] = d
    res = {"perps": {k: sorted(s for s, kk in kinds.items() if kk == k and s in data) for k in sorted(set(kinds.values()))},
           "A_funding": funding_spread(data), "B_premium_fade": premium_fade(data, kinds), "C_session_drift": session_drift(data, kinds)}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print("perps with data:", {k: len(v) for k, v in r["perps"].items()})
    A = r["A_funding"]
    print(f"A. funding: {A['perps']} perps over {A['days']} days · dispersion {A['dispersion_pct']}%/yr · persistence (rank corr halves) {A['persistence_rank_corr']}")
    f = A["funding_annual_pct"]; ks = list(f)
    print("   lowest:", {k: f[k] for k in ks[:5]}, " highest:", {k: f[k] for k in ks[-5:]})
    print("   spread book:", A["spread_book"])
    print("B. premium fade (bps per trade after costs, by session when the signal fired):")
    for k, v in r["B_premium_fade"].items():
        print(f"   {k}: " + " | ".join(f"{s} n={x['n']} {x['mean_bps']:+.0f}bps hit {x['hit_pct']}% t {x['t']}" + (f" halves {x['first_half']:+.0f}/{x['second_half']:+.0f}" if 'first_half' in x else "") for s, x in v.items()))
    print("C. session drift (mean bps per hour, t-stat):")
    for kind, v in r["C_session_drift"].items():
        print(f"   {kind}: " + " | ".join(f"{s} {x['mean_bps_per_hour']:+.2f} (t {x['t']})" for s, x in v.items() if s != "overnight_rotation"),
              "| overnight rotation:", v.get("overnight_rotation"))
