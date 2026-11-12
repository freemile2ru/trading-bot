"""High-momentum lab — is there a long leg worth having in liquid coins? (2026-11-12, pre-registered)

Why: the entry research of 2026-11-12 found our long entries random (E-ratio ≈ 1.0 over 331 trades, −1.02% a trade)
and every long lane negative; our longs buy dips in liquid coins. Fičura (2023, FFA WP 5:003) finds that in coins with
>$50M cap and >$5M/week volume the one-week effect is *momentum* driven by the short leg, that the distance from the
1-week high is a stronger sort (Q5 at the high +1.98%/wk vs Q1 far below it −0.70%/wk, t 4.9), and that the dip-buying
weekly reversal only exists in coins under ~$100k/week of volume. Borri, Liu, Tsyvinski & Wu (2025) find the two-week
momentum factor the one robust cross-sectional signal (weekly Q5−Q1 +2.6%, t 3.9). This replays the three sorts on the
coins we can actually trade, long leg only, with and without a BTC hedge, at our fees.

Method, fixed before looking:
  universe   the N_NAMES most-traded crypto USDT perps by today's 24h volume (a survivorship caveat the report carries),
             plus BTC as the hedge
  bars       daily perpetual klines from START
  slots      every Monday 00:00 UTC; signals from the bars that closed before it; held to the next Monday's open
  sorts      high_1w  close ÷ the highest high of the last 7 days − 1 (≤ 0; near the high ranks top)
             mom_2w   close ÷ the close 14 days before − 1
             dip_1w   −(close ÷ the close 7 days before − 1) — what our long lanes do, as the control
  books      long the top quintile, equal weight: alone (COST_BPS a side on the alt leg) and hedged with a BTC short of
             the same notional (both legs pay); and the long-short quintile spread as the literature's reference
  references BTC held through the same weeks; the universe held equal-weight (the alt drift)
  stats      weekly returns, non-overlapping; mean, t, both halves, hit rate, worst week
  verdict    a book "works" when its mean weekly return after costs is > 0 with t ≥ 2 and both halves positive;
             a book that works in the hedged form is the candidate for a forward paper book, nothing else is

    ./venv/bin/python -m research.high_momentum_lab
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone

import httpx

FAPI = "https://fapi.binance.com"
CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "high_momentum")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "high_momentum_results.json")
N_NAMES = 40
START = datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()
COST_BPS = 5.0                                                   # a side, taker, as the ledgers charge
STABLES = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "BUSDUSDT", "DAIUSDT", "USD1USDT", "EURUSDT"}
SORTS = ("high_1w", "mom_2w", "dip_1w")


def _get(c: httpx.Client, path: str, **params):
    for attempt in range(4):
        r = c.get(f"{FAPI}{path}", params=params)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (418, 429):
            time.sleep(10 * (attempt + 1))
            continue
        return []
    return []


def universe(c: httpx.Client) -> list[str]:
    info = _get(c, "/fapi/v1/exchangeInfo")
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in _get(c, "/fapi/v1/ticker/24hr")}
    syms = [s["symbol"] for s in info["symbols"] if s["contractType"] == "PERPETUAL" and s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
            and (s.get("underlyingType") or "COIN") == "COIN" and s["symbol"] not in STABLES and s["symbol"] != "BTCUSDT"]
    return sorted(syms, key=lambda s: -vol.get(s, 0))[:N_NAMES]


def daily(c: httpx.Client, sym: str) -> list[dict]:
    """Daily bars from START, cached on disk (refetched when the cache is older than a day)."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{sym}.json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 86400:
        return json.load(open(p))
    rows, cursor = [], int(START * 1000)
    while True:
        k = _get(c, "/fapi/v1/klines", symbol=sym, interval="1d", startTime=cursor, limit=1500)
        if not k:
            break
        rows += k
        if len(k) < 1500:
            break
        cursor = k[-1][0] + 1
    bars = [{"t": r[0] / 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in rows]
    json.dump(bars, open(p, "w"))
    return bars


def signals(bars: list[dict]) -> dict[str, float] | None:
    """The three sorts from the bars that closed before a slot (the last bar is the day before it)."""
    if len(bars) < 15:
        return None
    c = bars[-1]["c"]
    return {"high_1w": c / max(b["h"] for b in bars[-7:]) - 1, "mom_2w": c / bars[-15]["c"] - 1, "dip_1w": -(c / bars[-8]["c"] - 1)}


def _stats(xs: list[float]) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    half = n // 2
    halves = [round(100 * sum(xs[:half]) / half, 3) if half else None, round(100 * sum(xs[half:]) / (n - half), 3) if n - half else None]
    return {"n": n, "mean_pct": round(100 * m, 3), "t": round(m / sd * math.sqrt(n), 2) if sd else None, "halves_pct": halves,
            "hit_pct": round(100 * sum(1 for x in xs if x > 0) / n), "worst_week_pct": round(100 * min(xs), 2), "total_pct": round(100 * sum(xs), 1)}


def verdict(st: dict) -> str:
    if st.get("n", 0) < 20:
        return "too few weeks"
    return "works" if st["mean_pct"] > 0 and (st["t"] or 0) >= 2 and all(h is not None and h > 0 for h in st["halves_pct"]) else "no edge"


def replay(bars: dict[str, list[dict]], btc: list[dict]) -> dict:
    """Weekly slots over every Monday both the universe and BTC cover."""
    by_t = {s: {b["t"]: i for i, b in enumerate(bs)} for s, bs in bars.items()}
    btc_t = {b["t"]: i for i, b in enumerate(btc)}
    mondays = sorted(t for t in btc_t if datetime.fromtimestamp(t, timezone.utc).weekday() == 0 and t + 7 * 86400 in btc_t)
    weeks: dict[str, list[float]] = {f"{s}_{kind}": [] for s in SORTS for kind in ("long", "hedged", "long_short")}
    weeks["btc"], weeks["universe"] = [], []
    slots = []
    for t in mondays:
        rows = []
        for s, bs in bars.items():
            i = by_t[s].get(t)
            if i is None or i < 15 or t + 7 * 86400 not in by_t[s]:
                continue
            sig = signals(bs[:i])
            if not sig:
                continue
            fwd = bs[by_t[s][t + 7 * 86400]]["o"] / bs[i]["o"] - 1              # Monday open to the next Monday's open
            rows.append({"sym": s, "fwd": fwd, **sig})
        if len(rows) < 15:
            continue
        k = max(3, math.ceil(len(rows) / 5))
        cost = COST_BPS / 1e4
        btc_fwd = btc[btc_t[t + 7 * 86400]]["o"] / btc[btc_t[t]]["o"] - 1
        weeks["btc"].append(btc_fwd)
        weeks["universe"].append(sum(r["fwd"] for r in rows) / len(rows))
        slot = {"t": t, "n": len(rows)}
        for s in SORTS:
            ranked = sorted(rows, key=lambda r: r[s])
            top, bottom = ranked[-k:], ranked[:k]
            long = sum(r["fwd"] for r in top) / k - 2 * cost
            weeks[f"{s}_long"].append(long)
            weeks[f"{s}_hedged"].append(long - btc_fwd - 2 * cost)
            weeks[f"{s}_long_short"].append(long - sum(r["fwd"] for r in bottom) / k - 2 * cost)
            slot[s] = {"top": [r["sym"] for r in top], "long_pct": round(100 * long, 2)}
        slots.append(slot)
    books = {name: {**_stats(xs), "verdict": verdict(_stats(xs))} for name, xs in weeks.items()}
    return {"weeks": len(slots), "from": slots[0]["t"] if slots else None, "to": slots[-1]["t"] if slots else None, "books": books,
            "recent_slots": slots[-8:]}


def main() -> dict:
    with httpx.Client(timeout=30) as c:
        syms = universe(c)
        bars = {s: daily(c, s) for s in syms}
        btc = daily(c, "BTCUSDT")
    bars = {s: b for s, b in bars.items() if len(b) >= 30}
    out = {"ran_at": time.time(), "universe": sorted(bars), "n_names": len(bars), "cost_bps_side": COST_BPS, "start": START,
           "caveat": "universe chosen by today's volume (survivorship); weekly slots, Monday open to Monday open", **replay(bars, btc)}
    best = max(((n, b) for n, b in out["books"].items() if n.endswith("_hedged")), key=lambda nb: nb[1].get("mean_pct") or -1e9)
    out["candidate"] = best[0] if best[1]["verdict"] == "works" else None
    out["verdict"] = ("PROMOTE TO A FORWARD BOOK: " + best[0]) if out["candidate"] else "REJECTED: no hedged long leg works in both halves at our fees"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    for n, b in out["books"].items():
        print(f"{n:22} n={b.get('n'):3} mean={b.get('mean_pct')}%/wk t={b.get('t')} halves={b.get('halves_pct')} hit={b.get('hit_pct')}% worst={b.get('worst_week_pct')}% → {b.get('verdict')}")
    print(out["verdict"])
    return out


if __name__ == "__main__":
    main()
