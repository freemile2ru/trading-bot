"""Straddle lab — backtests for the two straddle experiments.

OPTIONS STRADDLE (--options)
  An at-the-money call + put on BTC or ETH. A long straddle pays |S_T − K| at expiry and
  costs a premium; it wins when the coin moves more than the options priced in. A short
  straddle is the other side: it keeps the premium when the move is smaller.

  History: Deribit's DVOL index (30-day at-the-money implied volatility, hourly) prices the
  straddle at entry with Black-Scholes; Binance's 1h perpetual prices give the outcome.
  Entry every day at 08:00 UTC (Deribit's expiry time), tenors of 7 and 30 days, held to
  expiry or managed (long: take profit at +100% of premium; short: stop when the straddle
  costs 2× what was collected, marked with the DVOL of that hour).
  Costs: half the live-measured bid-ask spread (7d ≈3.5%, 30d ≈1.2% of the premium),
  Deribit's 0.03%-of-underlying fee per leg (capped at 12.5% of the leg), delivery fee.
  Assumption to keep in mind: a 7-day option's IV is taken as 0.93 × DVOL (the term-structure
  gap measured live; it varies).

  Signals tested: implied-vol rank (DVOL vs its own last 90 days) and the variance risk
  premium (implied vol − realized vol over the past 7 days). The textbook result is that
  options are usually overpriced (short wins on average, with rare large losses) — the
  question is whether a signal picks the times that's not true.

BREAKOUT STRADDLE (--breakout)
  On a perpetual future: when the last 12 hours' range is unusually tight for that coin
  (bottom 10% / 20% of its last 30 days, or any time as the baseline), place a buy-stop
  just above the range and a sell-stop just below it. The first to trigger is the trade,
  the other is cancelled. Stop at the far side (or middle) of the range; exit at 1× or 2×
  the range, or trail. Taker entry (it's a stop order) + slippage; pessimistic within bars.
  Uses the 15m klines cached by setup_lab (1 year, 40 perpetuals).

Both report train (first 60% of time) vs test (last 40%) and a test t-stat counted on
non-overlapping periods. Usage (from backend/):
    ./venv/bin/python -m research.straddle_lab --options
    ./venv/bin/python -m research.straddle_lab --breakout
"""

import argparse
import asyncio
import glob
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

TRAIN_FRAC = 0.6


# ── Black-Scholes (r = 0, the crypto-options convention for short tenors) ─────

def _ncdf(x):
    return 0.5 * (1 + np.vectorize(math.erf)(np.asarray(x) / math.sqrt(2)))


def straddle_price(S, K, T, iv):
    """Call + put value in USD (r = 0)."""
    S, K, T, iv = map(np.asarray, (S, K, T, iv))
    T = np.maximum(T, 1e-9)
    sd = iv * np.sqrt(T)
    d1 = (np.log(S / K) + 0.5 * sd ** 2) / sd
    d2 = d1 - sd
    call = S * _ncdf(d1) - K * _ncdf(d2)
    put = K * _ncdf(-d2) - S * _ncdf(-d1)
    return call + put


def leg_fee(S, leg_price):
    """Deribit option fee: 0.03% of the underlying, capped at 12.5% of the option price."""
    return np.minimum(0.0003 * S, 0.125 * leg_price)


# ── Options data ───────────────────────────────────────────────────────────────

DERIBIT = "https://www.deribit.com/api/v2/public"


async def _dvol(currency: str, days: int) -> pd.Series:
    from data.http import get_json

    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows = []
    while end > start:
        r = await get_json(f"{DERIBIT}/get_volatility_index_data",
                           params={"currency": currency, "start_timestamp": start, "end_timestamp": end,
                                   "resolution": "3600"}, timeout=20.0)
        data = r["result"]["data"]
        if not data:
            break
        rows += data
        nxt = r["result"].get("continuation")
        if not nxt or nxt >= end:
            break
        end = nxt
        await asyncio.sleep(0.2)
    s = pd.Series({pd.Timestamp(t, unit="ms", tz="UTC"): c for t, _, _, _, c in rows})
    return s.sort_index()[~s.sort_index().index.duplicated()] / 100


