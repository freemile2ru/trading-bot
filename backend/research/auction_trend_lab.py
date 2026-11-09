"""The mentor's system on trend days — the five forward arrangements of experiments/auction replayed over the cached
5-minute bars of the four Mentor names (research_cache/auction_<SYM>_5m.pkl, written by research/auction_lab.py).

Why (2026-11-09): a gap-and-go session (AMD +4%, TSLA +5%, NVDA +3% by mid-morning) on which the live book took
nothing — QQQ confirmed twice and never retested, the other three were refused by the 2%-from-the-open chase gate. Two
candidate arrangements were written before this replay: "chase_gate" (the base arrangement plus the live gate, so
base − chase_gate is the gate's cost) and "trend_day" (trading/auction_levels.simulate_trend_day: beyond the initial
balance by half its range with the study's master and 5m bias aligned, enter the first EMA20 pullback that holds, stop
under it ≥ 1 ATR, target IB + 2× range, out on a VWAP loss, one trade a symbol a day).

Run: venv/bin/python -m research.auction_trend_lab   (writes research_cache/auction_trend_results.json)

  gates    the live book's: levels it does not trade (IBH/IBL/WKH/WKL as acceptance levels), entries before 14:00 ET
  costs    8 bps a side (the forward shadows' rate), fills at the bar's close
  judging  each arrangement's trades split in half by time; both halves must clear zero for the arrangement to go forward
"""
from __future__ import annotations

import json
import os
import pickle

import pandas as pd

from experiments import auction
from trading import auction_levels as AL

CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache")
OUT = os.path.join(CACHE, "auction_trend_results.json")
NAMES = ("QQQUSDT", "NVDAUSDT", "TSLAUSDT", "AMDUSDT")
ARRANGEMENTS = {**{k: {x: y for x, y in v.items() if x != "since"} for k, v in auction.VARIANTS_FORWARD.items()},
                "trend_day": {"entry": "trend_day", "min_grade": 0, "min_target_bps": auction.VARIANT["min_target_bps"], "target_mode": "ib2"}}


def load(sym: str) -> pd.DataFrame:
    with open(os.path.join(CACHE, f"auction_{sym}_5m.pkl"), "rb") as f:
        raw = pickle.load(f)
    df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume", "quote"][:len(raw[0])])
    if "ts" in df.columns:
        df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def trades(df: pd.DataFrame, sym: str, name: str, v: dict) -> list[dict]:
    if v.get("entry") == "trend_day":
        ts = AL.simulate_trend_day(df, sym, min_target_bps=v["min_target_bps"])
    else:
        ts = AL.simulate(df, sym, min_grade=v["min_grade"], min_target_bps=v["min_target_bps"], entry=v.get("entry", "next_open"),
                         target_mode=v.get("target_mode", "next"), max_from_open_pct=v.get("max_from_open_pct"))
    out = []
    for t in ts:
        if t.exit is None or t.level_name in auction.SKIP_LEVELS:
            continue
        et = t.entry_ts.tz_convert(auction.ET)
        minute = et.hour * 60 + et.minute
        if minute >= auction._m(auction.ENTRY_CUTOFF):
            continue
        out.append({"arrangement": name, "symbol": sym, "day": et.strftime("%Y-%m-%d"), "direction": t.direction, "level": t.level_name, "grade": t.grade,
                    "entry_ts": t.entry_ts.timestamp(), "exit_reason": t.exit_reason, "net_pct": t.pnl_pct - 2 * auction.VARIANT_COST_BPS / 100, "r": t.r})
    return out


def block(x: pd.DataFrame) -> dict:
    return {"n": int(len(x)), "won_pct": round(float(100 * (x.net_pct > 0).mean()), 1) if len(x) else None,
            "net_per_trade_pct": round(float(x.net_pct.mean()), 4) if len(x) else None, "sum_net_pct": round(float(x.net_pct.sum()), 2) if len(x) else None,
            "avg_r": round(float(x.r.mean()), 3) if len(x) and x.r.notna().any() else None}


def main() -> dict:
    rows = []
    for sym in NAMES:
        df = load(sym)
        for name, v in ARRANGEMENTS.items():
            rows += trades(df, sym, name, v)
    out = pd.DataFrame(rows)
    res = {"names": list(NAMES), "sessions": int(out.day.nunique()), "period": [str(out.day.min()), str(out.day.max())], "cost_bps": auction.VARIANT_COST_BPS,
           "arrangements": {}}
    for name, g in out.groupby("arrangement"):
        g = g.sort_values("entry_ts")
        half = len(g) // 2
        res["arrangements"][name] = {"rule": ARRANGEMENTS[name], **block(g), "first_half": block(g.iloc[:half]), "second_half": block(g.iloc[half:]),
                                     "by_exit": {k: block(x) for k, x in g.groupby("exit_reason")}, "by_symbol": {k: block(x) for k, x in g.groupby("symbol")}}
    td, base, gate = (res["arrangements"].get(k, {}) for k in ("trend_day", "base", "chase_gate"))
    res["verdict"] = (f"trend_day rejected: {td.get('n')} trades, {td.get('won_pct')}% won, {td.get('net_per_trade_pct'):+.3f}% a trade, both halves "
                      f"({td['first_half'].get('net_per_trade_pct'):+.3f}% / {td['second_half'].get('net_per_trade_pct'):+.3f}%) below zero — the EMA20 pullback is "
                      f"stopped {td['by_exit'].get('stop', {}).get('n', 0)} times of {td.get('n')} and the far extension is reached "
                      f"{td['by_exit'].get('target', {}).get('n', 0)} times. chase_gate vs base: {gate.get('net_per_trade_pct'):+.3f}% vs {base.get('net_per_trade_pct'):+.3f}% a trade "
                      f"on {gate.get('n')} vs {base.get('n')} trades — the gate removes net losers; it stays, and goes forward as a shadow.")
    with open(OUT, "w") as f:
        json.dump(res, f, indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    for k, v in r["arrangements"].items():
        print(f"{k:10s} n={v['n']:3d} won={v['won_pct']}% net/trade={v['net_per_trade_pct']:+.3f}% halves {v['first_half']['net_per_trade_pct']:+.3f} / {v['second_half']['net_per_trade_pct']:+.3f}")
    print(r["verdict"])
