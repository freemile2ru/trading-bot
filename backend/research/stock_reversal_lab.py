"""Stock reversal lab — does the crypto reversal score work on Binance's US stock perps? (2026-11-12, pre-registered)

Why: the user asked why the stock side failed while crypto looked promising, and to close the gap. The review found the
two sides were never asked the same question. What looks promising in crypto is the reversal score (autotrader/reversal.py,
from research/entry_lab.py): fade the coins whose positioning and flow are most crowded — open-interest growth, taker
buying, funding, 24h/7d return — and lean with the top traders. On stocks we only tested the scanner's momentum
conviction (research/stock_setup_lab.py, which also failed on crypto and was retired) and price-only rules
(research/equity_lab.py, stock_strategies_lab.py). Binance publishes the same positioning data for its stock perps.

Method, fixed before looking:
  universe  the 40 most-traded US stock/ETF perps (TRADIFI_PERPETUAL, EQUITY) and, as the control, the 40 most-traded
            crypto perps — same code, same window
  window    the last 30 days (Binance keeps 30 days of open interest, taker and top-trader history)
  readings  every 4h mark, from 1h data: ret_24h, ret_7d, oi_change_24h_pct, oi_change_7d_pct, volume_change_24h,
            funding_rate, taker_buy_sell_24h, top_trader_long_pct
  score     the crypto lab's signs, NOT refitted: each reading's cross-sectional percentile rank, signed, averaged
  target    xs_24h: the next 24h return minus the cross-section's mean (%); xs_4h beside it
  stats     per-mark Spearman IC averaged; the 24h labels overlap six marks, so t is divided by √6
  book      long the top 5 / short the bottom 5 of the score for 24h, 8 bps a side, each leg's funding paid or received
  sessions  stock marks split by when the score was read: US cash open, weekday closed, weekend
  verdict   the score "works on stocks" when its IC ≥ 0.03 with overlap-adjusted t ≥ 2, the same sign in both halves,
            and the 5/5 book is positive after costs in the second half. Otherwise the per-reading table says which
            crypto readings carry over and which flip — that is the gap.

    ./venv/bin/python -m research.stock_reversal_lab
"""
from __future__ import annotations

import json
import os
import time

import httpx
import numpy as np
import pandas as pd

FAPI = "https://fapi.binance.com"
CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "stock_reversal")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "stock_reversal_results.json")
N_NAMES = 40
DAYS = 30
TOP_K = 5
COST_BPS = 8.0
IC_MIN = 0.03
SIGNS = {"ret_24h": -1.0, "ret_7d": -1.0, "oi_change_24h_pct": -1.0, "oi_change_7d_pct": -1.0, "volume_change_24h": -1.0,
         "funding_rate": -1.0, "taker_buy_sell_24h": -1.0, "top_trader_long_pct": 1.0}       # autotrader/reversal.DEFAULT_CHOSEN
US_OPEN = (13 * 60 + 30, 20 * 60)                                                         # UTC minutes, DST ignored


def _get(c: httpx.Client, path: str, **params) -> list:
    for attempt in range(4):
        r = c.get(f"{FAPI}{path}", params=params)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (418, 429):
            time.sleep(10 * (attempt + 1))
            continue
        return []
    return []


def universes(c: httpx.Client) -> dict[str, list[str]]:
    info = c.get(f"{FAPI}/fapi/v1/exchangeInfo").json()
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in c.get(f"{FAPI}/fapi/v1/ticker/24hr").json()}
    stocks = [s["symbol"] for s in info["symbols"] if s["contractType"] == "TRADIFI_PERPETUAL" and s["status"] == "TRADING"
              and (s.get("underlyingType") or "EQUITY") == "EQUITY" and not any("pre" in x.lower() for x in (s.get("underlyingSubType") or []))]
    crypto = [s["symbol"] for s in info["symbols"] if s["contractType"] == "PERPETUAL" and s["status"] == "TRADING"
              and s["quoteAsset"] == "USDT" and s.get("underlyingType") == "COIN"]
    top = lambda xs: sorted(xs, key=lambda s: -vol.get(s, 0))[:N_NAMES]
    return {"stocks": top(stocks), "crypto": top(crypto)}


