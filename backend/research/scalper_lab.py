"""Scalper research lab — does the signal have an edge, and could any exit rescue it?

Runs entirely off the scalper's own databases (no live engine needed):

  1. Signal research   — forward mid-price move after every logged signal (taken or not),
                         overall and by flow strength, book support and side.
  2. Feature scan      — for each recorded feature, the +120s move in its best vs worst
                         quintile. A feature is only interesting if that gap beats costs.
  3. Stop lab          — replays non-overlapping signals over the recorded 1-second bars
                         through a grid of stops and targets, with realistic costs; picks
                         the best on the first 60% of the data and reports it on the last
                         40% (out of sample).

  4. Impulse study   (--impulse) — the "only scalp very strong candles" idea: find candles
                         far outside normal (relative to each coin's own volatility, and in
                         absolute size), then measure follow-through and replay chasing / fading
                         them with brackets and a trailing stop, all costs in, train/test split.

  5. Lead-lag        (--leadlag) — when BTC jumps, do the other coins follow a few seconds
                         later, and is the catch-up big enough to trade?
  6. Sessions        (--sessions) — volatility, spreads, signal quality and results by UTC
                         hour, and around the 8-hourly funding settlements.
  7. Fill realism    (--fills) — the queue model decides when our resting orders fill. Fills
                         where price traded *through* our level would have happened for real;
                         fills from queue position alone are the optimistic ones. Compares
                         the two, and filled vs cancelled entries (adverse selection).
  8. Liquidations    (--liquidations) — bursts of forced liquidations (recorded from Binance's
                         all-market stream): do they continue or snap back?

Usage (from backend/):
    ./venv/bin/python -m research.scalper_lab            # everything
    ./venv/bin/python -m research.scalper_lab --hours 48 # recent window only
    ./venv/bin/python -m research.scalper_lab --impulse  # one study only (also --leadlag,
                                                         # --sessions, --fills, --liquidations)

The verdict to look for: a signal needs an average forward move comfortably above the
round-trip cost (~5 bps with maker entry, taker stop) — and the stop lab needs combos
that are positive on BOTH halves. Anything else is noise.
"""

import argparse
import itertools
import sqlite3
import time

import numpy as np
import pandas as pd

from scalper import config as C
from scalper import costs

HORIZONS = (5, 15, 30, 60, 120)
LAB_HORIZON = 600                   # seconds a replayed trade may run (the live time stop)
SL_GRID = (15, 25, 35, 50, 75)
TP_GRID = (20, 30, 40, 60, 80, 120)
DEDUPE_S = 120                      # one signal per coin per window, so trades don't overlap


def _load_signals(since: float) -> pd.DataFrame:
    s = pd.read_sql("SELECT * FROM signals WHERE ts >= ? ORDER BY ts", sqlite3.connect(C.TRADES_DB), params=(since,))
    s["sgn"] = np.where(s.side == "long", 1, -1)
    return s


def _fwd(g: pd.DataFrame) -> pd.Series:
    out = {"n": len(g)}
    for h in HORIZONS:
        v = g[f"fwd_{h}"].dropna()
        out[f"+{h}s bps"] = v.mean()
        out[f"+{h}s hit%"] = (v > 0).mean() * 100
    return pd.Series(out)


def signal_research(s: pd.DataFrame) -> dict:
    cols = ["n", "+5s bps", "+30s bps", "+120s bps", "+120s hit%"]
    z = pd.cut(s.flow_z.abs(), [1.5, 2, 2.5, 3.5, 99], labels=["1.5–2", "2–2.5", "2.5–3.5", "3.5+"])
    book = pd.cut(s.obi * s.sgn, [-1.01, 0, 0.2, 0.5, 1.01], labels=["against", "0–0.2", "0.2–0.5", "0.5+"])
    return {
        "all": _fwd(s).round(2),
        "by_flow_z": s.groupby(z, observed=True).apply(_fwd, include_groups=False).round(2)[cols],
        "by_book": s.groupby(book, observed=True).apply(_fwd, include_groups=False).round(2)[cols],
        "by_side": s.groupby("side").apply(_fwd, include_groups=False).round(2)[cols],
    }