async def _options_data(days: int, refresh: bool) -> dict:
    from data.derivatives_client import fetch_klines_range

    path = os.path.join(CACHE_DIR, f"options_{days}d.pkl")
    if os.path.exists(path) and not refresh and time.time() - os.path.getmtime(path) < 86_400:
        return pd.read_pickle(path)
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = {}
    end = int(time.time() // 3600 * 3600 * 1000)
    for cur in ("BTC", "ETH"):
        print(f"Downloading {cur} DVOL and 1h prices ({days}d)…", flush=True)
        iv = await _dvol(cur, days)
        k = await fetch_klines_range(f"{cur}USDT", "1h", end - days * 86_400_000, end)
        px = pd.Series([r[4] for r in k], index=pd.to_datetime([r[0] for r in k], unit="ms", utc=True))
        out[cur] = pd.DataFrame({"S": px, "iv": iv}).sort_index().ffill().dropna()
    pd.to_pickle(out, path)
    return out


# ── Options backtest ───────────────────────────────────────────────────────────

TENORS = {7: {"iv_factor": 0.93, "half_spread": 0.035}, 30: {"iv_factor": 1.0, "half_spread": 0.012}}


def options_trades(df: pd.DataFrame, tenor: int) -> pd.DataFrame:
    cfg = TENORS[tenor]
    S, iv = df.S, df.iv
    lr = np.log(S).diff()
    rv7 = lr.rolling(24 * 7).std() * math.sqrt(24 * 365)
    rank = iv.rolling(24 * 90, min_periods=24 * 60).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)
    entries = df.index[(df.index.hour == 8) & rv7.notna().values & rank.notna().values]
    rows = []
    T0 = tenor / 365
    for t0 in entries:
        t1 = t0 + pd.Timedelta(days=tenor)
        if t1 not in df.index:
            continue
        s0, sig = S[t0], iv[t0] * cfg["iv_factor"]
        prem = float(straddle_price(s0, s0, T0, sig))
        fee_in = 2 * float(leg_fee(s0, prem / 2))
        spread = cfg["half_spread"] * prem
        fee_settle = 0.00015 * s0
        payoff = abs(S[t1] - s0)
        long_hold = payoff - prem - spread - fee_in - fee_settle
        short_hold = prem - payoff - spread - fee_in - fee_settle

        # Managed: walk hourly marks priced off that hour's DVOL.
        path = df.loc[t0:t1]
        tau = np.maximum((t1 - path.index).total_seconds().values / (365 * 86400), 1e-9)
        marks = straddle_price(path.S.values, s0, tau, path.iv.values * cfg["iv_factor"])
        long_m, short_m = long_hold, short_hold
        exit_cost = lambda m, s: cfg["half_spread"] * m + 2 * float(leg_fee(s, m / 2))
        hit = np.flatnonzero(marks[1:-1] >= 2 * prem)
        if hit.size:
            i = hit[0] + 1
            long_m = marks[i] - prem - spread - fee_in - exit_cost(marks[i], path.S.values[i])
        stop = np.flatnonzero(marks[1:-1] >= 2 * prem)
        if stop.size:
            i = stop[0] + 1
            short_m = prem - marks[i] - spread - fee_in - exit_cost(marks[i], path.S.values[i])
        rows.append({"ts": t0, "S": s0, "iv": iv[t0], "rv7": rv7[t0], "vrp": iv[t0] - rv7[t0], "iv_rank": rank[t0],
                     "premium_pct": prem / s0 * 100, "move_pct": payoff / s0 * 100,
                     "long_hold": long_hold / prem * 100, "short_hold": short_hold / prem * 100,
                     "long_managed": long_m / prem * 100, "short_managed": short_m / prem * 100,
                     "long_bps": long_hold / s0 * 1e4, "short_bps": short_hold / s0 * 1e4})
    return pd.DataFrame(rows)


def _stats(x: pd.Series, ts: pd.Series, step_days: int) -> dict:
    x = x.astype(float)
    cut = ts.quantile(TRAIN_FRAC)
    te = x[ts > cut]
    # Entries are daily but trades last `step_days`: count one per tenor for the t-stat.
    te_nov = te.iloc[::max(1, step_days)]
    t = te_nov.mean() / te_nov.std() * math.sqrt(len(te_nov)) if len(te_nov) > 2 and te_nov.std() > 0 else np.nan
    return {"n": len(x), "avg %prem": x.mean(), "win %": (x > 0).mean() * 100, "worst %prem": x.min(),
            "train": x[ts <= cut].mean(), "test": te.mean(), "test t": t}


