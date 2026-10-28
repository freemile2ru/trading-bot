"""Exit rethink — would an earlier breakeven or an early time-stop have saved our losing trades?

Run: venv/bin/python -m research.exit_rethink   (fetches Binance 5m perp candles, cached under
data/research_cache/exit_rethink_bars/; writes data/research_cache/exit_rethink.json)

From research/loss_patterns.py: of 125 losing decisions, 54 never got above +0.25R (no follow-
through at all) and 58 reached +0.25 to +1R before turning into a loss. Two families of fix:
  * earlier breakeven: move the stop to entry at +0.5R / +0.75R instead of +1R
  * time-stop: close at market if the trade hasn't reached +X R within N hours
Every closed crypto swing trade is replayed from its entry and ORIGINAL stop on 5-minute candles
under each variant (exits.apply_bar — the monitor's own code), so all variants see identical
entries. tighten_lab.py checked this replay against reality (same exit on 15/15 untouched trades).
Judged on decisions (the same move across books counts once), older vs newer half, with a
bootstrap range on the paired difference against the live policy (protect).
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import statistics as st
from dataclasses import replace

import httpx

from trading import exits

FAPI = "https://fapi.binance.com/fapi/v1/klines"
HERE = os.path.dirname(__file__)
CACHE = os.path.join(HERE, "..", "data", "research_cache", "exit_rethink_bars")
OUT = os.path.join(HERE, "..", "data", "research_cache", "exit_rethink.json")
BASE = exits.POLICIES["protect"]
# name → (policy, time-stop hours or None, time-stop min R)
VARIANTS = {
    "protect": (BASE, None, None),
    "be at +0.5R": (replace(BASE, name="x_be05", be_at_r=0.5), None, None),
    "be at +0.75R": (replace(BASE, name="x_be075", be_at_r=0.75), None, None),
    "trail from +0.75R": (replace(BASE, name="x_tr075", be_at_r=0.75, trail_at_r=0.75), None, None),
    "time-stop 4h if < +0.25R": (replace(BASE, name="x_ts4"), 4, 0.25),
    "time-stop 6h if < +0.25R": (replace(BASE, name="x_ts6"), 6, 0.25),
    "time-stop 12h if < +0.25R": (replace(BASE, name="x_ts12"), 12, 0.25),
    "time-stop 6h if < +0.5R": (replace(BASE, name="x_ts6b"), 6, 0.5),
    "be +0.5R & time-stop 6h": (replace(BASE, name="x_combo"), 6, 0.25),
}
VARIANTS["be +0.5R & time-stop 6h"] = (replace(BASE, name="x_combo", be_at_r=0.5), 6, 0.25)


async def bars_for(client, symbol: str, start_ms: int) -> list[list]:
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{symbol}_{start_ms}.json")
    if os.path.exists(path):
        return json.load(open(path))
    for attempt in range(3):
        try:
            r = await client.get(FAPI, params={"symbol": symbol, "interval": "5m", "startTime": start_ms, "limit": 320})
            r.raise_for_status()
            data = r.json()
            json.dump(data, open(path, "w"))
            await asyncio.sleep(0.12)
            return data
        except Exception:
            await asyncio.sleep(1 + attempt)
    return []


def replay(t: dict, bars: list[list], policy, ts_h, ts_r) -> float | None:
    exits.POLICIES[policy.name] = policy
    sim = {k: copy.deepcopy(t.get(k)) for k in ("id", "symbol", "direction", "take_profit_1", "take_profit_2", "timeframe", "liquidation_price")}
    sim["entry_price"] = t.get("initial_entry_price") or t["entry_price"]
    sim["stop_loss"] = t.get("original_stop_loss") or t["stop_loss"]
    sim["quantity"] = t.get("initial_quantity") or t["quantity"]
    sim["opened_ts"] = t["opened_ts"]
    exits.stamp(sim, policy.name, now=t["opened_ts"])
    if ts_h:
        sim["stale_after_s"], sim["stale_min_r"] = ts_h * 3600, ts_r
    r_unit = abs(sim["entry_price"] - sim["stop_loss"])
    sim["r_unit"] = r_unit
    s = 1 if t["direction"] == "long" else -1
    for b in bars:
        if b[0] / 1000 < t["opened_ts"]:
            continue
        hit = exits.apply_bar(sim, float(b[1]), float(b[2]), float(b[3]), float(b[4]), b[6] / 1000)
        if hit:
            return (hit[1] - sim["entry_price"]) * s / r_unit
    return None


async def main() -> dict:
    from trading import guards
    trades = []
    for book, w in guards._wallets().items():
        if book.startswith("main"):
            continue
        trades += [{**t, "book": book} for t in w.all_trades()["closed"] if t.get("risk_usd") and t.get("opened_ts")]
    trades.sort(key=lambda t: t["opened_ts"])
    rows = []
    async with httpx.AsyncClient(timeout=15) as client:
        for t in trades:
            bars = await bars_for(client, t["symbol"], int(t["opened_ts"] * 1000) - 300_000)
            if not bars:
                continue
            rs = {name: replay(t, bars, *v) for name, v in VARIANTS.items()}
            if None in rs.values():
                continue
            rows.append({"book": t["book"], "symbol": t["symbol"], "direction": t["direction"], "opened": t["opened_ts"], **rs})
    for v in VARIANTS.values():
        exits.POLICIES.pop(v[0].name, None) if v[0].name.startswith("x_") else None

    # Decisions: the same move across books counts once (average of its trades).
    dec: list[dict] = []
    for r in rows:
        g = next((d for d in reversed(dec[-40:]) if d["symbol"] == r["symbol"] and d["direction"] == r["direction"]
                  and r["opened"] - d["opened"] <= 2 * 3600), None)
        if g:
            g["n"] += 1
            for k in VARIANTS:
                g[k] += (r[k] - g[k]) / g["n"]
        else:
            dec.append({**r, "n": 1})
    rng = random.Random(4)
    h = len(dec) // 2
    res = {}
    for k in VARIANTS:
        diffs = [d[k] - d["protect"] for d in dec]
        boot = sorted(st.mean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
        res[k] = {"avg_r": round(st.mean(d[k] for d in dec), 3), "win_pct": round(sum(d[k] > 0 for d in dec) / len(dec) * 100, 1),
                  "vs_protect": round(st.mean(diffs), 3), "older": round(st.mean(diffs[:h]), 3), "newer": round(st.mean(diffs[h:]), 3),
                  "ci90": [round(boot[100], 3), round(boot[1899], 3)]}
    out = {"trades": len(rows), "decisions": len(dec), "variants": res}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = asyncio.run(main())
    print(f"{r['trades']} trades → {r['decisions']} decisions")
    for k, v in r["variants"].items():
        print(f"  {k:28s} avg {v['avg_r']:+.3f}R win {v['win_pct']:5.1f}% | vs protect {v['vs_protect']:+.3f} "
              f"(older {v['older']:+.3f}, newer {v['newer']:+.3f}) ci90 {v['ci90']}")
