"""Setup lab — do the technical score, the named setups and the entry gates make money?

The swing system rests on the technical score (analysis/technical.py), the named setups
(scanner/strategies.py) and two gates built from them: the autotrader's bias filter and the
scanner's escalation bar. This replays history through that production code:

  * every 4h, for each coin, the scan is rebuilt from closed 15m/30m/1h/4h candles
    (resampled from Binance 15m perpetual klines) — same TA, same setups, same conviction
    (fundamentals set to neutral: the FA lab found no edge there, and it's switched off)
  * a trade is simulated in each direction from the next 15m open, with the autotrader's own
    stop/targets (strategy.build_levels) and the live exit policy (exits.apply_bar, trailing
    stop, max hold by timeframe), costs in R: taker fees and slippage both ways
  * every signal is then judged by the R of the trade in *its* direction, next to what
    always-long and always-short made at the same moments (the market's drift)

Also breaks the gated trades down by market conditions (BTC trend, BTC volatility, hour,
weekday) — the "when should the system stand aside" question.

What it leaves out: Claude's confirmation, the 15m timing trigger, limit entries, news,
macro and funding. So it measures the deterministic signal the rest is built on. Candles
are closed ones (live scans also see the forming candle).

Usage (from backend/):
    ./venv/bin/python -m research.setup_lab                    # 180 days, 30 coins
    ./venv/bin/python -m research.setup_lab --days 365 --coins 40
    ./venv/bin/python -m research.setup_lab --refresh          # re-download candles

Takes a few minutes (≈30k scans, run in parallel). Results are cached per data set.
"""

import argparse
import asyncio
import os
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR, _universe

STEP_H = 4
TRAIN_FRAC = 0.6
TF_RULES = {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h"}
TF_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}
BARS = 200
WARMUP_DAYS = 35                 # 200 × 4h bars


# ── Data ───────────────────────────────────────────────────────────────────────