def feature_scan(s: pd.DataFrame) -> pd.DataFrame:
    feats = {
        "flow_z": s.flow_z.abs(), "book (in dir)": s.obi * s.sgn, "micro_skew (in dir)": s.micro_skew_bps * s.sgn,
        "vwap_dev (in dir)": s.vwap_dev_atr * s.sgn, "rv_60s": s.rv_60s_bps, "trades/s": s.trades_per_s,
        "spread": s.spread_bps, "atr": s.atr_bps, "flow_10s (in dir)": s.flow_10s * s.sgn,
    }
    rows = []
    for name, x in feats.items():
        q = pd.qcut(x.rank(method="first"), 5, labels=False)
        by = s.groupby(q).fwd_120.mean()
        rows.append({"feature": name, "Q1 bps": by.iloc[0], "Q5 bps": by.iloc[-1],
                     "Q5−Q1": by.iloc[-1] - by.iloc[0], "corr": x.corr(s.fwd_120)})
    return pd.DataFrame(rows).round(3).sort_values("Q5 bps", ascending=False)


def _paths(since: float) -> dict[str, pd.DataFrame]:
    bars = pd.read_sql("SELECT symbol, ts, h, l, c FROM bars_1s WHERE ts >= ?", sqlite3.connect(C.MARKET_DB),
                       params=(int(since),))
    return {sym: g.set_index("ts")[["h", "l", "c"]] for sym, g in bars.groupby("symbol")}


def _replay(row, paths, sl: float, tp: float) -> float | None:
    """Net bps for one signal: maker entry at the next second's mid, stop first on ties."""
    p = paths.get(row.symbol)
    if p is None:
        return None
    t0 = int(row.ts) + 1
    seg = p.loc[t0: t0 + LAB_HORIZON]
    if len(seg) < LAB_HORIZON * 0.8:
        return None
    maker, taker = costs.MAKER_FEE * 1e4, costs.TAKER_FEE * 1e4
    entry, s = seg.c.iloc[0], row.sgn
    fav = ((seg.h if s > 0 else seg.l) - entry) / entry * 1e4 * s
    adv = ((seg.l if s > 0 else seg.h) - entry) / entry * 1e4 * s
    stop_at = np.flatnonzero(adv.values <= -sl)
    tp_at = np.flatnonzero(fav.values >= tp)
    if stop_at.size and (not tp_at.size or stop_at[0] <= tp_at[0]):
        return -sl - (maker + taker + costs.LATENCY_BPS)
    if tp_at.size:
        return tp - 2 * maker
    end = (seg.c.iloc[-1] - entry) / entry * 1e4 * s
    return end - (maker + taker + costs.LATENCY_BPS)


def stop_lab(s: pd.DataFrame, since: float) -> tuple[pd.DataFrame, int]:
    keep, last = [], {}
    for r in s.itertuples():
        if r.ts - last.get(r.symbol, -1e18) >= DEDUPE_S:
            keep.append(r)
            last[r.symbol] = r.ts
    sig = pd.DataFrame(keep)
    paths = _paths(since)
    split = sig.ts.quantile(0.6)
    rows = []
    for sl, tp in itertools.product(SL_GRID, TP_GRID):
        r = sig.apply(lambda row: _replay(row, paths, sl, tp), axis=1).dropna()
        is_train = sig.loc[r.index, "ts"] <= split
        tr, te = r[is_train], r[~is_train]
        rows.append({"sl": sl, "tp": tp, "n_train": len(tr), "train_bps": tr.mean(), "train_win%": (tr > 0).mean() * 100,
                     "n_test": len(te), "test_bps": te.mean(), "test_win%": (te > 0).mean() * 100})
    return pd.DataFrame(rows).round(2).sort_values("train_bps", ascending=False), len(sig)


# ── 4. Impulse study ───────────────────────────────────────────────────────────

