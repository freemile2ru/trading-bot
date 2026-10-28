"""Confirmation entries — wait for the move to start before entering?

Run: venv/bin/python -m research.confirm_entry_lab   (5m candles cached by exit_rethink.py; writes
data/research_cache/confirm_entry_results.json)

From research/loss_patterns.py: of 125 losing decisions, 54 never got above +0.25R — the move
simply never started. We enter at market the moment a signal fires. A confirmation entry instead
rests a stop-entry order X R beyond the signal price and only enters if price trades there within
W hours; a signal that never moves is skipped.

Every closed crypto swing trade is replayed from its signal on 5m candles (exits.apply_bar under
protect, the thesis-faded exit not included):
  * market (live)   enter at the signal price — the baseline replay
  * confirm X/W     enter at signal ± X R (+ 3 bps slippage) if touched within W hours, with
                    either the ORIGINAL stop (wider risk, so R is measured on the new distance) or
                    the stop moved by the same amount (same risk distance)
Results are in R of the trade's own risk, skipped signals count 0. Decisions (one move across
books = one), older vs newer half, bootstrap range on the paired difference with market entry,
and a PLACEBO: skip the same share of signals at random — does confirming pick better trades, or
does it only trade less?
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

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "confirm_entry_results.json")
SLIP = 3 / 1e4


def run(t: dict, bars: list[list], x_r: float = 0.0, w_h: float = 0.0, keep_stop: bool = True) -> float | None:
    """R of the trade (0 if never entered). x_r=0: market entry at the signal price."""
    s = 1 if t["direction"] == "long" else -1
    sig = t.get("initial_entry_price") or t["entry_price"]
    stop0 = t.get("original_stop_loss") or t["stop_loss"]
    dist = abs(sig - stop0)
    start = t["opened_ts"]
    entry_ts, entry = start, sig
    rows = [b for b in bars if b[0] / 1000 >= start]
    if x_r < 0:                                   # pullback: a resting limit x R against the signal price
        limit = sig + s * x_r * dist
        entry = None
        for b in rows:
            if b[6] / 1000 > start + w_h * 3600:
                break
            if (float(b[3]) <= limit) if s > 0 else (float(b[2]) >= limit):
                entry, entry_ts = (min(limit, float(b[1])) if s > 0 else max(limit, float(b[1]))), b[6] / 1000
                break
        if entry is None:
            return 0.0
        if (entry - stop0) * s <= 0:              # gapped through the stop before filling
            return 0.0
    elif x_r > 0:
        trigger = sig + s * x_r * dist
        entry = None
        for b in rows:
            if b[6] / 1000 > start + w_h * 3600:
                break
            if (float(b[2]) >= trigger) if s > 0 else (float(b[3]) <= trigger):
                entry, entry_ts = max(trigger, float(b[1])) if s > 0 else min(trigger, float(b[1])), b[6] / 1000
                entry *= (1 + s * SLIP)
                break
        if entry is None:
            return 0.0
    stop = stop0 if keep_stop else entry - s * dist
    tp1 = entry + s * abs(t["take_profit_1"] - sig)
    tp2 = entry + s * abs(t["take_profit_2"] - sig)
    sim = {"id": t["id"], "symbol": t["symbol"], "direction": t["direction"], "entry_price": entry, "stop_loss": stop,
           "take_profit_1": tp1, "take_profit_2": tp2, "quantity": 1.0, "timeframe": t.get("timeframe")}
    exits.stamp(sim, BASE.name, now=entry_ts)
    r_unit = abs(entry - stop)
    for b in rows:
        if b[6] / 1000 <= entry_ts:
            continue
        hit = exits.apply_bar(sim, float(b[1]), float(b[2]), float(b[3]), float(b[4]), b[6] / 1000)
        if hit:
            return (hit[1] - entry) * s / r_unit
    return None


VARIANTS = {f"confirm +{x}R within {w}h ({'orig' if k else 'same-distance'} stop)": (x, w, k)
            for x in (0.25, 0.5) for w in (2, 6) for k in (True, False)}
VARIANTS |= {f"pullback -{x}R within {w}h ({'orig' if k else 'same-distance'} stop)": (-x, w, k)
             for x in (0.25, 0.5) for w in (2, 6) for k in (True, False)}


async def main() -> dict:
    from trading import guards
    trades = []
    for book, w in guards._wallets().items():
        if book.startswith("main"):
            continue
        trades += [t for t in w.all_trades()["closed"] if t.get("risk_usd") and t.get("opened_ts")]
    trades.sort(key=lambda t: t["opened_ts"])
    rows = []
    async with httpx.AsyncClient(timeout=15) as c:
        for t in trades:
            bars = await bars_for(c, t["symbol"], int(t["opened_ts"] * 1000) - 300_000)
            if not bars:
                continue
            r = {"market": run(t, bars)}
            r.update({k: run(t, bars, *v) for k, v in VARIANTS.items()})
            if None in r.values():
                continue
            rows.append({**r, "symbol": t["symbol"], "direction": t["direction"], "opened": t["opened_ts"]})
    names = ["market", *VARIANTS]
    dec: list[dict] = []
    for r in rows:
        g = next((d for d in reversed(dec[-40:]) if d["symbol"] == r["symbol"] and d["direction"] == r["direction"]
                  and r["opened"] - d["opened"] <= 7200), None)
        if g:
            g["n"] += 1
            for k in names:
                g[k] += (r[k] - g[k]) / g["n"]
        else:
            dec.append({**r, "n": 1})
    rng, h, res = random.Random(12), len(dec) // 2, {}
    for k in names:
        vals = [d[k] for d in dec]
        diffs = [d[k] - d["market"] for d in dec]
        taken = [v for v in vals if v != 0.0] if k != "market" else vals
        frac = len(taken) / len(vals)
        boot = sorted(st.mean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
        # Placebo: take the same share of signals at random, at market.
        plac = sorted(st.mean([d["market"] if rng.random() < frac else 0.0 for d in dec]) for _ in range(500))
        mean_v = st.mean(vals)
        res[k] = {"avg_r_per_signal": round(mean_v, 3), "taken_pct": round(frac * 100, 1),
                  "avg_r_per_trade": round(st.mean(taken), 3) if taken else None,
                  "win_pct": round(sum(v > 0 for v in taken) / len(taken) * 100, 1) if taken else None,
                  "vs_market": round(st.mean(diffs), 3), "older": round(st.mean(diffs[:h]), 3), "newer": round(st.mean(diffs[h:]), 3),
                  "ci90": [round(boot[100], 3), round(boot[1899], 3)],
                  "placebo_mean": round(st.mean(plac), 3), "p_placebo": round(sum(p >= mean_v for p in plac) / len(plac), 3)}
    out = {"trades": len(rows), "decisions": len(dec), "variants": res}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = asyncio.run(main())
    print(f"{r['trades']} trades → {r['decisions']} decisions")
    for k, v in r["variants"].items():
        print(f"  {k:48s} per signal {v['avg_r_per_signal']:+.3f}R taken {v['taken_pct']:5.1f}% per trade {v['avg_r_per_trade']:+.3f} win {v['win_pct']}% | "
              f"vs market {v['vs_market']:+.3f} (old {v['older']:+.3f} new {v['newer']:+.3f}) ci90 {v['ci90']} | placebo {v['placebo_mean']:+.3f} p={v['p_placebo']}")