def options_report(days: int, refresh: bool) -> None:
    data = asyncio.run(_options_data(days, refresh))
    pd.set_option("display.width", 220)
    for cur, df in data.items():
        print(f"\n══ {cur} options straddles — {df.index.min():%Y-%m-%d} → {df.index.max():%Y-%m-%d}, "
              f"DVOL now {df.iv.iloc[-1] * 100:.1f}%")
        for tenor in TENORS:
            tr = options_trades(df, tenor)
            if tr.empty:
                continue
            print(f"\n{tenor}-day ATM straddle, entered daily at 08:00 UTC ({len(tr)} entries). "
                  f"Avg premium {tr.premium_pct.mean():.2f}% of price vs avg move {tr.move_pct.mean():.2f}%. "
                  "P&L in % of premium, costs in:")
            rows = []
            for col, label in (("long_hold", "long, hold to expiry"), ("long_managed", "long, take profit at +100%"),
                               ("short_hold", "short, hold to expiry"), ("short_managed", "short, stop at −100%")):
                rows.append({"strategy": label, **_stats(tr[col], tr.ts, tenor)})
            q = pd.qcut(tr.iv_rank, [0, 0.2, 0.8, 1], labels=["IV rank <20%", "20–80%", ">80%"])
            for lbl, g in tr.groupby(q, observed=True):
                rows.append({"strategy": f"long when {lbl}", **_stats(g.long_hold, g.ts, tenor)})
                rows.append({"strategy": f"short when {lbl}", **_stats(g.short_managed, g.ts, tenor)})
            for lbl, g in ((f"IV < RV7 (options cheap)", tr[tr.vrp < 0]),
                           (f"IV − RV7 > 10 pts (options rich)", tr[tr.vrp > 0.10])):
                if len(g) > 10:
                    rows.append({"strategy": f"long when {lbl}", **_stats(g.long_hold, g.ts, tenor)})
                    rows.append({"strategy": f"short when {lbl}", **_stats(g.short_managed, g.ts, tenor)})
            print(pd.DataFrame(rows).round(2).to_string(index=False))


# ── Breakout straddle ──────────────────────────────────────────────────────────

RANGE_H = 12
ORDER_TTL_H = 24
MAX_HOLD_H = 48
BUFFER = 0.10                 # × range, beyond the range edge
TAKER, MAKER, SLIP = 0.0005, 0.0002, 0.0003
COMPRESSION = (0.10, 0.20, 1.0)                     # range percentile threshold; 1.0 = any time (baseline)
STOPS = ("far", "mid")
EXITS = ("tp1", "tp2", "trail")


def breakout_coin(args) -> list[dict]:
    sym, k15 = args
    h = k15.high.resample("1h").max()
    lo = k15.low.resample("1h").min()
    c = k15.close.resample("1h").last()
    hi_r, lo_r = h.rolling(RANGE_H).max(), lo.rolling(RANGE_H).min()
    width = (hi_r - lo_r) / c
    pct = width.rolling(24 * 30, min_periods=24 * 20).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)
    o15, h15, l15, c15 = k15.open.values, k15.high.values, k15.low.values, k15.close.values
    t15 = k15.index
    pos15 = pd.Series(np.arange(len(t15)), index=t15)
    out = []
    for q in COMPRESSION:
        for stop_mode in STOPS:
            for exit_mode in EXITS:
                busy_until = None
                for t in width.index:
                    if busy_until is not None and t < busy_until:
                        continue
                    p, w = pct.get(t), width.get(t)
                    if p is None or np.isnan(p) or np.isnan(w) or w < 0.003 or p > q:
                        continue
                    start = t + pd.Timedelta(hours=1)                    # the hour just closed
                    if start not in pos15.index:
                        continue
                    hi, low = hi_r[t], lo_r[t]
                    rng = hi - low
                    buy, sell = hi + BUFFER * rng, low - BUFFER * rng
                    i0 = pos15[start]
                    i_end = min(len(t15), i0 + ORDER_TTL_H * 4)
                    fill_i, d = None, 0
                    for i in range(i0, i_end):
                        up, dn = h15[i] >= buy, l15[i] <= sell
                        if up and dn:
                            fill_i, d = i, 0      # both in one bar: whipsaw, counted as a full loss
                            break
                        if up or dn:
                            fill_i, d = i, 1 if up else -1
                            break
                    if fill_i is None:
                        busy_until = t15[i_end - 1] if i_end > i0 else start
                        continue
                    if d == 0:
                        out.append({"sym": sym, "ts": t15[fill_i], "q": q, "stop": stop_mode, "exit": exit_mode,
                                    "r": -1.0 - 2 * (TAKER + SLIP) * c15[fill_i] / max(rng, 1e-12), "whipsaw": True})
                        busy_until = t15[fill_i]
                        continue
                    trig = buy if d > 0 else sell
                    entry = max(trig, o15[fill_i]) if d > 0 else min(trig, o15[fill_i])   # gaps fill worse
                    entry *= 1 + d * SLIP
                    stop = (low if d > 0 else hi) if stop_mode == "far" else (hi + low) / 2
                    risk = (entry - stop) * d
                    if risk <= 0:
                        continue
                    target = entry + d * (1 if exit_mode == "tp1" else 2) * rng if exit_mode != "trail" else None
                    best, trail_stop = entry, stop
                    exit_px, maker = None, False
                    last = min(len(t15), fill_i + MAX_HOLD_H * 4)
                    for i in range(fill_i, last):
                        cur_stop = trail_stop
                        if (l15[i] <= cur_stop) if d > 0 else (h15[i] >= cur_stop):
                            exit_px = (min(o15[i], cur_stop) if d > 0 else max(o15[i], cur_stop)) * (1 - d * SLIP)
                            break
                        if i == fill_i:
                            continue                        # entry bar: only the stop can trigger
                        if target is not None and ((h15[i] >= target) if d > 0 else (l15[i] <= target)):
                            exit_px, maker = target, True
                            break
                        best = max(best, h15[i]) if d > 0 else min(best, l15[i])
                        if exit_mode == "trail" and (best - entry) * d >= risk:
                            trail_stop = max(trail_stop, best - risk) if d > 0 else min(trail_stop, best + risk)
                    else:
                        exit_px = c15[last - 1]
                    fees = entry * TAKER + exit_px * (MAKER if maker else TAKER)
                    out.append({"sym": sym, "ts": t15[fill_i], "q": q, "stop": stop_mode, "exit": exit_mode,
                                "r": ((exit_px - entry) * d - fees) / risk, "whipsaw": False})
                    busy_until = t15[min(i, len(t15) - 1)]
    return out