IMPULSE_DEDUPE_S = 300               # one event per coin per 5 min — a burst is one event
IMPULSE_HORIZON = 600
IMPULSE_REL = [(5, 5), (5, 8), (15, 5), (60, 4), (60, 6)]          # (window s, move ≥ z × normal)
IMPULSE_ABS = [(60, 30), (60, 50), (60, 80), (300, 100), (300, 150)]   # (window s, move ≥ bps)
IMPULSE_BRACKETS = [(20, 30), (30, 60), (40, 80), (50, 150), (80, 240)]
IMPULSE_TRAILS = [(30, 30), (50, 50), (80, 80)]    # (initial stop, trail distance) — let runaways run
MIN_TEST_EVENTS = 30


def _impulse_paths(since: float) -> dict[str, pd.DataFrame]:
    """Gap-filled 1s paths with high/low, close and traded notional."""
    bars = pd.read_sql("SELECT symbol, ts, h, l, c, buy_vol, sell_vol FROM bars_1s WHERE ts >= ?",
                       sqlite3.connect(C.MARKET_DB), params=(int(since),))
    out = {}
    for sym, g in bars.groupby("symbol"):
        g = g.set_index("ts").sort_index()
        g = g.reindex(np.arange(g.index.min(), g.index.max() + 1))
        g["c"] = g.c.ffill()
        g["h"], g["l"] = g.h.fillna(g.c), g.l.fillna(g.c)
        g["notional"] = (g.buy_vol.fillna(0) + g.sell_vol.fillna(0)) * g.c
        out[sym] = g[["h", "l", "c", "notional"]]
    return out


def _dedupe(e: pd.DataFrame) -> pd.DataFrame:
    keep, last = [], -1e18
    for r in e.itertuples():
        if r.ts - last >= IMPULSE_DEDUPE_S:
            keep.append(r.Index)
            last = r.ts
    return e.loc[keep]


