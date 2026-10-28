"""Swing research lab — which fundamentals actually predict the next move?

Two data sources, same tests:

  history   (default) Rebuilds every feature that can be derived from Binance history —
            returns, relative strength vs BTC, cross-sectional ranks, the live FA's
            momentum shape, funding, stablecoin supply growth — across the most liquid
            perpetuals for up to a year, sampled every 4h. Answers today, no waiting.
  selection Runs scanner/selection.py's live "best 10 tradeable" ranking over the history
            and checks whether the coins it picks actually move more over the next 24h
            (range, net move, how cleanly they trend) than the ones it passes over.
  recorded  Uses what the scanner logs on each full sweep (signals.db scan_features),
            which adds the inputs Binance/DefiLlama keep no history for: open interest,
            long/short ratios, taker flow, protocol and chain TVL and fees. Needs a week
            or more of recording to say anything.

Two questions per feature, each on a train (first 60% of time) / test (last 40%) split:

  pick     Cross-sectional: at each timestamp, does ranking coins by the feature rank
           their next-period return *relative to BTC*? Spearman IC (−1…+1) and the
           top-minus-bottom-fifth return spread. This is "which coin to trade".
  timing   Time-series: when a coin's feature is high *for that coin* (z-score vs its own
           last 30 days), does the coin go up? Pooled IC with forward raw return, and
           the average return of trading in the feature's direction when |z| > 1.
           This is "which way to trade it".

A feature is worth using only if its IC has the same sign on both halves and the test
half is statistically distinct from zero (|t| ≥ 2, counted on non-overlapping periods).
Everything else is noise, however good one half looks.

Usage (from backend/):
    ./venv/bin/python -m research.swing_lab                 # history, 365 days, top 40
    ./venv/bin/python -m research.swing_lab --days 180 --coins 30
    ./venv/bin/python -m research.swing_lab --source selection
    ./venv/bin/python -m research.swing_lab --source recorded
    ./venv/bin/python -m research.swing_lab --refresh       # re-download the history cache

Caveat: the history universe is today's most liquid coins that were already listed at the
start, so coins that died since are missing (survivorship). That flatters "buy strength"
features a little; treat a borderline pass as a fail.
"""

import argparse
import asyncio
import os
import time

import numpy as np
import pandas as pd

from data.defillama_client import fetch_stablecoin_history
from data.derivatives_client import fetch_funding_range, fetch_klines_range
from data.http import get_json

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache")
STEP_H = 4                             # sample every 4h
HORIZONS = (4, 24, 72)                 # forward return horizons, hours
TRAIN_FRAC = 0.6
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDE", "USD1", "DAI", "BUSD", "USDP", "RLUSD"}


def _clamp(x, lo=-1.0, hi=1.0):
    return np.clip(x, lo, hi)


# ── History download (cached) ──────────────────────────────────────────────────

