"""Scaling in and out — add to winners once the trailing stop protects them; trim losers earlier.

Run: venv/bin/python -m research.scale_in_out_lab   (5m candles cached by exit_rethink.py)

research/stop_sizing_lab.py added half a position at +0.5R — before the protect exit has moved anything,
so the add carried fresh risk. Asked 2026-10-26: add once the trailing stop protects the trade. Under
protect the stop goes to breakeven at +1R and trails 0.5R behind the best price from there, so an add at
+1R or +1.5R exits with the trade at roughly ≥ +0.5R (a bounded loss on the add, nothing on the original).
Trimming: close part of a loser at −0.3R / −0.5R / −0.7R, or after 4h if it's still below entry.
Every closed crypto swing trade replayed on 5m candles (exits.apply_bar under protect), fees in, results in
R of the original risk, per decision, older vs newer half, 90% bootstrap range vs the live setup.
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

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "scale_in_out_results.json")
FILL = 0.0008


def replay(t: dict, bars: list[list]) -> dict | None:
    s = 1 if t["direction"] == "long" else -1
    entry = t.get("initial_entry_price") or t["entry_price"]
    stop0 = t.get("original_stop_loss") or t["stop_loss"]
    unit = abs(entry - stop0)
    sim = {"id": t["id"], "symbol": t["symbol"], "direction": t["direction"], "entry_price": entry, "stop_loss": stop0,
           "take_profit_1": entry + s * 50 * unit, "take_profit_2": entry + s * 99 * unit, "quantity": 1.0,
           "timeframe": t.get("timeframe")}
    exits.stamp(sim, BASE.name, now=t["opened_ts"])
    path = []                                    # (end_ts, low, high, close)
    for b in bars:
        if b[0] / 1000 < t["opened_ts"]:
            continue
        o, h, l, c, end = float(b[1]), float(b[2]), float(b[3]), float(b[4]), b[6] / 1000
        path.append((end, l, h, c))
        hit = exits.apply_bar(sim, o, h, l, c, end)
        if hit:
            return {"s": s, "entry": entry, "unit": unit, "frac": unit / entry, "exit": hit[1], "path": path, "t0": t["opened_ts"]}
    return None


def r_of(x: dict, px: float) -> float:
    return (px - x["entry"]) * x["s"] / x["unit"]


def first_touch(x: dict, level_r: float) -> int | None:
    lvl = x["entry"] + x["s"] * level_r * x["unit"]
    for i, (_, l, h, _) in enumerate(x["path"]):
        if (level_r > 0 and ((h >= lvl) if x["s"] > 0 else (l <= lvl))) or (level_r < 0 and ((l <= lvl) if x["s"] > 0 else (h >= lvl))):
            return i
    return None


def variants(x: dict) -> dict:
    fc = FILL / x["frac"]                         # one fill, in R, for a full-size position
    base = r_of(x, x["exit"]) - 2 * fc
    out = {"live": base}
    for lvl in (1.0, 1.5):
        for size in (0.5, 1.0):
            add = 0.0
            if first_touch(x, lvl) is not None:
                add = size * (r_of(x, x["exit"]) - lvl) - size * 2 * fc
            out[f"add {size:g}× at +{lvl:g}R"] = base + add
    for lvl in (-0.3, -0.5, -0.7):
        i = first_touch(x, lvl)
        out[f"trim ½ at {lvl:g}R"] = base if i is None else 0.5 * lvl + 0.5 * r_of(x, x["exit"]) - 2 * fc
    # time trim: after 4h, if below entry, close half at that bar's close
    trim = base
    for end, _, _, c in x["path"]:
        if end - x["t0"] >= 4 * 3600:
            if r_of(x, c) < 0:
                trim = 0.5 * r_of(x, c) + 0.5 * r_of(x, x["exit"]) - 2 * fc
            break
    out["trim ½ after 4h if red"] = trim
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
            x = replay(t, bars) if bars else None
            if x:
                rows.append({**variants(x), "symbol": t["symbol"], "direction": t["direction"], "opened": t["opened_ts"]})
    names = [k for k in rows[0] if k not in ("symbol", "direction", "opened")]
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
    rng, h, res = random.Random(17), len(dec) // 2, {}
    for k in names:
        diffs = [d[k] - d["live"] for d in dec]
        boot = sorted(st.mean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
        res[k] = {"avg_r": round(st.mean(d[k] for d in dec), 3), "vs_live": round(st.mean(diffs), 3),
                  "older": round(st.mean(diffs[:h]), 3), "newer": round(st.mean(diffs[h:]), 3),
                  "ci90": [round(boot[100], 3), round(boot[1899], 3)]}
    out = {"trades": len(rows), "decisions": len(dec), "variants": res}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = asyncio.run(main())
    print(f"{r['trades']} trades → {r['decisions']} decisions")
    for k, v in r["variants"].items():
        print(f"  {k:24s} avg {v['avg_r']:+.3f}R | vs live {v['vs_live']:+.3f} (old {v['older']:+.3f}, new {v['newer']:+.3f}) ci90 {v['ci90']}")
