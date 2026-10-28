"""Spike exits — close a trade when price moves sharply against it, or leave it to the stop?

Run: venv/bin/python -m research.spike_exit_lab   (5m candles cached by exit_rethink.py, BTC's too)

Every closed crypto swing trade is replayed from entry on 5-minute candles under the live protect
policy (exits.apply_bar), with an extra rule: close at the candle's close as soon as, over the last
15 minutes (3 candles),
  * coin   the trade's own coin moved against it by ≥ k × its stop distance, or
  * btc    BTC moved against the trade's direction by ≥ x %  (a market-wide spike: every position
           on the same side is hit together — the 10-18 23:00 cluster)
Judged as the paired difference with protect on decisions (one move across books = one), older vs
newer half, 90% bootstrap range.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import statistics as st

import httpx

from research.exit_rethink import BASE, bars_for
from trading import exits

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "spike_exit_results.json")
LOOK = 3


def replay(t: dict, bars: list[list], btc: dict[int, float], coin_k: float | None = None, btc_pct: float | None = None) -> float | None:
    sim = {k: t.get(k) for k in ("id", "symbol", "direction", "take_profit_1", "take_profit_2", "timeframe", "liquidation_price")}
    sim["entry_price"] = t.get("initial_entry_price") or t["entry_price"]
    sim["stop_loss"] = t.get("original_stop_loss") or t["stop_loss"]
    sim["quantity"] = t.get("initial_quantity") or t["quantity"]
    exits.stamp(sim, BASE.name, now=t["opened_ts"])
    r_unit = abs(sim["entry_price"] - sim["stop_loss"])
    s = 1 if t["direction"] == "long" else -1
    closes: list[float] = []
    for b in bars:
        if b[0] / 1000 < t["opened_ts"]:
            continue
        o, h, l, c, end = float(b[1]), float(b[2]), float(b[3]), float(b[4]), b[6] / 1000
        hit = exits.apply_bar(sim, o, h, l, c, end)
        if hit:
            return (hit[1] - sim["entry_price"]) * s / r_unit
        closes.append(c)
        if len(closes) > LOOK:
            if coin_k is not None and (c - closes[-1 - LOOK]) * s <= -coin_k * r_unit:
                return (c - sim["entry_price"]) * s / r_unit
            if btc_pct is not None:
                b_now, b_then = btc.get(b[0]), btc.get(b[0] - LOOK * 300_000)
                if b_now and b_then and (b_now / b_then - 1) * 100 * s <= -btc_pct:
                    return (c - sim["entry_price"]) * s / r_unit
    return None


VARIANTS = {"protect (live)": {}, **{f"coin −{k}R in 15m": {"coin_k": k} for k in (0.4, 0.6, 0.8)},
            **{f"BTC −{x}% in 15m": {"btc_pct": x} for x in (0.5, 0.8, 1.2)}}


async def main() -> dict:
    from trading import guards
    trades = []
    for book, w in guards._wallets().items():
        if not book.startswith("main"):
            trades += [t for t in w.all_trades()["closed"] if t.get("risk_usd") and t.get("opened_ts")]
    trades.sort(key=lambda t: t["opened_ts"])
    rows = []
    async with httpx.AsyncClient(timeout=15) as c:
        for t in trades:
            start = int(t["opened_ts"] * 1000) - 300_000
            bars = await bars_for(c, t["symbol"], start)
            bb = await bars_for(c, "BTCUSDT", start)
            if not bars or not bb:
                continue
            btc = {b[0]: float(b[4]) for b in bb}
            r = {k: replay(t, bars, btc, **v) for k, v in VARIANTS.items()}
            if None in r.values():
                continue
            rows.append({**r, "symbol": t["symbol"], "direction": t["direction"], "opened": t["opened_ts"]})
    dec: list[dict] = []
    for r in rows:
        g = next((d for d in reversed(dec[-40:]) if d["symbol"] == r["symbol"] and d["direction"] == r["direction"]
                  and r["opened"] - d["opened"] <= 7200), None)
        if g:
            g["n"] += 1
            for k in VARIANTS:
                g[k] += (r[k] - g[k]) / g["n"]
        else:
            dec.append({**r, "n": 1})
    rng, h, res = random.Random(9), len(dec) // 2, {}
    for k in VARIANTS:
        diffs = [d[k] - d["protect (live)"] for d in dec]
        boot = sorted(st.mean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
        res[k] = {"avg_r": round(st.mean(d[k] for d in dec), 3), "changed": sum(abs(x) > 1e-9 for x in diffs),
                  "vs_live": round(st.mean(diffs), 3), "older": round(st.mean(diffs[:h]), 3), "newer": round(st.mean(diffs[h:]), 3),
                  "ci90": [round(boot[100], 3), round(boot[1899], 3)]}
    out = {"trades": len(rows), "decisions": len(dec), "variants": res}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = asyncio.run(main())
    print(f"{r['trades']} trades → {r['decisions']} decisions")
    for k, v in r["variants"].items():
        print(f"  {k:20s} avg {v['avg_r']:+.3f}R changed {v['changed']:3d} | vs live {v['vs_live']:+.3f} (old {v['older']:+.3f} new {v['newer']:+.3f}) ci90 {v['ci90']}")