async def _download(days: int, coins: int, refresh: bool) -> dict[str, pd.DataFrame]:
    from data.derivatives_client import fetch_klines_range

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"klines15m_{days}d_{coins}c.pkl")
    if os.path.exists(path) and not refresh and time.time() - os.path.getmtime(path) < 86_400:
        return pd.read_pickle(path)
    end = int(time.time() // 900 * 900 * 1000)
    start = end - (days + WARMUP_DAYS) * 86_400_000
    syms = await _universe(coins, start)
    print(f"Downloading 15m klines for {len(syms)} coins × {days + WARMUP_DAYS}d…", flush=True)
    sem = asyncio.Semaphore(4)

    async def one(sym):
        async with sem:
            return sym, await fetch_klines_range(sym, "15m", start, end)

    out = {}
    for sym, rows in await asyncio.gather(*(one(s) for s in syms)):
        df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume", "qv"]).drop_duplicates("t")
        out[sym] = df.set_index(pd.to_datetime(df.t, unit="ms", utc=True))[["open", "high", "low", "close", "volume"]]
    pd.to_pickle(out, path)
    return out


def _candles(df: pd.DataFrame) -> list[dict]:
    ot = (df.index.astype("int64") // 1_000_000).tolist()
    return [{"open_time": t, "open": o, "high": h, "low": lo, "close": c, "volume": v}
            for t, o, h, lo, c, v in zip(ot, df.open.tolist(), df.high.tolist(), df.low.tolist(),
                                         df.close.tolist(), df.volume.tolist())]


# ── One coin (runs in a worker process) ────────────────────────────────────────

def _simulate(direction: str, entry_bar: int, bars15: pd.DataFrame, scan: dict, fast: dict) -> dict:
    from autotrader import strategy
    from trading import exits, futures

    o = bars15.open.values
    h, lo, c = bars15.high.values, bars15.low.values, bars15.close.values
    ends = (bars15.index.astype("int64") // 1_000_000_000).values + 900
    entry = futures.slipped(o[entry_bar], direction, opening=True)
    lv = strategy.build_levels(direction, entry, scan, fast)
    t = {"symbol": scan["symbol"], "direction": direction, "entry_price": entry, "quantity": 1.0,
         "stop_loss": lv["stop_loss"], "take_profit_1": lv["take_profit_1"], "take_profit_2": lv["take_profit_2"],
         "timeframe": scan.get("best_timeframe") or "1h"}
    exits.stamp(t, exits.DEFAULT_POLICY, now=ends[entry_bar] - 900)
    r_unit = t["r_unit"]
    s = 1 if direction == "long" else -1
    for i in range(entry_bar, len(o)):
        hit = exits.apply_bar(t, o[i], h[i], lo[i], c[i], float(ends[i]))
        if hit:
            reason, px = hit
            if reason in ("stop_loss", "tightened_stop", "breakeven", "trailing_stop", "liquidated"):
                px = futures.slipped(px, direction, opening=False)
            cost = entry * 2 * futures.TAKER_FEE
            return {"r": ((px - entry) * s - cost) / r_unit, "exit": reason, "bars": i - entry_bar + 1,
                    "sl_pct": lv["sl_pct"]}
    return {"r": np.nan, "exit": "open", "bars": len(o) - entry_bar, "sl_pct": lv["sl_pct"]}


def run_coin(args) -> list[dict]:
    sym, bars15, days = args
    from analysis.scoring import build_signal_package
    from analysis.technical import compute_multi_timeframe_ta
    from autotrader import strategy
    from scanner import escalation
    from scanner.engine import _extract_triggers, compute_conviction
    from scanner.strategies import detect_setups

    frames = {tf: bars15 if tf == "15m" else bars15.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        for tf, rule in TF_RULES.items()}
    first = bars15.index.max() - pd.Timedelta(days=days)
    steps = [ts for ts in pd.date_range(first.ceil(f"{STEP_H}h"), bars15.index.max() - pd.Timedelta(hours=8),
                                        freq=f"{STEP_H}h")]
    pos15 = {ts: i for i, ts in enumerate(bars15.index)}
    out = []
    for ts in steps:
        if ts not in pos15:
            continue
        ohlcv = {}
        for tf, df in frames.items():
            closed = df[df.index + pd.Timedelta(milliseconds=TF_MS[tf]) <= ts].tail(BARS)
            ohlcv[tf] = _candles(closed) if len(closed) >= 60 else []
        if not all(ohlcv.values()):
            continue
        ta = compute_multi_timeframe_ta(ohlcv)
        pkg = build_signal_package(symbol=sym, ta_result=ta, news={}, macro={},
                                   fear_greed={"value": 50}, social={}, coingecko={})
        tf_signals = pkg["ta_summary"]["timeframe_signals"]
        setups = detect_setups(tf_signals, pkg.get("tf_alignment", {}))
        conv = compute_conviction(pkg["composite_score"], setups, 0.0)
        scan = {
            "symbol": sym, "price": ohlcv["15m"][-1]["close"], "conviction": conv["conviction"],
            "confidence": min(95, pkg.get("confidence_pct", 0) + 3 * len(conv["aligned_setups"])),
            "conviction_detail": conv, "tf_alignment": pkg.get("tf_alignment", {}), "fa": None,
            "best_timeframe": pkg.get("best_timeframe"), "signal_package": pkg,
            "triggers": _extract_triggers(tf_signals, pkg.get("best_timeframe", "1h"),
                                          (pkg.get("stop_loss_pct") or 0.0) / 1.5),
        }
        _, fast = strategy.fast_tf(scan)
        bias_dir, _ = strategy.bias(scan)
        qualified, why = escalation.qualifies(scan)
        row = {"ts": ts, "symbol": sym, "conviction": conv["conviction"], "composite": pkg["composite_score"],
               "confidence": scan["confidence"], "align_mult": scan["tf_alignment"].get("multiplier", 1.0),
               "regime": (pkg.get("market_regime") or {}).get("regime"), "best_tf": scan["best_timeframe"],
               "autotrader_bias": bias_dir, "scanner_signal": qualified,
               "setups": ";".join(f"{s['name']}:{s['direction']}" for s in setups)}
        i = pos15[ts]
        for d in ("long", "short"):
            res = _simulate(d, i, bars15, scan, fast)
            row[f"r_{d}"], row[f"exit_{d}"], row[f"sl_{d}"] = res["r"], res["exit"], res["sl_pct"]
            row[f"bars_{d}"] = res["bars"]                 # 15m bars held — for charging funding (stock lab)
        out.append(row)
    return out


# ── Analysis ───────────────────────────────────────────────────────────────────

def _stats(r: pd.Series, ts: pd.Series, cut) -> dict:
    """Mean R, win rate, train/test means, and a test t-stat clustered by day (trades from
    the same day and coin overlap, so each day counts once)."""
    r = r.astype(float)
    ok = r.notna()
    r, ts = r[ok], ts[ok]
    test = r[ts >= cut]
    daily = test.groupby(ts[ts >= cut].dt.floor("1D")).mean()
    t = daily.mean() / daily.std() * np.sqrt(len(daily)) if len(daily) > 2 and daily.std() > 0 else np.nan
    return {"n": len(r), "avg R": r.mean(), "win%": (r > 0).mean() * 100,
            "train R": r[ts < cut].mean(), "test R": test.mean(), "test t": t}


def _signal_r(df: pd.DataFrame, direction: pd.Series) -> pd.Series:
    return pd.Series(np.where(direction == "long", df.r_long, np.where(direction == "short", df.r_short, np.nan)),
                     index=df.index)


def report(df: pd.DataFrame, klines: dict) -> None:
    pd.set_option("display.width", 220)
    cut = df.ts.quantile(TRAIN_FRAC)
    conv_dir = pd.Series(np.where(df.conviction > 0, "long", np.where(df.conviction < 0, "short", None)), index=df.index)
    rows = [{"signal": "always long (market drift)", **_stats(df.r_long, df.ts, cut)},
            {"signal": "always short", **_stats(df.r_short, df.ts, cut)}]
    for lo, hi in ((0.1, 0.2), (0.2, 0.3), (0.3, 0.45), (0.45, 1.01)):
        m = df.conviction.abs().between(lo, hi, inclusive="left")
        rows.append({"signal": f"conviction {lo:.2f}–{hi:.2f}, its direction",
                     **_stats(_signal_r(df, conv_dir)[m], df.ts[m], cut)})
    m = df.autotrader_bias.notna()
    rows.append({"signal": "autotrader bias gate", **_stats(_signal_r(df, df.autotrader_bias)[m], df.ts[m], cut)})
    m = df.scanner_signal
    rows.append({"signal": "scanner signal gate (would ask Claude)",
                 **_stats(_signal_r(df, conv_dir)[m], df.ts[m], cut)})
    print("\nSIGNALS — R per trade (live trailing exit, costs in). Compare with the drift rows: "
          "an edge has to beat what simply being long or short made at the same moments.")
    print(pd.DataFrame(rows).round(3).to_string(index=False))

    ex = df.assign(setup=df.setups.str.split(";")).explode("setup")
    ex = ex[ex.setup.fillna("") != ""]
    rows = []
    for setup, g in ex.groupby("setup"):
        name, d = setup.split(":")
        rows.append({"setup": name, "direction": d,
                     **_stats(g[f"r_{d}"], g.ts, cut)})
    print("\nNAMED SETUPS — R of trading each setup in its own direction")
    print(pd.DataFrame(rows).sort_values("test R", ascending=False).round(3).to_string(index=False))

    # Market conditions for the autotrader's gated trades.
    btc = klines["BTCUSDT"].close.resample("1h").last()
    trend = (btc / btc.shift(24 * 7) - 1).reindex(df.ts, method="ffill").values
    vol = np.log(btc).diff().rolling(24 * 30).std().reindex(df.ts, method="ffill").values
    g = df.assign(r=_signal_r(df, df.autotrader_bias), btc_trend=trend, btc_vol=vol,
                  dirn=df.autotrader_bias)[df.autotrader_bias.notna()]
    g["BTC 7d"] = np.where(g.btc_trend > 0.03, "up >3%", np.where(g.btc_trend < -0.03, "down >3%", "flat ±3%"))
    g["BTC vol"] = pd.qcut(g.btc_vol, 3, labels=["low", "mid", "high"]) if g.btc_vol.notna().sum() > 30 else "n/a"
    g["hour (UTC)"] = (g.ts.dt.hour // 8 * 8).map(lambda h: f"{h:02d}–{h + 8:02d}")
    g["weekday"] = np.where(g.ts.dt.weekday >= 5, "weekend", "weekday")
    print("\nMARKET CONDITIONS — autotrader-gated trades, split by what the market was doing")
    for col in ("BTC 7d", "BTC vol", "hour (UTC)", "weekday"):
        rows = [{"condition": f"{col}: {k}", "direction": d, **_stats(x.r, x.ts, cut)}
                for (k, d), x in g.groupby([col, "dirn"], observed=True)]
        print(pd.DataFrame(rows).round(3).to_string(index=False))

    gate = _signal_r(df, df.autotrader_bias)[df.autotrader_bias.notna()]
    drift = df.r_long[df.autotrader_bias.notna()]
    s = _stats(gate, df.ts[df.autotrader_bias.notna()], cut)
    beats = (s["train R"] > drift[df.ts < cut].mean()) and (s["test R"] > drift[df.ts >= cut].mean())
    print("\nVERDICT:", f"autotrader gate test {s['test R']:+.3f}R/trade (t {s['test t']:.1f}); "
          + ("positive and above market drift on both halves" if beats and s["test R"] > 0 and s["test t"] >= 2
             else "not a proven edge — it doesn't beat drift on both halves with |t| ≥ 2"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--coins", type=int, default=30)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    klines = asyncio.run(_download(args.days, args.coins, args.refresh))
    path = os.path.join(CACHE_DIR, f"setup_results_{args.days}d_{args.coins}c.pkl")
    if os.path.exists(path) and not args.refresh and os.path.getmtime(path) >= max(
            os.path.getmtime(os.path.join(CACHE_DIR, f"klines15m_{args.days}d_{args.coins}c.pkl")), 0):
        df = pd.read_pickle(path)
    else:
        t0 = time.time()
        print(f"Replaying scans for {len(klines)} coins (parallel)…", flush=True)
        with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 4) - 2)) as pool:
            rows = [r for coin_rows in pool.map(run_coin, [(s, k, args.days) for s, k in klines.items()])
                    for r in coin_rows]
        df = pd.DataFrame(rows)
        df.to_pickle(path)
        print(f"…done in {(time.time() - t0) / 60:.1f} min", flush=True)
    print(f"Setup lab — {len(df):,} scans, {df.symbol.nunique()} coins, {df.ts.min():%Y-%m-%d} → "
          f"{df.ts.max():%Y-%m-%d}; train/test split at {TRAIN_FRAC:.0%} of time")
    report(df, klines)


if __name__ == "__main__":
    main()
