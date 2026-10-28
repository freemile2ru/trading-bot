"""Stock setup lab — would OUR scanner and autotrader rules work on Binance's stock perpetuals?

The equity lab (research/equity_lab.py) tested textbook stock strategies; this tests our own system.
It is the crypto setup lab (research/setup_lab.py) pointed at the plain stocks and broad ETFs Binance
lists as USDⓈ-M perpetuals — US and Asian — on exactly the data the live system would read: each
contract's own 15-minute candles since it listed (Jan–Jul 2026).

Every 4 h, for each stock, the scan is rebuilt through the production TA, named setups and
conviction, and a trade is simulated each way with the autotrader's levels and the live exit policy
(setup_lab._simulate). On top of setup_lab's taker fees and slippage, each trade is charged its
contract's measured average funding for the time it was held (longs pay, shorts receive).

What it reports, per region and for US index ETFs vs single stocks:
  * conviction buckets, the autotrader's bias gate and the scanner's signal gate (what would be sent
    to Claude), each next to ALWAYS-LONG and ALWAYS-SHORT at the same moments — an edge has to beat
    the drift, not just be positive in a rising market
  * whether the scan happened while the stock's exchange was open or shut (perps trade 24/7, but
    off-hours their prices only follow the overnight futures and news)
  * train (first 60%) vs test (last 40%), t-stat clustered by day
Pre-registered verdict: an edge needs the gate's test R > 0 with t ≥ 2 AND above always-long's test
R AND above it on the train half too. What it leaves out, as the crypto lab does: Claude's
confirmation, the 15m timing trigger, news and fundamentals.

    ./venv/bin/python -m research.stock_setup_lab            # downloads once, cached for a day
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor

import httpx
import numpy as np
import pandas as pd

from research.setup_lab import TRAIN_FRAC, WARMUP_DAYS, _signal_r, _stats, run_coin
from research.swing_lab import CACHE_DIR

KLINES = os.path.join(CACHE_DIR, "stock_klines15m.pkl")
RESULTS = os.path.join(CACHE_DIR, "stock_setup_results.pkl")
SUMMARY = os.path.join(CACHE_DIR, "stock_setup_summary.json")
INDEX_ETFS = {"SPYUSDT", "QQQUSDT", "IWMUSDT", "EWYUSDT", "EWZUSDT", "EWJUSDT", "KODEX200USDT"}
# Regular sessions in UTC (DST ignored — at most an hour off at the edges, for the open/shut split).
SESSIONS = {"us": (13.5, 20.0), "hk": (1.5, 8.0), "kr": (0.0, 6.5)}
ASIA_HK = {"HK0700USDT", "HK1810USDT", "MEITUANUSDT", "KUAISHOUUSDT", "BYDUSDT", "POPMARTUSDT", "HK0992USDT"}
ASIA_KR = {"SKHYNIXUSDT", "SAMSUNGUSDT", "HYUNDAIUSDT", "NAVERUSDT", "LGELECTRONICSUSDT", "SAMSUNGEMUSDT",
           "HANMIUSDT", "KODEX200USDT"}


def universe() -> list[str]:
    """Binance's plain stock/ETF perpetuals (leveraged and inverse products excluded)."""
    from research.equity_lab import ASIA, US
    info = httpx.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=30).json()["symbols"]
    live = {s["symbol"] for s in info if s.get("status") == "TRADING"}
    wanted = [f"{t}USDT" for t in dict.fromkeys(US)] + list(ASIA)
    return [s for s in wanted if s in live]


def region(sym: str) -> str:
    return "kr" if sym in ASIA_KR else "hk" if sym in ASIA_HK else "us"


async def _download(refresh: bool) -> dict[str, pd.DataFrame]:
    from data.derivatives_client import fetch_klines_range
    if os.path.exists(KLINES) and not refresh and time.time() - os.path.getmtime(KLINES) < 86_400:
        return pd.read_pickle(KLINES)
    syms = universe()
    end = int(time.time() // 900 * 900 * 1000)
    print(f"Downloading 15m klines since listing for {len(syms)} stock perpetuals…", flush=True)
    sem = asyncio.Semaphore(4)

    async def one(sym):
        async with sem:
            return sym, await fetch_klines_range(sym, "15m", end - 400 * 86_400_000, end)

    out = {}
    for sym, rows in await asyncio.gather(*(one(s) for s in syms)):
        if len(rows) < (WARMUP_DAYS + 30) * 96:              # needs warm-up plus a month of scans
            continue
        df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume", "qv"]).drop_duplicates("t")
        out[sym] = df.set_index(pd.to_datetime(df.t, unit="ms", utc=True))[["open", "high", "low", "close", "volume"]]
    pd.to_pickle(out, KLINES)
    return out


def _funding_per_bar(syms: list[str]) -> dict[str, float]:
    """Average funding per 15-minute bar held (longs pay +)."""
    out = {}
    for s in syms:
        rows = httpx.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": s, "limit": 1000}, timeout=20).json()
        if isinstance(rows, list) and len(rows) > 10:
            span_bars = (rows[-1]["fundingTime"] - rows[0]["fundingTime"]) / 900_000
            out[s] = sum(float(r["fundingRate"]) for r in rows[1:]) / span_bars
        else:
            out[s] = 0.0
    return out


def _replay(klines: dict, refresh: bool) -> pd.DataFrame:
    if os.path.exists(RESULTS) and not refresh and os.path.getmtime(RESULTS) >= os.path.getmtime(KLINES):
        return pd.read_pickle(RESULTS)
    args = [(s, k, int((k.index.max() - k.index.min()).days) - WARMUP_DAYS) for s, k in klines.items()]
    print(f"Replaying scans for {len(args)} stocks (parallel)…", flush=True)
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 4) - 2)) as pool:
        rows = [r for coin in pool.map(run_coin, args) for r in coin]
    df = pd.DataFrame(rows)
    fund = _funding_per_bar(list(klines))
    for d, s in (("long", 1), ("short", -1)):
        # Funding over the hold, in R: rate per bar × bars held, over the stop distance (sl %).
        f = df.symbol.map(fund).fillna(0.0) * df[f"bars_{d}"] / (df[f"sl_{d}"] / 100)
        df[f"r_{d}"] = df[f"r_{d}"] - s * f
    df["market_open"] = [SESSIONS[region(s)][0] <= (t.hour + t.minute / 60) < SESSIONS[region(s)][1] and t.weekday() < 5
                         for s, t in zip(df.symbol, df.ts)]
    df["group"] = np.where(df.symbol.isin(INDEX_ETFS), "index ETFs", np.where(df.symbol.map(region) == "us", "US stocks", "Asian stocks"))
    pd.to_pickle(df, RESULTS)
    return df


