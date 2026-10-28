"""Thesis exits — should a running trade be closed when the scanner stops believing in it?

Run: venv/bin/python -m research.thesis_exit_lab   (reads the scanner's logged readings in
signals.db and the 5m candles cached by exit_rethink.py; writes data/research_cache/thesis_exit_lab.json)

Running trades are managed by their exit policy (protect) plus, on the autotrader only, a flip
exit that needs conviction ≥ 0.30 against the trade AND the 15m signal against it — it has never
fired. This tests simpler "the thesis is gone" exits on every closed crypto swing trade:

  * the scanner logs each watched coin's conviction every few minutes (scan_features)
  * each trade is replayed on 5m candles under protect from its entry (the monitor's own code);
    a thesis rule closes it at the first candle close after its trigger, if protect hasn't
    already closed it
  * triggers are read from readings strictly after entry, and must hold on two readings in a
    row (a single noisy reading doesn't close anything)
Judged on decisions (one move across books counts once), older vs newer half, bootstrap range
on the paired difference with protect.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import statistics as st

import httpx

from research.exit_rethink import BASE, bars_for
from storage.db import SIGNALS_DB
from trading import exits

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "thesis_exit_lab.json")
MIN_READINGS = 6


def _readings(symbol: str, start: float, end: float) -> list[tuple[float, float]]:
    con = sqlite3.connect(f"file:{SIGNALS_DB}?mode=ro", uri=True)
    try:
        return con.execute("SELECT ts, conviction FROM scan_features WHERE symbol=? AND ts>? AND ts<=? ORDER BY ts",
                           (symbol, start, end)).fetchall()
    finally:
        con.close()


def _first(readings, s: int, entry_conv: float, cond) -> float | None:
    """Time of the second consecutive reading meeting cond(signed conviction, hours in, entry conviction)."""
    prev = False
    for ts, conv in readings:
        ok = cond(conv * s, ts, entry_conv)
        if ok and prev:
            return ts
        prev = ok
    return None


def rules(opened: float):
    h = lambda ts: (ts - opened) / 3600
    return {
        "against >= 0.30": lambda c, ts, e: c <= -0.30,
        "against >= 0.15": lambda c, ts, e: c <= -0.15,
        "turned against (< 0)": lambda c, ts, e: c < 0,
        "faded < 0.10 after 2h": lambda c, ts, e: h(ts) >= 2 and c < 0.10,
        "faded < half of entry after 2h": lambda c, ts, e: h(ts) >= 2 and c < 0.5 * e,
        "faded < 0.10 after 4h": lambda c, ts, e: h(ts) >= 4 and c < 0.10,
    }


def replay(t: dict, bars: list[list], exit_at: float | None) -> float | None:
    """Protect from the original stop; if exit_at comes first, close at the next candle close."""
    sim = {k: t.get(k) for k in ("id", "symbol", "direction", "take_profit_1", "take_profit_2", "timeframe", "liquidation_price")}
    sim["entry_price"] = t.get("initial_entry_price") or t["entry_price"]
    sim["stop_loss"] = t.get("original_stop_loss") or t["stop_loss"]
    sim["quantity"] = t.get("initial_quantity") or t["quantity"]
    exits.stamp(sim, BASE.name, now=t["opened_ts"])
    r_unit = abs(sim["entry_price"] - sim["stop_loss"])
    s = 1 if t["direction"] == "long" else -1
    for b in bars:
        if b[0] / 1000 < t["opened_ts"]:
            continue
        end = b[6] / 1000
        hit = exits.apply_bar(sim, float(b[1]), float(b[2]), float(b[3]), float(b[4]), end)
        if hit:
            return (hit[1] - sim["entry_price"]) * s / r_unit
        if exit_at is not None and end >= exit_at:
            return (float(b[4]) - sim["entry_price"]) * s / r_unit
    return None


async def main() -> dict:
    from trading import guards
    trades = []
    for book, w in guards._wallets().items():
        if book.startswith("main"):
            continue
        trades += [{**t, "book": book} for t in w.all_trades()["closed"] if t.get("risk_usd") and t.get("opened_ts")]
    trades.sort(key=lambda t: t["opened_ts"])
    rows, skipped = [], 0
    async with httpx.AsyncClient(timeout=15) as client:
        for t in trades:
            reads = _readings(t["symbol"], t["opened_ts"], t["opened_ts"] + 24 * 3600)
            if len(reads) < MIN_READINGS:
                skipped += 1
                continue
            bars = await bars_for(client, t["symbol"], int(t["opened_ts"] * 1000) - 300_000)
            if not bars:
                continue
            s = 1 if t["direction"] == "long" else -1
            entry_conv = abs(t.get("conviction_at_entry") or 0.4)
            base = replay(t, bars, None)
            if base is None:
                continue
            row = {"symbol": t["symbol"], "direction": t["direction"], "opened": t["opened_ts"], "protect": base}
            for name, cond in rules(t["opened_ts"]).items():
                r = replay(t, bars, _first(reads, s, entry_conv, cond))
                row[name] = base if r is None else r
            rows.append(row)
    names = ["protect", *rules(0)]
    dec: list[dict] = []
    for r in rows:
        g = next((d for d in reversed(dec[-40:]) if d["symbol"] == r["symbol"] and d["direction"] == r["direction"]
                  and r["opened"] - d["opened"] <= 2 * 3600), None)
        if g:
            g["n"] += 1
            for k in names:
                g[k] += (r[k] - g[k]) / g["n"]
        else:
            dec.append({**r, "n": 1})
    rng, h, res = random.Random(8), len(dec) // 2, {}
    for k in names:
        diffs = [d[k] - d["protect"] for d in dec]
        boot = sorted(st.mean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
        res[k] = {"avg_r": round(st.mean(d[k] for d in dec), 3), "changed": sum(abs(x) > 1e-9 for x in diffs),
                  "vs_protect": round(st.mean(diffs), 3), "older": round(st.mean(diffs[:h]), 3),
                  "newer": round(st.mean(diffs[h:]), 3), "ci90": [round(boot[100], 3), round(boot[1899], 3)]}
    out = {"trades": len(rows), "skipped_no_readings": skipped, "decisions": len(dec), "rules": res}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = asyncio.run(main())
    print(f"{r['trades']} trades ({r['skipped_no_readings']} skipped: coin not scanned while open) → {r['decisions']} decisions")
    for k, v in r["rules"].items():
        print(f"  {k:32s} avg {v['avg_r']:+.3f}R changed {v['changed']:3d} | vs protect {v['vs_protect']:+.3f} "
              f"(older {v['older']:+.3f}, newer {v['newer']:+.3f}) ci90 {v['ci90']}")
