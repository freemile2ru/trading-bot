"""Stops and in-trade sizing — wider/tighter stops, averaging down (DCA), cutting, adding to winners.

Run: venv/bin/python -m research.stop_sizing_lab   (5m candles cached by exit_rethink.py)

Asked 2026-10-26: stop-loss exits are the biggest losses (−$10.2k over 117 trades in the Trades page) —
should stops sit wider, should entries wait lower, and should running trades be averaged down (DCA) or
cut back depending on how they're doing? Every closed crypto swing trade is replayed from its entry on
5-minute candles under the live protect policy (exits.apply_bar), with fees in:

  stop width   the stop at 0.75× / 1.25× / 1.5× / 2× its distance, the position resized so the money at
               risk is the same — R is per unit of risk, so a wider stop isn't "free"
  DCA          add half a position if price reaches −0.5R (stop unchanged — risk grows to 1.25R)
  cut          close half if price reaches −0.5R
  pyramid      add half a position if price reaches +0.5R (stop unchanged)
Results in R of the ORIGINAL risk, per decision (one move across books = one), older vs newer half,
90% bootstrap range on the paired difference with the live setup. Costs: 0.08% a fill on notional
(taker + slippage), so a fill costs 0.0008 / stop% in R.
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

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "stop_sizing_results.json")
FILL = 0.0008


def _run(t: dict, bars: list[list], stop_x: float = 1.0) -> tuple[float, float, float, list[tuple[float, float, float]]] | None:
    """(R gross, exit price, stop fraction, path of (low, high, end) bars until exit) under protect."""
    s = 1 if t["direction"] == "long" else -1
    entry = t.get("initial_entry_price") or t["entry_price"]
    stop0 = t.get("original_stop_loss") or t["stop_loss"]
    dist = abs(entry - stop0) * stop_x
    sim = {"id": t["id"], "symbol": t["symbol"], "direction": t["direction"], "entry_price": entry,
           "stop_loss": entry - s * dist, "take_profit_1": entry + s * 50 * dist, "take_profit_2": entry + s * 99 * dist,
           "quantity": 1.0, "timeframe": t.get("timeframe")}
    exits.stamp(sim, BASE.name, now=t["opened_ts"])
    path = []
    for b in bars:
        if b[0] / 1000 < t["opened_ts"]:
            continue
        o, h, l, c, end = float(b[1]), float(b[2]), float(b[3]), float(b[4]), b[6] / 1000
        path.append((l, h, end))
        hit = exits.apply_bar(sim, o, h, l, c, end)
        if hit:
            return (hit[1] - entry) * s / dist, hit[1], dist / entry, path
    return None


def _touched(path, level: float, s: int, adverse: bool) -> bool:
    for l, h, _ in path:
        if adverse and ((l <= level) if s > 0 else (h >= level)):
            return True
        if not adverse and ((h >= level) if s > 0 else (l <= level)):
            return True
    return False


def variants(t: dict, bars: list[list]) -> dict | None:
    base = _run(t, bars)
    if base is None:
        return None
    r, exit_px, stop_frac, path = base
    s = 1 if t["direction"] == "long" else -1
    entry = t.get("initial_entry_price") or t["entry_price"]
    unit = abs(entry - (t.get("original_stop_loss") or t["stop_loss"]))
    cost = 2 * FILL / stop_frac                                   # in and out, in R
    out = {"live": r - cost}
    for x in (0.75, 1.25, 1.5, 2.0):
        w = _run(t, bars, x)
        if w is None:
            return None
        out[f"stop {x}×"] = w[0] - 2 * FILL / w[2]
    down, up = entry - s * 0.5 * unit, entry + s * 0.5 * unit
    hit_down, hit_up = _touched(path, down, s, True), _touched(path, up, s, False)
    add_fill = FILL / stop_frac
    # DCA: +0.5 unit at −0.5R, out with the trade
    out["DCA ½ at −0.5R"] = out["live"] + (0.5 * (exit_px - down) * s / unit - 0.5 * 2 * add_fill if hit_down else 0.0)
    # cut: close half at −0.5R
    out["cut ½ at −0.5R"] = (0.5 * (-0.5) + 0.5 * r - cost) if hit_down else out["live"]
    # pyramid: +0.5 unit at +0.5R
    out["add ½ at +0.5R"] = out["live"] + (0.5 * (exit_px - up) * s / unit - 0.5 * 2 * add_fill if hit_up else 0.0)
    out["_peak_lt_025"] = not _touched(path, entry + s * 0.25 * unit, s, False)
    return out


async def main() -> dict:
    from trading import guards
    trades = []
    for book, w in guards._wallets().items():
        if not book.startswith("main"):
            trades += [t for t in w.all_trades()["closed"] if t.get("risk_usd") and t.get("opened_ts") and not t.get("forced_daily")]
    trades.sort(key=lambda t: t["opened_ts"])
    rows = []
    async with httpx.AsyncClient(timeout=15) as c:
        for t in trades:
            bars = await bars_for(c, t["symbol"], int(t["opened_ts"] * 1000) - 300_000)
            v = variants(t, bars) if bars else None
            if v:
                rows.append({**v, "symbol": t["symbol"], "direction": t["direction"], "opened": t["opened_ts"]})
    names = [k for k in rows[0] if not k.startswith("_") and k not in ("symbol", "direction", "opened")]
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
    rng, h, res = random.Random(13), len(dec) // 2, {}
    for k in names:
        diffs = [d[k] - d["live"] for d in dec]
        boot = sorted(st.mean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
        vals = [d[k] for d in dec]
        res[k] = {"avg_r": round(st.mean(vals), 3), "win_pct": round(sum(v > 0 for v in vals) / len(vals) * 100, 1),
                  "vs_live": round(st.mean(diffs), 3), "older": round(st.mean(diffs[:h]), 3), "newer": round(st.mean(diffs[h:]), 3),
                  "ci90": [round(boot[100], 3), round(boot[1899], 3)]}
    out = {"trades": len(rows), "decisions": len(dec), "never_reached_quarter_r_pct": round(sum(r["_peak_lt_025"] for r in rows) / len(rows) * 100, 1),
           "variants": res}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = asyncio.run(main())
    print(f"{r['trades']} trades → {r['decisions']} decisions · {r['never_reached_quarter_r_pct']}% never got +0.25R in their favour")
    for k, v in r["variants"].items():
        print(f"  {k:18s} avg {v['avg_r']:+.3f}R win {v['win_pct']:5.1f}% | vs live {v['vs_live']:+.3f} (old {v['older']:+.3f}, new {v['newer']:+.3f}) ci90 {v['ci90']}")