def _paged(c: httpx.Client, path: str, symbol: str, start_ms: int, end_ms: int, **extra) -> list:
    out, cursor = [], start_ms
    while cursor < end_ms:
        rows = _get(c, path, symbol=symbol, startTime=cursor, endTime=end_ms, limit=500, **extra)
        if not rows:
            break
        out += rows
        last = int(rows[-1]["timestamp"] if isinstance(rows[-1], dict) else rows[-1][0])
        if len(rows) < 500 or last + 1 <= cursor:
            break
        cursor = last + 1
    return out


def fetch(c: httpx.Client, symbol: str, end: int) -> pd.DataFrame | None:
    path = os.path.join(CACHE, f"{symbol}_{end // 86_400_000}.pkl")
    if os.path.exists(path):
        return pd.read_pickle(path)
    start_px = end - (DAYS + 9) * 86_400_000                          # 7d readings need a week before the window
    start_pos = end - (DAYS - 0.1) * 86_400_000
    k = []
    cur = start_px
    while cur < end:
        rows = _get(c, "/fapi/v1/klines", symbol=symbol, interval="1h", startTime=cur, endTime=end, limit=1000)
        if not rows:
            break
        k += rows
        if len(rows) < 1000:
            break
        cur = rows[-1][0] + 1
    if len(k) < 24 * 10:
        return None
    df = pd.DataFrame({"ts": [r[0] for r in k], "close": [float(r[4]) for r in k], "qvol": [float(r[7]) for r in k]}).set_index("ts")
    oi = _paged(c, "/futures/data/openInterestHist", symbol, int(start_pos), end, period="1h")
    tk = _paged(c, "/futures/data/takerlongshortRatio", symbol, int(start_pos), end, period="1h")
    tp = _paged(c, "/futures/data/topLongShortPositionRatio", symbol, int(start_pos), end, period="1h")
    fr = _get(c, "/fapi/v1/fundingRate", symbol=symbol, startTime=start_px, endTime=end, limit=1000)
    hour = lambda ms: int(ms) // 3_600_000 * 3_600_000
    df["oi"] = pd.Series({hour(r["timestamp"]): float(r["sumOpenInterestValue"]) for r in oi})
    df["buy"] = pd.Series({hour(r["timestamp"]): float(r["buyVol"]) for r in tk})
    df["sell"] = pd.Series({hour(r["timestamp"]): float(r["sellVol"]) for r in tk})
    df["top_long"] = pd.Series({hour(r["timestamp"]): float(r["longAccount"]) for r in tp})
    f = pd.Series({hour(r["fundingTime"]): float(r["fundingRate"]) for r in fr}, dtype=float)
    df["funding"] = f.reindex(df.index).ffill() if len(f) else np.nan
    df["funding_paid"] = f.reindex(df.index).fillna(0.0) if len(f) else 0.0     # the rate charged at each settlement hour
    os.makedirs(CACHE, exist_ok=True)
    df.to_pickle(path)
    return df


