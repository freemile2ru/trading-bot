"""Watchlist tightening — did moving the stop when a coin left the watchlist help or hurt?

Run: venv/bin/python -m research.tighten_lab   (fetches Binance 5m perp candles; writes
data/research_cache/tighten_lab.json)

The autotrader (and its rules twin) moves a trade's stop halfway to the price — or to breakeven
if ≥ +0.5R — the first time its coin drops off the scanner's watchlist. 92 trades then closed on
that tightened stop at −0.60R on average, having peaked at +0.52R. The question is what those
trades would have done without it.

Method: replay every closed autotrader trade from its entry on 5-minute candles under the live
"protect" policy (exits.apply_bar — the code the monitor runs), from its ORIGINAL stop, with no
tightening. First check the replay against trades that were never tightened (their replayed R
should match the real one), then compare tightened trades' real R with their untightened replay.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import statistics as st
from datetime import datetime

import httpx

from trading import exits

FAPI = "https://fapi.binance.com/fapi/v1/klines"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "tighten_lab.json")
BOOKS = ("auto", "auto_live", "auto_rules")


async def _bars(client: httpx.AsyncClient, symbol: str, start_ms: int, n: int = 1500) -> list[list]:
    for attempt in range(3):
        try:
            r = await client.get(FAPI, params={"symbol": symbol, "interval": "5m", "startTime": start_ms, "limit": n})
            r.raise_for_status()
            return r.json()
        except Exception:
            await asyncio.sleep(1 + attempt)
    return []


def replay(t: dict, bars: list[list]) -> tuple[float, str] | None:
    """R of the trade under its own exit policy from its original stop, untouched."""
    sim = {k: copy.deepcopy(t[k]) for k in ("id", "symbol", "direction", "take_profit_1", "take_profit_2", "timeframe")}
    sim["entry_price"] = t.get("initial_entry_price") or t["entry_price"]
    sim["stop_loss"] = t.get("original_stop_loss") or t["stop_loss"]
    sim["quantity"] = t.get("initial_quantity") or t["quantity"]
    sim["opened_ts"] = t["opened_ts"]
    sim["liquidation_price"] = t.get("liquidation_price")
    exits.stamp(sim, t["exit_policy"], now=t["opened_ts"])
    r_unit = abs(sim["entry_price"] - sim["stop_loss"])
    sim["r_unit"] = r_unit
    s = 1 if t["direction"] == "long" else -1
    for b in bars:
        if b[0] / 1000 < t["opened_ts"]:
            continue
        o, h, l, c, end = float(b[1]), float(b[2]), float(b[3]), float(b[4]), b[6] / 1000
        hit = exits.apply_bar(sim, o, h, l, c, end)
        if hit:
            reason, price = hit
            return (price - sim["entry_price"]) * s / r_unit, reason
    return None


async def main() -> dict:
    from trading import guards
    trades = []
    for book, w in guards._wallets().items():
        if book not in BOOKS:
            continue
        for t in w.all_trades()["closed"]:
            if t.get("risk_usd") and t.get("opened_ts") and t.get("exit_policy") in exits.POLICIES:
                trades.append({**t, "book": book})
    out = []
    async with httpx.AsyncClient(timeout=15) as client:
        for i, t in enumerate(trades):
            bars = await _bars(client, t["symbol"], int(t["opened_ts"] * 1000) - 300_000)
            rep = replay(t, bars) if bars else None
            if rep is None:
                continue
            # Real R on price only (fees and funding out), so it's comparable with the replay.
            s = 1 if t["direction"] == "long" else -1
            entry = t.get("initial_entry_price") or t["entry_price"]
            r_unit = abs(entry - (t.get("original_stop_loss") or t["stop_loss"]))
            gross = t["pnl_usd"] + (t.get("fees_usd") or 0) - (t.get("funding_usd") or 0)
            real_r = gross / t["risk_usd"]
            out.append({"book": t["book"], "id": t["id"], "symbol": t["symbol"], "direction": t["direction"],
                        "opened": t["opened_ts"], "tightened": bool(t.get("watchlist_tightened")) and t.get("close_reason") == "tightened_stop",
                        "touched": bool(t.get("watchlist_tightened")), "real_r": round(real_r, 3),
                        "replay_r": round(rep[0], 3), "replay_exit": rep[1], "real_exit": t.get("close_reason")})
            await asyncio.sleep(0.15)

    calib = [x for x in out if not x["touched"]]
    tight = [x for x in out if x["tightened"]]
    touched_ok = [x for x in out if x["touched"] and not x["tightened"]]
    mean = lambda xs: round(st.mean(xs), 3) if xs else None
    rng = random.Random(3)

    def ci(diffs):
        if len(diffs) < 5:
            return None
        b = sorted(st.mean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
        return [round(b[100], 3), round(b[1899], 3)]
    every_touched = [x for x in out if x["touched"]]
    d_all = [x["real_r"] - x["replay_r"] for x in every_touched]
    h = len(every_touched) // 2
    res = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "replayed": len(out),
        "calibration": {"trades": len(calib), "real_r": mean([x["real_r"] for x in calib]),
                        "replay_r": mean([x["replay_r"] for x in calib]),
                        "abs_err": mean([abs(x["real_r"] - x["replay_r"]) for x in calib]),
                        "same_exit_pct": round(sum(x["real_exit"] == x["replay_exit"] for x in calib) / len(calib) * 100, 1) if calib else None},
        "closed_on_tightened_stop": {"trades": len(tight), "real_r": mean([x["real_r"] for x in tight]),
                                     "replay_r": mean([x["replay_r"] for x in tight]),
                                     "replay_wins": sum(x["replay_r"] > 0 for x in tight)},
        "tightened_but_survived": {"trades": len(touched_ok), "real_r": mean([x["real_r"] for x in touched_ok]),
                                   "replay_r": mean([x["replay_r"] for x in touched_ok])},
        "all_tightened": {"trades": len(every_touched), "effect_r": mean(d_all), "ci90": ci(d_all),
                          "effect_older": mean(d_all[:h]), "effect_newer": mean(d_all[h:])},
        "rows": out,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(res, open(OUT, "w"), indent=1)
    return res


if __name__ == "__main__":
    r = asyncio.run(main())
    print(json.dumps({k: v for k, v in r.items() if k != "rows"}, indent=1))