def _row(label: str, r: pd.Series, ts: pd.Series, cut) -> dict:
    s = _stats(r, ts, cut)
    return {"signal": label, **{k: (round(float(v), 3) if isinstance(v, (float, np.floating)) and not np.isnan(v) else
                                    (None if isinstance(v, (float, np.floating)) else v)) for k, v in s.items()}}


def analyse(df: pd.DataFrame) -> dict:
    cut = df.ts.quantile(TRAIN_FRAC)
    conv_dir = pd.Series(np.where(df.conviction > 0, "long", np.where(df.conviction < 0, "short", None)), index=df.index)
    out = {"scans": len(df), "stocks": int(df.symbol.nunique()), "period": [str(df.ts.min().date()), str(df.ts.max().date())],
           "split": str(cut.date()), "groups": {}}
    for name, g in [("all", df)] + list(df.groupby("group")) + [("exchange open", df[df.market_open]),
                                                                  ("exchange shut", df[~df.market_open])]:
        cd = conv_dir.loc[g.index]
        rows = [_row("always long (drift)", g.r_long, g.ts, cut), _row("always short", g.r_short, g.ts, cut)]
        for lo, hi in ((0.2, 0.35), (0.35, 0.45), (0.45, 1.01)):
            m = g.conviction.abs().between(lo, hi, inclusive="left")
            rows.append(_row(f"conviction {lo:.2f}–{hi:.2f}, its direction", _signal_r(g, cd)[m], g.ts[m], cut))
        m = g.autotrader_bias.notna()
        gate = _row("autotrader bias gate", _signal_r(g, g.autotrader_bias)[m], g.ts[m], cut)
        rows.append(gate)
        m = g.scanner_signal.astype(bool)
        rows.append(_row("scanner signal gate (would ask Claude)", _signal_r(g, cd)[m], g.ts[m], cut))
        drift = rows[0]
        edge = (gate["test R"] or -9) > 0 and (gate["test t"] or 0) >= 2 and \
            (gate["test R"] or -9) > (drift["test R"] or 0) and (gate["train R"] or -9) > (drift["train R"] or 0)
        out["groups"][name] = {"rows": rows, "edge": bool(edge), "scans": len(g)}
    ex = df.assign(setup=df.setups.str.split(";")).explode("setup")
    ex = ex[ex.setup.fillna("") != ""]
    setups = []
    for setup, g in ex.groupby("setup"):
        name, d = setup.split(":")
        setups.append({"setup": name, "direction": d, **_row("", g[f"r_{d}"], g.ts, cut)})
    out["setups"] = sorted(setups, key=lambda x: -(x["test R"] if x["test R"] is not None else -9))
    out["verdict"] = "edge" if out["groups"]["all"]["edge"] else "no edge"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    klines = asyncio.run(_download(a.refresh))
    df = _replay(klines, a.refresh)
    res = analyse(df)
    json.dump({**res, "generated_at": time.time()}, open(SUMMARY, "w"), indent=1, default=str)
    pd.set_option("display.width", 220)
    print(f"\n{res['scans']} scans of {res['stocks']} stocks, {res['period'][0]} → {res['period'][1]}, test from {res['split']}")
    for name, g in res["groups"].items():
        print(f"\n■ {name} ({g['scans']} scans) — {'EDGE' if g['edge'] else 'no edge'}")
        print(pd.DataFrame(g["rows"]).to_string(index=False))
    print("\nNAMED SETUPS (in their own direction), best test R first")
    print(pd.DataFrame(res["setups"]).drop(columns=["signal"]).to_string(index=False))
    print(f"\nVERDICT: {res['verdict']}")


if __name__ == "__main__":
    main()