def readings(sym: str, df: pd.DataFrame, marks: list[int]) -> list[dict]:
    rows = []
    for t in marks:
        if t not in df.index:
            continue
        i = df.index.get_loc(t)
        if i < 24 * 7 or i + 24 >= len(df):
            continue
        c = df["close"]
        w24, w48 = df.iloc[i - 23:i + 1], df.iloc[i - 47:i - 23]
        oi = df["oi"]
        buy, sell = w24["buy"].sum(), w24["sell"].sum()
        fwd24 = (c.iloc[i + 24] / c.iloc[i] - 1) * 100
        fwd4 = (c.iloc[i + 4] / c.iloc[i] - 1) * 100
        rows.append({
            "bucket": t, "symbol": sym,
            "ret_24h": (c.iloc[i] / c.iloc[i - 24] - 1) * 100, "ret_7d": (c.iloc[i] / c.iloc[i - 168] - 1) * 100,
            "oi_change_24h_pct": (oi.iloc[i] / oi.iloc[i - 24] - 1) * 100 if oi.iloc[i - 24] and not np.isnan(oi.iloc[i - 24]) else np.nan,
            "oi_change_7d_pct": (oi.iloc[i] / oi.iloc[i - 168] - 1) * 100 if not np.isnan(oi.iloc[i - 168]) and oi.iloc[i - 168] else np.nan,
            "volume_change_24h": (w24["qvol"].sum() / w48["qvol"].sum() - 1) * 100 if w48["qvol"].sum() else np.nan,
            "funding_rate": df["funding"].iloc[i], "taker_buy_sell_24h": buy / sell if sell else np.nan,
            "top_trader_long_pct": df["top_long"].iloc[i],
            "fwd_24h": fwd24, "fwd_4h": fwd4,
            "funding_24h_pct": float(df["funding_paid"].iloc[i + 1:i + 25].sum() * 100),   # what a long pays over the hold
        })
    return rows


def rank_ic(df: pd.DataFrame, col: str, target: str) -> pd.Series:
    out = {}
    for b, g in df.groupby("bucket"):
        m = g[col].notna() & g[target].notna()
        if m.sum() >= 8:
            out[b] = g.loc[m, col].rank().corr(g.loc[m, target].rank())
    return pd.Series(out).dropna().sort_index()


def _ic_row(s: pd.Series, overlap: int) -> dict:
    if len(s) < 12:
        return {"n": int(len(s))}
    half = len(s) // 2
    sd = s.std(ddof=1)
    t = float(s.mean() / (sd / np.sqrt(len(s)))) / np.sqrt(overlap) if sd > 0 else 0.0
    return {"ic": round(float(s.mean()), 4), "first": round(float(s.iloc[:half].mean()), 4), "second": round(float(s.iloc[half:].mean()), 4),
            "t_adj": round(t, 2), "n": int(len(s))}


def score_of(df: pd.DataFrame, signs: dict[str, float]) -> pd.Series:
    parts = [sign * (df.groupby("bucket")[c].rank(pct=True) - 0.5) for c, sign in signs.items() if df[c].notna().mean() > 0.5]
    return pd.concat(parts, axis=1).mean(axis=1)


def book(df: pd.DataFrame, score: pd.Series, k: int = TOP_K) -> dict:
    rets = []
    for b, g in df.assign(score=score).dropna(subset=["score", "fwd_24h"]).groupby("bucket"):
        if len(g) < 2 * k + 2:
            continue
        g = g.sort_values("score")
        lo, hi = g.iloc[:k], g.iloc[-k:]
        long_leg = (hi["fwd_24h"] - hi["funding_24h_pct"]).mean()
        short_leg = (-lo["fwd_24h"] + lo["funding_24h_pct"]).mean()
        rets.append((b, (long_leg + short_leg) / 2 - 2 * COST_BPS / 100))
    if not rets:
        return {"marks": 0}
    s = pd.Series(dict(rets)).sort_index()
    half = len(s) // 2
    daily = s.iloc[::6]                                                     # one non-overlapping slot a day for the t
    return {"marks": int(len(s)), "avg_pct": round(float(s.mean()), 4), "first_half_pct": round(float(s.iloc[:half].mean()), 4),
            "second_half_pct": round(float(s.iloc[half:].mean()), 4), "positive_pct": round(float((s > 0).mean() * 100), 1),
            "t_daily": round(float(daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily)))), 2) if len(daily) > 2 and daily.std(ddof=1) > 0 else None}


def session_of(ms: int) -> str:
    d = pd.Timestamp(ms, unit="ms", tz="UTC")
    if d.weekday() >= 5:
        return "weekend"
    m = d.hour * 60 + d.minute
    return "us_open" if US_OPEN[0] <= m < US_OPEN[1] else "weekday_closed"