def impulse_events(paths: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Every strong candle, by each definition. `move` is signed bps over the window."""
    rows = []
    for sym, g in paths.items():
        r1 = np.log(g.c).diff() * 1e4
        sd1 = r1.rolling(3600, min_periods=1800).std().shift(1)          # the past hour's 1s volatility
        base = g.notional.rolling(3600, min_periods=1800).mean().shift(1)
        for W in sorted({w for w, _ in IMPULSE_REL + IMPULSE_ABS}):
            mv = (g.c / g.c.shift(W) - 1) * 1e4
            e = pd.DataFrame({"symbol": sym, "ts": g.index, "move": mv, "z": mv / (sd1 * np.sqrt(W)),
                              "vol_x": g.notional.rolling(W).sum() / (base * W)}).dropna()
            for w, z in IMPULSE_REL:
                if w == W:
                    hit = _dedupe(e[(e.z.abs() >= z) & (e.vol_x >= 3)])
                    rows.append(hit.assign(kind=f"{W}s ≥{z}σ, vol ≥3×"))
            for w, b in IMPULSE_ABS:
                if w == W:
                    rows.append(_dedupe(e[e.move.abs() >= b]).assign(kind=f"{W}s ≥{b} bps"))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _follow(path: pd.DataFrame, ts: int, sign: float, h: int) -> float:
    a, b = path.c.get(ts + 1), path.c.get(ts + 1 + h)
    return (b / a - 1) * 1e4 * sign if a and b else np.nan


def _replay_impulse(path: pd.DataFrame, ts: int, sign: float, sl: float, tp: float | None,
                    trail: float | None) -> float:
    """Net bps for one trade: taker entry the next second; maker target or taker stop;
    stop checked first within a second (pessimistic). With `trail`, the stop follows the
    best price at `trail` bps once it has moved that far, and there is no target."""
    p = path.loc[ts + 1: ts + 1 + IMPULSE_HORIZON]
    if len(p) < IMPULSE_HORIZON * 0.8:
        return np.nan
    taker, maker, lat = costs.TAKER_FEE * 1e4, costs.MAKER_FEE * 1e4, costs.LATENCY_BPS
    entry = p.c.iloc[0] * (1 + sign * lat / 1e4)
    fav = ((p.h if sign > 0 else p.l) / entry - 1).values * 1e4 * sign
    adv = ((p.l if sign > 0 else p.h) / entry - 1).values * 1e4 * sign
    stop, best = -sl, 0.0
    for f, a in zip(fav, adv):
        if a <= stop:
            return stop - 2 * taker - lat
        if tp is not None and f >= tp:
            return tp - taker - maker
        if trail is not None:
            best = max(best, f)
            if best >= trail:
                stop = max(stop, best - trail)
    end = (p.c.iloc[-1] / entry - 1) * 1e4 * sign
    return end - 2 * taker - lat


def impulse_study(since: float) -> None:
    paths = _impulse_paths(since)
    if not paths:
        print("No 1s bars recorded in that window.")
        return
    ev = impulse_events(paths)
    hours = sum(len(p) for p in paths.values()) / len(paths) / 3600
    chase = 2 * costs.TAKER_FEE * 1e4 + costs.LATENCY_BPS
    print(f"4. IMPULSE STUDY — strong candles across {len(paths)} coins, ~{hours:.0f}h of 1s bars. "
          f"Chasing costs ≈ {chase:.1f} bps (taker in and out).\n")
    if ev.empty:
        print("No strong candles found.")
        return

    rows = []
    for kind, s in ev.groupby("kind", sort=False):
        sign = np.sign(s.move.values)
        f = {h: np.array([_follow(paths[r.symbol], r.ts, sg, h) for r, sg in zip(s.itertuples(), sign)])
             for h in (30, 120, 600)}
        rows.append({"candle": kind, "events": len(s), "per day": len(s) / hours * 24,
                     "median move": s.move.abs().median(), "+30s": np.nanmean(f[30]),
                     "+120s": np.nanmean(f[120]), "+600s": np.nanmean(f[600]),
                     "+600s median": np.nanmedian(f[600]), "still going %": np.nanmean(f[600] > 0) * 100})
    pd.set_option("display.width", 220)
    print("Follow-through after the candle (bps in the candle's direction, before costs; "
          "a mean far above the median = a few runaways carry it):")
    print(pd.DataFrame(rows).round(1).to_string(index=False))

    print(f"\nReplays, all costs in — tuned on the first 60% of time, checked on the last 40%. "
          f"A result needs ≥{MIN_TEST_EVENTS} test events to mean anything.")
    passing = []
    for kind, s in ev.groupby("kind", sort=False):
        cut = s.ts.quantile(0.6)
        is_train = (s.ts <= cut).values
        out = []
        configs = [("chase", sl, tp, None) for sl, tp in IMPULSE_BRACKETS] + \
                  [("fade", sl, tp, None) for sl, tp in IMPULSE_BRACKETS] + \
                  [("chase+trail", sl, None, tr) for sl, tr in IMPULSE_TRAILS]
        for mode, sl, tp, tr in configs:
            direction = -1 if mode == "fade" else 1
            res = np.array([_replay_impulse(paths[r.symbol], r.ts, np.sign(r.move) * direction, sl, tp, tr)
                            for r in s.itertuples()])
            tr_, te = res[is_train], res[~is_train]
            n_te = int(np.isfinite(te).sum())
            row = {"mode": mode, "SL": sl, "TP / trail": tp or f"trail {tr}", "train bps": np.nanmean(tr_),
                   "test bps": np.nanmean(te), "test win%": np.nanmean(te[np.isfinite(te)] > 0) * 100,
                   "test n": n_te}
            out.append(row)
            if row["train bps"] > 0 and row["test bps"] > 0 and n_te >= MIN_TEST_EVENTS:
                passing.append(f"{kind}: {mode} SL {sl} / {row['TP / trail']} → "
                               f"test {row['test bps']:+.1f} bps over {n_te} events")
        best = pd.DataFrame(out).sort_values("train bps", ascending=False).head(5)
        print(f"\n{kind} — {len(s)} events (best 5 on train)")
        print(best.round(1).to_string(index=False))

    print("\nIMPULSE VERDICT:", "positive on both halves with enough events → worth a live paper test:\n  "
          + "\n  ".join(passing) if passing else
          f"nothing positive on both halves with ≥{MIN_TEST_EVENTS} test events — keep recording.")


# ── Shared: 1s closes for every coin on one clock ─────────────────────────────

def _close_panel(since: float) -> pd.DataFrame:
    bars = pd.read_sql("SELECT symbol, ts, c FROM bars_1s WHERE ts >= ?", sqlite3.connect(C.MARKET_DB),
                       params=(int(since),))
    if bars.empty:
        return pd.DataFrame()
    wide = bars.pivot_table(index="ts", columns="symbol", values="c")
    return wide.reindex(np.arange(wide.index.min(), wide.index.max() + 1)).ffill()


def _split_stats(x: pd.Series, ts: pd.Series) -> dict:
    x = x.astype(float)
    cut = ts.quantile(0.6)
    tr, te = x[ts <= cut], x[ts > cut]
    return {"n": int(x.notna().sum()), "mean bps": x.mean(), "hit %": (x > 0).mean() * 100,
            "train bps": tr.mean(), "test bps": te.mean()}


# ── 5. Lead-lag ────────────────────────────────────────────────────────────────

LEADLAG_WINDOWS = (3, 5, 10)
LEADLAG_Z = 4.0


def leadlag_study(since: float) -> None:
    px = _close_panel(since)
    if px.empty or "BTCUSDT" not in px:
        print("5. LEAD-LAG — no BTC bars recorded.")
        return
    lp = np.log(px)
    alts = [c for c in px.columns if c != "BTCUSDT"]
    chase = 2 * costs.TAKER_FEE * 1e4 + costs.LATENCY_BPS
    print(f"5. LEAD-LAG — does BTC lead the other {len(alts)} coins? ~{len(px) / 3600:.0f}h of 1s bars. "
          f"Entry one second after the BTC move; chasing costs ≈ {chase:.1f} bps.\n")

    # Continuous: correlation of BTC's last-5s return with each alt's next-5s return.
    btc5 = (lp.BTCUSDT - lp.BTCUSDT.shift(5)) * 1e4
    ics = {a: btc5.corr((lp[a].shift(-6) - lp[a].shift(-1)) * 1e4) for a in alts}
    same = {a: btc5.corr((lp[a] - lp[a].shift(5)) * 1e4) for a in alts}
    print("   corr(BTC last 5s, alt NEXT 5s) — the lead — vs corr(BTC last 5s, alt SAME 5s):")
    print("   " + "  ".join(f"{a.removesuffix('USDT')} {ics[a]:+.3f}/{same[a]:+.2f}" for a in alts))

    rows = []
    for k in LEADLAG_WINDOWS:
        b = (lp.BTCUSDT - lp.BTCUSDT.shift(k)) * 1e4
        z = b / (lp.BTCUSDT.diff().rolling(3600, min_periods=1800).std().shift(1) * 1e4 * np.sqrt(k))
        hits = z[z.abs() >= LEADLAG_Z].index
        keep, last = [], -1e18
        for t in hits:
            if t - last >= 60:
                keep.append(t)
                last = t
        ev = []
        for t in keep:
            sgn = np.sign(b.at[t])
            for a in alts:
                same_move = (lp.at[t, a] - lp.at[t - k, a]) * 1e4 * sgn if t - k in lp.index else np.nan
                r = {"ts": t, "alt": a, "caught_up": same_move / abs(b.at[t]) if b.at[t] else np.nan}
                for h in (5, 15, 30, 60):
                    if t + 1 + h in lp.index:
                        r[f"f{h}"] = (lp.at[t + 1 + h, a] - lp.at[t + 1, a]) * 1e4 * sgn
                ev.append(r)
        ev = pd.DataFrame(ev)
        if ev.empty:
            continue
        for label, part in (("all alts", ev), ("alts that hadn't caught up (<50% of BTC's move)", ev[ev.caught_up < 0.5])):
            st = _split_stats(part.f15, part.ts)
            rows.append({"BTC move": f"{k}s ≥{LEADLAG_Z:g}σ", "BTC events": len(keep), "group": label,
                         "+5s": part.f5.mean(), "+15s": st["mean bps"], "+30s": part.f30.mean(), "+60s": part.f60.mean(),
                         "hit% 15s": st["hit %"], "train 15s": st["train bps"], "test 15s": st["test bps"], "n": st["n"]})
    pd.set_option("display.width", 220)
    print("\n   After a sharp BTC move: alt follow-through in BTC's direction, bps (entry 1s later, before costs)")
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    best = max((r["test 15s"] for r in rows if r["n"] >= 100), default=np.nan)
    print(f"\n   LEAD-LAG VERDICT: best out-of-sample catch-up {best:+.2f} bps at 15s vs ≈{chase:.1f} bps to chase — "
          + ("worth a paper test." if best > chase else "not tradable at taker costs."))


# ── 6. Sessions ────────────────────────────────────────────────────────────────

def sessions_study(since: float) -> None:
    bars = pd.read_sql("SELECT symbol, ts, c, spread_bps, trades, buy_vol, sell_vol FROM bars_1s WHERE ts >= ?",
                       sqlite3.connect(C.MARKET_DB), params=(int(since),))
    if bars.empty:
        print("6. SESSIONS — no bars recorded.")
        return
    bars = bars.sort_values(["symbol", "ts"])
    bars["r"] = np.log(bars.c).groupby(bars.symbol).diff() * 1e4
    bars["hour"] = (bars.ts // 3600 % 24).astype(int)
    sec = bars.ts % 28800                                     # funding at 00/08/16 UTC
    bars["funding"] = (sec < 900) | (sec > 28800 - 900)
    per = bars.groupby("hour").agg(vol_per_min=("r", lambda x: x.std() * np.sqrt(60)),
                                   spread=("spread_bps", "median"), trades_s=("trades", "mean"))
    sig = _load_signals(since)
    if not sig.empty:
        sig["hour"] = (sig.ts // 3600 % 24).astype(int)
        per = per.join(sig.groupby("hour").agg(signals=("id", "size"), sig_fwd60=("fwd_60", "mean"),
                                               sig_hit60=("fwd_60", lambda x: (x > 0).mean() * 100)))
    tr = pd.read_sql("SELECT exit_ts, net_bps FROM trades WHERE exit_ts >= ?", sqlite3.connect(C.TRADES_DB),
                     params=(since,))
    if not tr.empty:
        tr["hour"] = (tr.exit_ts // 3600 % 24).astype(int)
        per = per.join(tr.groupby("hour").agg(trades=("net_bps", "size"), trade_net_bps=("net_bps", "mean")))
    print("6. SESSIONS — by UTC hour: 1-minute volatility (bps), median spread, trades/s per coin, "
          "signals and their +60s move, scalper trades' net bps\n")
    print(per.round(2).to_string())
    f = bars.groupby("funding").r.std() * np.sqrt(60)
    line = f"\n   ±15 min around funding settlements: volatility {f.get(True, np.nan):.2f} vs {f.get(False, np.nan):.2f} bps/min elsewhere"
    if not sig.empty:
        s_sec = sig.ts % 28800
        near = (s_sec < 900) | (s_sec > 28800 - 900)
        line += (f"; signals +60s {sig.fwd_60[near].mean():+.2f} (n {near.sum()}) vs "
                 f"{sig.fwd_60[~near].mean():+.2f} bps elsewhere")
    print(line)
    print("   Hours with bigger moves but similar spreads give the same costs more room; hours where "
          "signal +60s turns positive on both halves of the data are the ones to restrict trading to.")


# ── 7. Fill realism ────────────────────────────────────────────────────────────

def fills_study(since: float) -> None:
    con_t = sqlite3.connect(C.TRADES_DB)
    orders = pd.read_sql("SELECT * FROM orders WHERE placed_ts >= ?", con_t, params=(since,))
    if orders.empty:
        print("7. FILL REALISM — no orders recorded.")
        return
    bars = pd.read_sql("SELECT symbol, ts, h, l FROM bars_1s WHERE ts >= ?", sqlite3.connect(C.MARKET_DB),
                       params=(int(since) - 5,)).set_index(["symbol", "ts"]).sort_index()

    def traded_through(o) -> bool | None:
        s0 = int(o.fill_ts)
        try:
            w = bars.loc[(o.symbol, slice(s0 - 1, s0 + 1)), :]
        except KeyError:
            return None
        if w.empty:
            return None
        return bool((w.l < o.price).any()) if o.side == "buy" else bool((w.h > o.price).any())

    filled = orders[(orders.status == "filled") & orders.fill_ts.notna()].copy()
    filled["through"] = [traded_through(o) for o in filled.itertuples()]
    trades = pd.read_sql("SELECT id, net_bps, exit_reason FROM trades", con_t)
    ent = filled[filled.purpose == "entry"].merge(trades, left_on="position_id", right_on="id", how="left",
                                                  suffixes=("", "_t"))
    print("7. FILL REALISM — resting orders the queue model filled. 'Traded through' = price went beyond our "
          "level within ±1s (would have filled for real); 'queue only' = filled by our queue position alone.\n")
    rows = []
    for purpose, g in filled.groupby("purpose"):
        for thr, gg in g.groupby(g.through.map({True: "traded through", False: "queue only", None: "no bar"})):
            row = {"order": purpose, "fill": thr, "n": len(gg), "share %": len(gg) / len(g) * 100,
                   "adverse 5s bps": gg.adv_5.mean(), "time to fill s": gg.time_to_fill.median()}
            if purpose == "entry":
                row["trade net bps"] = ent[ent.through.map({True: "traded through", False: "queue only",
                                                            None: "no bar"}) == thr].net_bps.mean()
            rows.append(row)
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    # Adverse selection: where did price go after entries that filled vs ones that never did?
    ent_all = orders[orders.purpose == "entry"].copy()
    closes = pd.read_sql("SELECT symbol, ts, c FROM bars_1s WHERE ts >= ?", sqlite3.connect(C.MARKET_DB),
                         params=(int(since) - 5,)).set_index(["symbol", "ts"]).c

    def after(o, h=60):
        a, b = closes.get((o.symbol, int(o.placed_ts))), closes.get((o.symbol, int(o.placed_ts) + h))
        return (b / a - 1) * 1e4 * (1 if o.side == "buy" else -1) if a and b else np.nan

    ent_all["move60"] = [after(o) for o in ent_all.itertuples()]
    print("\n   Entry orders: price move 60s after placing, in the order's direction (bps):")
    print("   " + "  ".join(f"{k}: {g.move60.mean():+.2f} (n {g.move60.notna().sum()})"
                            for k, g in ent_all.groupby("status")))
    q = rows and next((r for r in rows if r["order"] == "entry" and r["fill"] == "queue only"), None)
    print("   If 'queue only' entries are a large share and the cancelled entries ran in our favour, the "
          "paper results are optimistic: real queues are longer than the model assumes."
          + (f" Here queue-only entries are {q['share %']:.0f}% of entry fills." if q else ""))


# ── 8. Liquidations ────────────────────────────────────────────────────────────

LIQ_WINDOW_S = 10
LIQ_MIN_USD = (100_000, 500_000, 2_000_000)


def liquidation_study(since: float) -> None:
    con = sqlite3.connect(C.MARKET_DB)
    try:
        liq = pd.read_sql("SELECT * FROM liquidations WHERE ts >= ?", con, params=(since,))
    except Exception:
        liq = pd.DataFrame()
    if liq.empty:
        print("8. LIQUIDATIONS — nothing recorded yet. The scalper feed records Binance's all-market "
              "liquidation stream while the backend runs; give it 1–2 weeks.")
        return
    px = _close_panel(since)
    lp = np.log(px)
    liq = liq[liq.symbol.isin(px.columns)]
    liq["sec"] = liq.ts.astype(int)
    # side SELL = longs being liquidated (forced selling) → pressure down.
    liq["signed"] = np.where(liq.side == "SELL", -1, 1) * liq.notional
    span_h = (liq.ts.max() - liq.ts.min()) / 3600
    print(f"8. LIQUIDATIONS — {len(liq):,} forced liquidations on our {liq.symbol.nunique()} coins over {span_h:.0f}h "
          f"(Binance sends ≤1 per coin per second, so totals are floors)\n")
    rows = []
    for sym, g in liq.groupby("symbol"):
        per_s = g.groupby("sec").signed.sum()
        idx = np.arange(per_s.index.min(), per_s.index.max() + 1)
        roll = per_s.reindex(idx, fill_value=0).rolling(LIQ_WINDOW_S).sum()
        for thr in LIQ_MIN_USD:
            hits = roll[roll.abs() >= thr]
            last = -1e18
            for t, v in hits.items():
                if t - last < 120 or t + 301 not in lp.index:
                    continue
                last = t
                d = -np.sign(v)                               # direction the liquidations push price
                rows.append({"thr": thr, "symbol": sym, "ts": t, "usd": abs(v),
                             **{f"f{h}": (lp.at[t + 1 + h, sym] - lp.at[t + 1, sym]) * 1e4 * d
                                for h in (10, 30, 60, 300)}})
    ev = pd.DataFrame(rows)
    if ev.empty:
        print("   no bursts above the thresholds yet")
        return
    out = []
    for thr, g in ev.groupby("thr"):
        st = _split_stats(g.f60, g.ts)
        out.append({"burst (10s)": f"≥ ${thr / 1e6:g}M", "events": len(g), "+10s": g.f10.mean(), "+30s": g.f30.mean(),
                    "+60s": st["mean bps"], "+300s": g.f300.mean(), "hit% 60s": st["hit %"],
                    "train 60s": st["train bps"], "test 60s": st["test bps"]})
    print("   Move after a liquidation burst, in the direction the liquidations push (+ = cascade continues, "
          "− = snap-back), bps, entry 1s later, before costs:")
    print(pd.DataFrame(out).round(2).to_string(index=False))


STUDIES = {"impulse": impulse_study, "leadlag": leadlag_study, "sessions": sessions_study,
           "fills": fills_study, "liquidations": liquidation_study}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=None, help="only use the last N hours of data")
    for name in STUDIES:
        ap.add_argument(f"--{name}", action="store_true", help=f"run only the {name} study")
    args = ap.parse_args()
    since = time.time() - args.hours * 3600 if args.hours else 0
    only = [n for n in STUDIES if getattr(args, n)]
    if only:
        for n in only:
            STUDIES[n](since)
            print()
        return

    s = _load_signals(since)
    if s.empty:
        print("No signals recorded in that window.")
        return
    pd.set_option("display.width", 200)
    cost = (costs.MAKER_FEE + costs.TAKER_FEE) * 1e4 + costs.LATENCY_BPS
    print(f"Scalper lab — {len(s):,} signals over {(s.ts.max() - s.ts.min()) / 3600:.1f}h · "
          f"round-trip cost to beat ≈ {cost:.1f} bps (maker in, taker out)\n")

    res = signal_research(s)
    print("1. SIGNAL RESEARCH — mid move after each signal, bps in its direction\n")
    print(res["all"].to_string(), "\n")
    for k in ("by_flow_z", "by_book", "by_side"):
        print(f"{k}:\n{res[k].to_string()}\n")

    print("2. FEATURE SCAN — +120s move, worst vs best quintile\n")
    print(feature_scan(s).to_string(index=False), "\n")

    print("3. STOP LAB — non-overlapping signals, 10-min paths, tuned on first 60%, tested on last 40%\n")
    lab, n = stop_lab(s, since)
    print(f"{n:,} signals replayed\n")
    print(lab.head(8).to_string(index=False))
    both = lab[(lab.train_bps > 0) & (lab.test_bps > 0)]
    best = lab.iloc[0]
    print(f"\nbest on train: SL {best.sl:g} / TP {best.tp:g} → train {best.train_bps:+.2f}, test {best.test_bps:+.2f} bps/trade")
    print(f"combos positive on BOTH halves: {len(both)} of {len(lab)}")
    edge = res["all"]["+120s bps"]
    print("\nVERDICT:", "edge worth testing live" if len(both) and edge > cost
          else f"no tradable edge — signals move {edge:+.2f} bps vs {cost:.1f} bps cost")
    for fn in STUDIES.values():
        print()
        fn(since)


if __name__ == "__main__":
    main()