async def _universe(n: int, start_ms: int) -> list[str]:
    info = await get_json("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20.0)
    ok = {s["symbol"] for s in info["symbols"]
          if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
          and s.get("underlyingType") == "COIN" and s.get("status") == "TRADING"
          and s["baseAsset"] not in STABLE_BASES and s.get("onboardDate", 0) < start_ms - 30 * 86_400_000}
    tickers = await get_json("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=20.0)
    ranked = sorted((t for t in tickers if t["symbol"] in ok), key=lambda t: -float(t["quoteVolume"]))
    syms = [t["symbol"] for t in ranked[:n]]
    return syms if "BTCUSDT" in syms else ["BTCUSDT", *syms[: n - 1]]


async def _download(days: int, coins: int, refresh: bool) -> tuple[dict, dict, pd.Series]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"history_{days}d_{coins}c.pkl")
    if os.path.exists(path) and not refresh and time.time() - os.path.getmtime(path) < 86_400:
        return pd.read_pickle(path)

    end = int(time.time() // 3600 * 3600 * 1000)
    start = end - (days + 31) * 86_400_000          # +31d warm-up for 7d/30d lookbacks
    syms = await _universe(coins, start)
    print(f"Downloading {len(syms)} coins × {days + 31}d of 1h klines and funding…", flush=True)
    sem = asyncio.Semaphore(4)

    async def one(sym):
        async with sem:
            k = await fetch_klines_range(sym, "1h", start, end)
            f = await fetch_funding_range(sym, start, end)
            return sym, k, f

    klines, funding = {}, {}
    for sym, k, f in await asyncio.gather(*(one(s) for s in syms)):
        df = pd.DataFrame(k, columns=["t", "o", "h", "l", "c", "v", "qv"]).drop_duplicates("t")
        klines[sym] = df.set_index(pd.to_datetime(df.t, unit="ms", utc=True))[["c", "h", "l", "qv"]]
        fd = pd.DataFrame(f)
        funding[sym] = (pd.Series(fd.rate.values, index=pd.to_datetime(fd.time, unit="ms", utc=True))
                        if len(fd) else pd.Series(dtype=float))
    st = pd.DataFrame(await fetch_stablecoin_history())
    stable = pd.Series(st.usd.values, index=pd.to_datetime(st.date, unit="s", utc=True))
    data = (klines, funding, stable)
    pd.to_pickle(data, path)
    return data


# ── Feature panel ──────────────────────────────────────────────────────────────

def _momentum_shape(h1, h24, d7):
    """The live FA's momentum component (scanner/fundamentals.score_momentum_shape)."""
    return _clamp(_clamp(h1 / 3) * 0.20 + _clamp(h24 / 8) * 0.45 + _clamp(d7 / 20) * 0.35)


def build_panel(klines: dict, funding: dict, stable: pd.Series, days: int) -> pd.DataFrame:
    btc = klines["BTCUSDT"].c
    stable_h = stable.resample("1h").ffill().reindex(btc.index, method="ffill")
    s_g7 = (stable_h / stable_h.shift(24 * 7) - 1) * 100
    s_g30 = (stable_h / stable_h.shift(24 * 30) - 1) * 100
    btc_ret = {h: (btc / btc.shift(h) - 1) * 100 for h in (24, 72, 168)}
    btc_fwd = {h: (btc.shift(-h) / btc - 1) * 100 for h in HORIZONS}

    frames = []
    for sym, k in klines.items():
        c = k.c.reindex(btc.index)
        r = lambda h: (c / c.shift(h) - 1) * 100
        f = pd.DataFrame(index=btc.index)
        f["symbol"] = sym
        f["ret_1h"], f["ret_24h"], f["ret_72h"], f["ret_7d"] = r(1), r(24), r(72), r(168)
        f["rs_24h"] = f.ret_24h - btc_ret[24]
        f["rs_72h"] = f.ret_72h - btc_ret[72]
        f["rs_7d"] = f.ret_7d - btc_ret[168]
        f["momentum_shape"] = _momentum_shape(f.ret_1h, f.ret_24h, f.ret_7d)
        # The price-derived part of the live FA score: relative strength (0.30) + momentum (0.25).
        f["fa_live_price_part"] = (0.30 * _clamp(f.rs_24h / 8) + 0.25 * f.momentum_shape) / 0.55
        qv24 = k.qv.reindex(btc.index).rolling(24).sum()
        f["volume_change_24h"] = (qv24 / qv24.shift(24) - 1) * 100
        # A plain technical-trend proxy, to see how much each FA feature just repeats the chart.
        ema50 = c.ewm(span=50, adjust=False).mean()
        f["trend_vs_ema50"] = (c / ema50 - 1) * 100
        fr = funding[sym]
        if len(fr):
            fr_h = fr.resample("1h").last().ffill().reindex(btc.index, method="ffill")
            f["funding_rate"] = fr_h
            f["funding_7d_avg"] = fr_h.rolling(168).mean()
            m30, sd30 = fr_h.rolling(720).mean(), fr_h.rolling(720).std()
            f["funding_z"] = (f.funding_7d_avg - m30) / sd30.replace(0, np.nan)
        f["stablecoin_growth_7d"] = s_g7
        f["stablecoin_growth_30d"] = s_g30
        f["btc_ret_24h"] = btc_ret[24]
        for h in HORIZONS:
            fwd = (c.shift(-h) / c - 1) * 100
            f[f"fwd_{h}h"] = fwd
            f[f"rel_{h}h"] = fwd - btc_fwd[h]
        frames.append(f)

    panel = pd.concat(frames)
    panel = panel[panel.index.hour % STEP_H == 0]
    panel = panel[panel.index >= panel.index.max() - pd.Timedelta(days=days)]
    panel.index.name = "ts"
    panel = panel.reset_index()
    for col in ("rs_24h", "rs_7d"):
        panel[f"rank_{col[3:]}"] = panel.groupby("ts")[col].rank(pct=True) * 2 - 1
    return panel


HISTORY_FEATURES = [
    "ret_24h", "ret_7d", "rs_24h", "rs_72h", "rs_7d", "rank_24h", "rank_7d", "momentum_shape",
    "fa_live_price_part", "volume_change_24h", "funding_rate", "funding_7d_avg", "funding_z",
    "trend_vs_ema50",
]
GLOBAL_FEATURES = ["stablecoin_growth_7d", "stablecoin_growth_30d", "btc_ret_24h"]


# ── Tests ──────────────────────────────────────────────────────────────────────

def _t(series: pd.Series, horizon_h: int) -> float:
    """t-stat of a per-timestamp IC series, counted on non-overlapping periods only."""
    s = series.dropna()
    s = s.iloc[:: max(1, horizon_h // STEP_H)]
    return s.mean() / s.std() * np.sqrt(len(s)) if len(s) > 2 and s.std() > 0 else np.nan


def cross_sectional(panel: pd.DataFrame, feat: str, h: int, min_coins: int = 8) -> dict:
    d = panel[["ts", "symbol", feat, f"rel_{h}h"]].dropna()
    d = d[d.symbol != "BTCUSDT"]
    d = d[d.groupby("ts").symbol.transform("size") >= min_coins]
    if d.empty:
        return {}
    rx = d.groupby("ts")[feat].rank()
    ry = d.groupby("ts")[f"rel_{h}h"].rank()
    d = d.assign(rx=rx - rx.groupby(d.ts).transform("mean"), ry=ry - ry.groupby(d.ts).transform("mean"))
    g = d.assign(xy=d.rx * d.ry, xx=d.rx ** 2, yy=d.ry ** 2).groupby("ts")[["xy", "xx", "yy"]].sum()
    ic = g.xy / np.sqrt(g.xx * g.yy)
    q = d.groupby("ts")[feat].transform(lambda x: pd.qcut(x.rank(method="first"), 5, labels=False))
    top, bottom = d[q == 4].groupby("ts")[f"rel_{h}h"], d[q == 0].groupby("ts")[f"rel_{h}h"]
    cut = ic.index[int(len(ic) * TRAIN_FRAC)]
    test = lambda x: x[x.index >= cut]
    # Mean and median spread: crypto returns are skewed (a few pumps), so a mean spread can
    # be large while the typical coin shows nothing — the IC is the arbiter.
    return {"ic_train": ic[ic.index < cut].mean(), "ic_test": test(ic).mean(), "t_test": _t(test(ic), h),
            "spread_mean_bps": test(top.mean() - bottom.mean()).mean() * 100,
            "spread_median_bps": test(top.median() - bottom.median()).median() * 100, "n_ts": len(ic)}


def time_series(panel: pd.DataFrame, feat: str, h: int) -> dict:
    d = panel[["ts", "symbol", feat, f"fwd_{h}h"]].dropna().sort_values("ts")
    if d.empty:
        return {}
    win = 30 * 24 // STEP_H
    g = d.groupby("symbol")[feat]
    mean = g.transform(lambda x: x.rolling(win, min_periods=win // 2).mean())
    sd = g.transform(lambda x: x.rolling(win, min_periods=win // 2).std())
    d["z"] = (d[feat] - mean) / sd.replace(0, np.nan)
    d = d.dropna(subset=["z"])
    if d.empty:
        return {}
    cut = d.ts.quantile(TRAIN_FRAC)
    out = {}
    for name, part in (("train", d[d.ts < cut]), ("test", d[d.ts >= cut])):
        out[f"ic_{name}"] = part.z.rank().corr(part[f"fwd_{h}h"].rank())      # Spearman
        strong = part[part.z.abs() > 1]
        out[f"edge_{name}_bps"] = (np.sign(strong.z) * strong[f"fwd_{h}h"]).mean() * 100 if len(strong) else np.nan
    # Pooled correlations overstate significance (coins move together), so the t-stat is on
    # the per-timestamp return of trading every coin in its feature's direction, non-overlapping.
    test = d[d.ts >= cut]
    per_ts = (np.sign(test.z) * test[f"fwd_{h}h"]).groupby(test.ts).mean()
    out["t_test"] = _t(per_ts, h)
    return out


def verdict(row: dict) -> str:
    a, b, t = row.get("ic_train"), row.get("ic_test"), row.get("t_test")
    if any(v is None or np.isnan(v) for v in (a, b, t)):
        return "no data"
    if np.sign(a) == np.sign(b) and abs(t) >= 2:
        return "HOLDS (+)" if b > 0 else "HOLDS (−, inverse)"
    if np.sign(a) == np.sign(b) and abs(t) >= 1.3:
        return "weak"
    return "noise"


def report(panel: pd.DataFrame, features: list[str], global_features: list[str]) -> list[dict]:
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 20)
    results = []
    for h in (24, 72):
        rows = []
        for feat in features:
            if feat.startswith(("rs_", "rank_")):
                continue      # same cross-sectional order as the raw return (BTC's move is common to all)
            r = cross_sectional(panel, feat, h)
            if r:
                rows.append({"feature": feat, **r, "verdict": verdict(r)})
        tbl = pd.DataFrame(rows)
        if tbl.empty:
            print(f"\nPICK {h}h — not enough data yet (needs ≥8 coins per timestamp with a known {h}h outcome)")
            continue
        print(f"\nPICK — rank coins by feature vs their {h}h return relative to BTC "
              f"(Spearman IC; spread = top fifth − bottom fifth, bps per {h}h)")
        print(tbl.round(3).to_string(index=False))
        results += [{"test": f"pick_{h}h", **r} for r in rows]

    for h in (24, 72):
        rows = []
        for feat in features:
            r = time_series(panel, feat, h)
            if r:
                rows.append({"feature": feat, **r, "verdict": verdict(r)})
        for feat in global_features:
            r = time_series(panel, feat, h)
            if r:
                rows.append({"feature": f"{feat} (market-wide)", **r, "verdict": verdict(r)})
        tbl = pd.DataFrame(rows)
        if tbl.empty:
            print(f"\nTIMING {h}h — not enough data yet (needs ~2 weeks per coin for its 30-day baseline)")
            continue
        print(f"\nTIMING — when the feature is high for that coin (z vs its own 30d), does it rise over {h}h? "
              f"(edge = avg return trading the feature's sign when |z| > 1, bps)")
        print(tbl.round(3).to_string(index=False))
        results += [{"test": f"timing_{h}h", **r} for r in rows]
    return results


def overlap(panel: pd.DataFrame, features: list[str]) -> None:
    cols = [f for f in features if f in panel and f != "trend_vs_ema50"]
    c = panel[cols + ["trend_vs_ema50"]].corr(method="spearman")["trend_vs_ema50"].drop("trend_vs_ema50")
    print("\nOVERLAP WITH THE CHART — Spearman correlation with price vs its 1h EMA50 "
          "(above ~0.6 means the feature mostly repeats what the technical score already sees)")
    print(c.round(2).sort_values(ascending=False).to_string())


# ── Recorded scans ─────────────────────────────────────────────────────────────

async def recorded_panel(days: float | None) -> pd.DataFrame:
    from storage import signals

    rows = signals.scan_features(time.time() - days * 86400 if days else 0)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{"ts": r["ts"], "symbol": r["symbol"], "price": r["price"], "conviction": r["conviction"],
                        "fa_score": r["fa_score"], "ta_score": r["ta_score"], **r["features"]} for r in rows])
    # One scan per coin per 4h — the same step as the history panel, so the non-overlap
    # t-stats and 30-day z-score windows mean the same thing in both modes.
    df["ts"] = pd.to_datetime(df.ts, unit="s", utc=True).dt.floor(f"{STEP_H}h")
    df = df.drop_duplicates(["symbol", "ts"], keep="first").reset_index(drop=True)
    start, end = int(df.ts.min().timestamp() * 1000), int(time.time() * 1000)
    syms = sorted(set(df.symbol) | {"BTCUSDT"})
    kl = dict(zip(syms, await asyncio.gather(*(fetch_klines_range(s, "1h", start - 3_600_000, end) for s in syms),
                                             return_exceptions=True)))
    closes = {s: pd.Series([r[4] for r in k], index=pd.to_datetime([r[0] for r in k], unit="ms", utc=True))
              for s, k in kl.items() if isinstance(k, list) and k}

    def fwd(sym, ts, h):
        s = closes.get(sym)
        if s is None:
            return np.nan
        t0 = ts.floor("1h")
        a, b = s.get(t0), s.get(t0 + pd.Timedelta(hours=h))
        return (b / a - 1) * 100 if a and b else np.nan

    for h in HORIZONS:
        df[f"fwd_{h}h"] = [fwd(s, t, h) for s, t in zip(df.symbol, df.ts)]
        btc = {t: fwd("BTCUSDT", t, h) for t in df.ts.unique()}
        df[f"rel_{h}h"] = df[f"fwd_{h}h"] - df.ts.map(btc)
    return df


RECORDED_EXTRA = [
    "fa_score", "ta_score", "conviction", "oi_change_24h_pct", "oi_change_7d_pct", "oi_price_confirm", "taker_buy_sell_24h",
    "long_account_pct", "top_trader_long_pct", "basis_pct", "protocol_tvl_change_7d_pct",
    "protocol_fees_change_7d_pct", "chain_tvl_change_7d_pct", "chain_fees_change_7d_pct", "turnover",
]


# ── Selection check ────────────────────────────────────────────────────────────

def selection_check(klines: dict, days: int, size: int = 10) -> None:
    """Replay the live tradeability ranking every 4h and score what it picked by the next 24h."""
    from scanner.selection import score_pool

    closes = pd.DataFrame({s: k.c for s, k in klines.items()})
    highs = pd.DataFrame({s: k.h for s, k in klines.items()})
    lows = pd.DataFrame({s: k.l for s, k in klines.items()})
    qv = pd.DataFrame({s: k.qv for s, k in klines.items()})
    ret = lambda h: (closes / closes.shift(h) - 1) * 100
    r1, r24, r7d = ret(1), ret(24), ret(168)
    qv24 = qv.rolling(24).sum()
    hi24, lo24 = highs.rolling(24).max(), lows.rolling(24).min()
    # Outcomes over the next 24h: range, absolute net move, and trend efficiency
    # (net move ÷ path length: 1 = a straight line, ~0 = chop).
    f_hi, f_lo = hi24.shift(-24), lo24.shift(-24)
    f_range = (f_hi - f_lo) / closes * 100
    f_move = ((closes.shift(-24) / closes - 1) * 100).abs()
    path = closes.pct_change().abs().rolling(24).sum().shift(-24) * 100
    f_eff = f_move / path

    stamps = closes.index[(closes.index.hour % STEP_H == 0)
                          & (closes.index >= closes.index.max() - pd.Timedelta(days=days))
                          & (closes.index <= closes.index.max() - pd.Timedelta(hours=24))]
    rows = []
    for ts in stamps:
        pool, tickers = [], {}
        for s in closes.columns:
            if np.isnan(r7d.at[ts, s]) or np.isnan(qv24.at[ts, s]):
                continue
            c = closes.at[ts, s]
            prev_qv = qv24.shift(24).at[ts, s]
            pool.append({"pair": s, "symbol": s.removesuffix("USDT"), "percent_change_1h": r1.at[ts, s],
                         "percent_change_7d": r7d.at[ts, s],
                         "volume_change_24h": (qv24.at[ts, s] / prev_qv - 1) * 100 if prev_qv else 0})
            tickers[s] = {"quote_volume": qv24.at[ts, s], "price_change_pct": r24.at[ts, s],
                          "range_pct": (hi24.at[ts, s] - lo24.at[ts, s]) / c * 100}
        ranked = score_pool(pool, tickers, set())
        for i, c in enumerate(ranked):
            s = c["pair"]
            rows.append({"ts": ts, "symbol": s, "picked": i < size, "score": c["tradeability"],
                         "range": f_range.at[ts, s], "move": f_move.at[ts, s], "eff": f_eff.at[ts, s]})
    df = pd.DataFrame(rows).dropna()
    cut = df.ts.quantile(TRAIN_FRAC)
    print(f"Selection check — live tradeability ranking replayed at {df.ts.nunique():,} timestamps over "
          f"{df.symbol.nunique()} coins; top {size} vs the rest, next-24h outcomes (test = last 40% of time)\n")
    out = []
    for name, part in (("all", df), ("test", df[df.ts >= cut])):
        for picked, g in part.groupby("picked"):
            out.append({"period": name, "group": f"top {size}" if picked else "passed over", "coin-periods": len(g),
                        "24h range %": g.range.mean(), "24h |move| %": g.move.mean(),
                        "trend efficiency": g.eff.mean()})
    print(pd.DataFrame(out).round(3).to_string(index=False))
    for col, label in (("range", "range"), ("move", "|move|"), ("eff", "efficiency")):
        ic = df.groupby("ts").apply(lambda g: g.score.rank().corr(g[col].rank()), include_groups=False)
        test = ic[ic.index >= cut]
        print(f"   score vs next-24h {label:10}: rank IC {ic.mean():+.3f} (test {test.mean():+.3f}, t {_t(test, 24):.1f})")
    print("\nRange and |move| are what the ranking is built to find; efficiency is what a trend "
          "follower needs. A ranking that finds movement but not cleaner trends picks volatility, not edge.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", choices=("history", "selection", "recorded"), default="history")
    ap.add_argument("--days", type=float, default=None, help="history: days to test (default 365); "
                                                             "recorded: only the last N days")
    ap.add_argument("--coins", type=int, default=40, help="history: most liquid N perpetuals")
    ap.add_argument("--refresh", action="store_true", help="history: re-download instead of using the cache")
    args = ap.parse_args()

    if args.source == "selection":
        days = int(args.days or 365)
        klines, _, _ = asyncio.run(_download(days, args.coins, args.refresh))
        selection_check(klines, days)
    elif args.source == "history":
        days = int(args.days or 365)
        klines, funding, stable = asyncio.run(_download(days, args.coins, args.refresh))
        panel = build_panel(klines, funding, stable, days)
        print(f"Swing lab — history: {panel.symbol.nunique()} coins, {panel.ts.nunique():,} timestamps "
              f"({panel.ts.min():%Y-%m-%d} → {panel.ts.max():%Y-%m-%d}), train/test split at "
              f"{TRAIN_FRAC:.0%} of time")
        report(panel, HISTORY_FEATURES, GLOBAL_FEATURES)
        overlap(panel, HISTORY_FEATURES)
    else:
        panel = asyncio.run(recorded_panel(args.days))
        if panel.empty:
            print("No scans recorded yet — the scanner writes one row per coin every 5 minutes while running.")
            return
        span = (panel.ts.max() - panel.ts.min()).total_seconds() / 86400
        print(f"Swing lab — recorded: {len(panel):,} scans, {panel.symbol.nunique()} coins over {span:.1f} days")
        if span < 7:
            print("Under a week of data: treat everything below as a smoke test, not evidence.")
        feats = [f for f in HISTORY_FEATURES + RECORDED_EXTRA if f in panel and panel[f].notna().sum() > 50]
        glob = [f for f in ("stablecoin_growth_7d_pct", "btc_dominance_pct", "market_cap_change_24h_pct")
                if f in panel]
        report(panel, feats, glob)


if __name__ == "__main__":
    main()