def analyse(panel: pd.DataFrame) -> dict:
    panel = panel.copy()
    panel["xs_24h"] = panel["fwd_24h"] - panel.groupby("bucket")["fwd_24h"].transform("mean")
    panel["xs_4h"] = panel["fwd_4h"] - panel.groupby("bucket")["fwd_4h"].transform("mean")
    readings_ic = {c: {"sign_in_crypto_lab": SIGNS[c], **_ic_row(rank_ic(panel, c, "xs_24h"), 6), "coverage": round(float(panel[c].notna().mean()), 2)}
                   for c in SIGNS}
    panel["score"] = score_of(panel, SIGNS)
    sc24, sc4 = rank_ic(panel, "score", "xs_24h"), rank_ic(panel, "score", "xs_4h")
    out = {"names": int(panel["symbol"].nunique()), "marks": int(panel["bucket"].nunique()),
           "readings": readings_ic, "score_ic_24h": _ic_row(sc24, 6), "score_ic_4h": _ic_row(sc4, 1),
           "book": book(panel, panel["score"]),
           "always_long_24h_pct": round(float(panel["fwd_24h"].mean()), 4)}
    sess = panel["bucket"].map(session_of)
    out["by_session"] = {s: {"score_ic_24h": _ic_row(rank_ic(panel[sess == s], "score", "xs_24h"), 6),
                             "book": book(panel[sess == s], panel.loc[sess == s, "score"])} for s in ("us_open", "weekday_closed", "weekend")}
    # Readings whose sign agrees on this market — what a re-signed score would look like (shown, not adopted: it is fitted here)
    agree = {c: SIGNS[c] for c, r in readings_ic.items() if r.get("ic") is not None and np.sign(r["ic"]) == 1}
    out["agreeing_readings"] = sorted(agree)
    return out


def verdict(r: dict) -> str:
    s, b = r["score_ic_24h"], r["book"]
    ok = (s.get("ic", 0) >= IC_MIN and s.get("t_adj", 0) >= 2 and s.get("first", 0) > 0 and s.get("second", 0) > 0
          and b.get("second_half_pct", -1) > 0)
    return "works" if ok else "no edge"


def main() -> dict:
    t0 = time.time()
    end = int(time.time() // 3600 * 3600 * 1000)
    with httpx.Client(timeout=30) as c:
        unis = universes(c)
        start_mark = end - (DAYS - 7) * 86_400_000                           # the first mark with a full week of positioning behind it
        marks = list(range(start_mark // 14_400_000 * 14_400_000, end - 24 * 3_600_000, 14_400_000))
        res = {}
        for name, syms in unis.items():
            rows = []
            for s in syms:
                df = fetch(c, s, end)
                if df is not None:
                    rows += readings(s, df, marks)
            panel = pd.DataFrame(rows)
            res[name] = analyse(panel)
            res[name]["verdict"] = verdict(res[name])
            res[name]["symbols"] = syms
    out = {"generated_at": time.time(), "window_days": DAYS, "first_mark": start_mark, "end": end, "signs": SIGNS,
           "results": res, "seconds": round(time.time() - t0)}
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1, default=float)
    return out


if __name__ == "__main__":
    o = main()
    for name, r in o["results"].items():
        print(f"\n== {name}: {r['names']} names, {r['marks']} marks — {r['verdict']}")
        print("  score IC 24h", r["score_ic_24h"], "| 4h", r["score_ic_4h"])
        print("  book 5/5", r["book"], "| always-long 24h", r["always_long_24h_pct"])
        for c, x in r["readings"].items():
            print(f"   {c:22} sign {x['sign_in_crypto_lab']:+.0f}  ic {x.get('ic')}  1st {x.get('first')}  2nd {x.get('second')}  t {x.get('t_adj')}  cov {x['coverage']}")
        for s, x in r["by_session"].items():
            print(f"   session {s:15} IC {x['score_ic_24h']}  book {x['book']}")