def breakout_report() -> None:
    files = sorted(glob.glob(os.path.join(CACHE_DIR, "klines15m_*d_*c.pkl")), key=os.path.getsize)
    if not files:
        print("No 15m klines cached — run `python -m research.setup_lab` once to download them.")
        return
    klines = pd.read_pickle(files[-1])
    print(f"Breakout straddle — {len(klines)} perpetuals from {os.path.basename(files[-1])}; "
          f"range {RANGE_H}h, orders live {ORDER_TTL_H}h, max hold {MAX_HOLD_H}h, costs in (R per trade)", flush=True)
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 4) - 2)) as pool:
        rows = [r for res in pool.map(breakout_coin, list(klines.items())) for r in res]
    df = pd.DataFrame(rows)
    cut = df.ts.quantile(TRAIN_FRAC)
    out = []
    for (q, stop, ex), g in df.groupby(["q", "stop", "exit"]):
        te = g[g.ts > cut]
        daily = te.groupby(te.ts.dt.floor("1D")).r.mean()
        t = daily.mean() / daily.std() * math.sqrt(len(daily)) if len(daily) > 2 and daily.std() > 0 else np.nan
        wins, losses = g.r[g.r > 0].sum(), -g.r[g.r <= 0].sum()
        out.append({"compression": "any time" if q >= 1 else f"range ≤ p{int(q * 100)}", "stop": stop, "exit": ex,
                    "trades": len(g), "whipsaw %": g.whipsaw.mean() * 100, "avg R": g.r.mean(), "win %": (g.r > 0).mean() * 100,
                    "profit factor": wins / losses if losses else np.nan,
                    "train R": g.r[g.ts <= cut].mean(), "test R": te.r.mean(), "test t": t})
    pd.set_option("display.width", 220)
    print(pd.DataFrame(out).round(3).to_string(index=False))
    best = [r for r in out if r["train R"] > 0 and r["test R"] > 0 and r["test t"] >= 2]
    print("\nVERDICT:", ("holds on both halves: " + "; ".join(
        f"{r['compression']} / {r['stop']} stop / {r['exit']} → test {r['test R']:+.3f}R (t {r['test t']:.1f})" for r in best))
          if best else "no configuration is positive on both halves with t ≥ 2.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--options", action="store_true")
    ap.add_argument("--breakout", action="store_true")
    ap.add_argument("--days", type=int, default=730, help="options: days of history")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    if args.options or not args.breakout:
        options_report(args.days, args.refresh)
    if args.breakout or not args.options:
        breakout_report()


if __name__ == "__main__":
    main()
