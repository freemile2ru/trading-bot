"""Auction-level lab — the mentor's NQ thinkScript system (trading/auction_levels.py) replayed on Binance's stock and index
perps, 5-minute bars, every closed bar since each one listed (2026-01 … 2026-04 onward).

  symbols   QQQUSDT (the Nasdaq-100 proxy the study was written for), SPYUSDT, NVDAUSDT, TSLAUSDT
  costs     taker in and out at 8 bps a side (the paper wallet's fee + slippage)
  reported  per symbol: trades, win rate, average net % and R, both halves, by grade (0–8), by level, by exit, by side

Pre-registered rule (2026-10-31, before the first run): the system earns a paper book only if, on QQQ, its net average
per trade is positive in BOTH halves at every grade ≥ 6, with at least 60 trades; the book then trades grade ≥ 6 only.
If only one half is positive it goes on the shelf as "mixed"; if neither, "rejected".

    ./venv/bin/python -m research.auction_lab
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pandas as pd

from trading import auction_levels as AL

CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache")
RESULTS = os.path.join(CACHE, "auction_results.json")
SYMBOLS = ("QQQUSDT", "SPYUSDT", "NVDAUSDT", "TSLAUSDT")
COST_BPS = 8.0                                   # the paper wallet's taker fee + slippage, a side — what a Binance perp book would pay
COSTS = (0.0, 0.5, 2.0, 8.0)                    # a side: gross; CME micro futures (~0.5 bp); a cheap perp venue; Binance perps
MIN_TRADES, PROMOTE_GRADE = 60, 6
# Variants built around the replay's weaknesses (targets inside the costs, breakeven gross): a minimum target distance,
# continuation-only setups, a minimum reward-to-risk, a higher grade. Pre-registered (2026-10-31, before the first
# variant run): a variant is chosen only if, at Binance costs on QQQ, NVDA and TSLA pooled, it is positive in BOTH halves
# with ≥ 60 trades; ties go to the simpler variant (fewer filters).
VARIANTS = {
    "study": dict(min_grade=0),
    "grade7": dict(min_grade=7),
    "target25": dict(min_target_bps=25),
    "target50": dict(min_target_bps=50),
    "rr1.5": dict(min_rr=1.5),
    "cont": dict(cont_only=True),
    "target50+grade7": dict(min_target_bps=50, min_grade=7),
    "target50+rr1.5": dict(min_target_bps=50, min_rr=1.5),
    "target50+cont+grade7": dict(min_target_bps=50, cont_only=True, min_grade=7),
    # The mentor's reading (2026-11-01): the best entry is the retest after confirmation; the best exit lets a runner run.
    "retest": dict(entry="retest"),
    "second_target": dict(target_mode="second"),
    "trail": dict(trail=True),
    "retest+second": dict(entry="retest", target_mode="second"),
    "retest+trail": dict(entry="retest", trail=True),
    "retest+second+trail": dict(entry="retest", target_mode="second", trail=True),
    "retest+second+trail+grade7": dict(entry="retest", target_mode="second", trail=True, min_grade=7),
    "second+trail+target50": dict(target_mode="second", trail=True, min_target_bps=50),
}
LOW_COST_BPS = 0.5                              # CME micro futures, the instrument the study was written for


async def bars(symbol: str, days: int = 400) -> pd.DataFrame:
    """5-minute bars, cached for a day."""
    path = os.path.join(CACHE, f"auction_{symbol}_5m.pkl")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
        return pd.read_pickle(path)
    from data.derivatives_client import fetch_klines_range
    now = int(time.time() * 1000)
    rows = await fetch_klines_range(symbol, "5m", now - days * 86400 * 1000, now)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "quote"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df.to_pickle(path)
    return df


def run_symbol(symbol: str, df: pd.DataFrame) -> dict:
    trades = AL.simulate(df, symbol)
    rep = AL.summarize(trades, COST_BPS)
    rep["bars"] = int(len(df))
    rep["from"] = str(df.index[0].date()) if len(df) else None
    high = [t for t in trades if t.grade >= PROMOTE_GRADE]
    rep["high_grade"] = AL.summarize(high, COST_BPS)
    # The same trades at other cost levels: the study was written for NQ futures, where a round trip costs ~1 bp.
    rep["by_cost"] = {str(c): {"all": AL.summarize(trades, c).get("all"), "high_grade": AL.summarize(high, c).get("all"),
                               "high_grade_halves": [AL.summarize(high, c).get("first_half"), AL.summarize(high, c).get("second_half")]} for c in COSTS}
    if trades:
        rows = pd.DataFrame([t.row() for t in trades])
        rows["target_bps"] = (rows["target"] / rows["entry"] - 1).abs() * 1e4
        rows["risk_bps"] = rows["risk_pct"] * 100
        rep["geometry"] = {"median_target_bps": round(float(rows["target_bps"].median()), 1), "median_risk_bps": round(float(rows["risk_bps"].median()), 1),
                           "trades_per_day": round(len(rows) / max((df.index[-1] - df.index[0]).days, 1), 2)}
    return rep


def verdict(reports: dict) -> str:
    q = (reports.get("QQQUSDT") or {}).get("high_grade") or {}
    if (q.get("trades") or 0) < MIN_TRADES:
        return f"not enough grade ≥{PROMOTE_GRADE} trades on QQQ ({q.get('trades', 0)} of {MIN_TRADES}) — pending"
    f, s = (q.get("first_half") or {}).get("avg_net_pct"), (q.get("second_half") or {}).get("avg_net_pct")
    if f is not None and s is not None and f > 0 and s > 0:
        return f"promote — grade ≥{PROMOTE_GRADE} on QQQ nets {q['all']['avg_net_pct']:+.3f}% a trade, both halves positive ({f:+.3f} / {s:+.3f})"
    if (f or 0) > 0 or (s or 0) > 0:
        return f"mixed — grade ≥{PROMOTE_GRADE} on QQQ: halves {f:+.3f} / {s:+.3f}% a trade"
    return f"rejected — grade ≥{PROMOTE_GRADE} on QQQ loses in both halves ({f:+.3f} / {s:+.3f}% a trade after costs)"


def run_variants(frames: dict[str, pd.DataFrame]) -> dict:
    """Every variant on QQQ, NVDA and TSLA pooled, at Binance costs, both halves."""
    out = {}
    for name, kw in VARIANTS.items():
        trades = []
        for sym in ("QQQUSDT", "NVDAUSDT", "TSLAUSDT"):
            if sym in frames:
                trades += AL.simulate(frames[sym], sym, **kw)
        trades.sort(key=lambda t: t.entry_ts)
        rep = AL.summarize(trades, COST_BPS)
        low = AL.summarize(trades, LOW_COST_BPS)
        out[name] = {"params": kw, "trades": rep.get("trades", 0), "all": rep.get("all"), "first_half": rep.get("first_half"),
                     "second_half": rep.get("second_half"), "by_exit": rep.get("by_exit"),
                     "low_cost": {"all": low.get("all"), "first_half": low.get("first_half"), "second_half": low.get("second_half")}}
    return out


def choose(variants: dict) -> tuple[str | None, str]:
    ok = {n: v for n, v in variants.items() if (v.get("trades") or 0) >= MIN_TRADES
          and (v.get("first_half") or {}).get("avg_net_pct", 0) > 0 and (v.get("second_half") or {}).get("avg_net_pct", 0) > 0}
    if not ok:
        return None, "no variant is positive in both halves at Binance costs with 60+ trades — the book runs the best-scoring variant at measuring size, unproven"
    best = max(ok, key=lambda n: (ok[n]["all"]["avg_net_pct"], -len(ok[n]["params"])))
    return best, f"adopt '{best}': {ok[best]['all']['avg_net_pct']:+.3f}% a trade, halves {ok[best]['first_half']['avg_net_pct']:+.3f} / {ok[best]['second_half']['avg_net_pct']:+.3f}"


async def main() -> dict:
    reports, frames = {}, {}
    for sym in SYMBOLS:
        try:
            df = await bars(sym)
        except Exception as e:
            reports[sym] = {"error": str(e)}
            continue
        frames[sym] = df
        reports[sym] = run_symbol(sym, df)
        print(f"  {sym}: {reports[sym].get('trades')} trades from {reports[sym].get('from')}")
    variants = run_variants(frames)
    chosen, why = choose(variants)
    out = {"symbols": reports, "cost_bps": COST_BPS, "rule": __doc__.split("Pre-registered rule")[1].split("\n\n")[0].strip(),
           "verdict": verdict(reports), "variants": variants, "chosen": chosen, "variant_verdict": why, "ran_at": time.time()}
    json.dump(out, open(RESULTS, "w"), indent=1, default=str)
    return out


def _line(name: str, b: dict) -> str:
    return f"{name:22} n={b.get('n', 0):4}  win {b.get('win_pct') or 0:5.1f}%  avg {b.get('avg_net_pct') or 0:+.3f}%  avgR {b.get('avg_r') or 0:+.2f}  sum {b.get('sum_net_pct') or 0:+.1f}%"


if __name__ == "__main__":
    rep = asyncio.run(main())
    for sym, r in rep["symbols"].items():
        if "error" in r or not r.get("trades"):
            print(f"\n== {sym}: {r.get('error') or 'no trades'}")
            continue
        print(f"\n== {sym}: {r['trades']} trades on {r['bars']} bars from {r['from']} (cost {COST_BPS:.0f} bps a side)")
        print(_line("all", r["all"])); print(_line("first half", r["first_half"])); print(_line("second half", r["second_half"]))
        for g in sorted(r["by_grade"]):
            print(_line(f"grade {g}", r["by_grade"][g]))
        for name, b in sorted(r["by_level"].items()):
            print(_line(f"level {name}", b))
        for name, b in sorted(r["by_exit"].items()):
            print(_line(f"exit {name}", b))
        for name, b in sorted(r["by_direction"].items()):
            print(_line(f"side {name}", b))
        g = r.get("geometry") or {}
        print(f"geometry: median target {g.get('median_target_bps')} bps away, median risk {g.get('median_risk_bps')} bps, {g.get('trades_per_day')} trades a day")
        for c, b in r["by_cost"].items():
            h = b["high_grade_halves"]
            print(_line(f"cost {float(c):.1f} bps/side all", b["all"]) + f"   | grade ≥{PROMOTE_GRADE}: " + _line("", b["high_grade"]).strip() +
                  f"  halves {(h[0] or {}).get('avg_net_pct')} / {(h[1] or {}).get('avg_net_pct')}")
        hg = r["high_grade"]
        if hg.get("trades"):
            print(_line(f"grade ≥{PROMOTE_GRADE} all", hg["all"])); print(_line(f"grade ≥{PROMOTE_GRADE} 1st", hg["first_half"])); print(_line(f"grade ≥{PROMOTE_GRADE} 2nd", hg["second_half"]))
    print("\n== variants (QQQ + NVDA + TSLA pooled) — Binance costs 8 bps/side | futures costs 0.5 bp/side")
    for name, v in rep["variants"].items():
        if v["trades"]:
            lc = v["low_cost"]
            print(_line(name, v["all"]) + f"   halves {v['first_half']['avg_net_pct']:+.3f} / {v['second_half']['avg_net_pct']:+.3f}"
                  f"   | futures: avg {lc['all']['avg_net_pct']:+.3f}% win {lc['all']['win_pct']}% halves {lc['first_half']['avg_net_pct']:+.3f} / {lc['second_half']['avg_net_pct']:+.3f}")
        else:
            print(f"{name:22} no trades")
    print(f"\nVerdict: {rep['verdict']}\nVariants: {rep['variant_verdict']}\nWritten to {RESULTS}")
