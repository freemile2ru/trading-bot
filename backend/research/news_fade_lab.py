"""Fade the headline — do coins in the news underperform over the next day, by enough to pay for the trade?

Why (2026-11-04, the news reaction log): over 3,300 classified items the called direction was right 47–52% of the
time at every horizon, and the coin's raw 24-hour move after an item averaged −0.25%, worse for product upgrades
(−0.45%), partnerships (−0.44%) and price commentary (−0.64%) — above the 16 bps round trip, if it holds up.

Run: venv/bin/python -m research.news_fade_lab   (news.db reactions; writes research_cache/news_fade_results.json)

  event     a classified item about a coin we trade (scope "coin"), one trade per coin per UTC day (the first item)
  rules     "short_all": short the coin for HOLD after any item
            "fade_call": the opposite of Claude's called direction when |direction| ≥ 0.3
            "fade_bullish": short only after bullish items (direction ≥ 0.3)
            "follow_call": the control — trade the called direction
Costs 16 bps a round trip. Halves by time; the second half judges with a bootstrap of daily sums (95% interval).
Verdict: a rule passes only if its second-half interval is above zero after costs.
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import defaultdict

from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "news_fade_results.json")
COST_PCT = 0.16
HORIZONS = ("r_4h", "r_24h")
MIN_DIR = 0.3


def events() -> list[dict]:
    from news import store
    rows = [r for r in store.reactions() if r.get("r_24h") is not None and (r.get("scope") or "coin") == "coin"]
    rows.sort(key=lambda r: r.get("seen") or 0)
    first: dict[tuple[str, int], dict] = {}
    for r in rows:
        key = (r["symbol"], int((r.get("seen") or 0) // 86400))
        first.setdefault(key, r)                                      # one trade a coin a day: the first item
    return list(first.values())


def signed(rule: str, r: dict, h: str) -> float | None:
    d = float(r.get("direction") or 0.0)
    move = float(r[h])
    if rule == "short_all":
        return -move
    if rule == "fade_call":
        return -move * (1 if d >= MIN_DIR else -1) if abs(d) >= MIN_DIR else None
    if rule == "fade_bullish":
        return -move if d >= MIN_DIR else None
    if rule == "follow_call":
        return move * (1 if d >= MIN_DIR else -1) if abs(d) >= MIN_DIR else None
    return None


def bootstrap_daily(daily: dict[int, float], n: int = 2000, seed: int = 7) -> tuple[float, float]:
    vals = list(daily.values())
    if len(vals) < 5:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    sums = sorted(sum(rng.choice(vals) for _ in vals) for _ in range(n))
    return sums[int(0.025 * n)], sums[int(0.975 * n)]


def evaluate(rows: list[dict], rule: str, h: str) -> dict:
    per = [(r, signed(rule, r, h)) for r in rows]
    per = [(r, s - COST_PCT) for r, s in per if s is not None]
    if not per:
        return {"n": 0}
    daily: dict[int, float] = defaultdict(float)
    for r, s in per:
        daily[int((r.get("seen") or 0) // 86400)] += s
    lo, hi = bootstrap_daily(daily)
    return {"n": len(per), "avg_pct": round(sum(s for _, s in per) / len(per), 3),
            "hit_pct": round(sum(1 for _, s in per if s > 0) / len(per) * 100, 1),
            "days": len(daily), "sum_pct": round(sum(daily.values()), 2), "ci95_sum": [round(lo, 2), round(hi, 2)]}


def run() -> dict:
    rows = events()
    if not rows:
        return {"n": 0, "verdict": "no reactions logged"}
    mid = rows[len(rows) // 2]["seen"]
    halves = {"first": [r for r in rows if r["seen"] < mid], "second": [r for r in rows if r["seen"] >= mid]}
    out: dict = {"n_items": len(rows), "period": [rows[0]["seen"], rows[-1]["seen"]], "cost_pct": COST_PCT, "rules": {}, "by_event_type": {}}
    for rule in ("short_all", "fade_call", "fade_bullish", "follow_call"):
        out["rules"][rule] = {h: {"all": evaluate(rows, rule, h), **{k: evaluate(v, rule, h) for k, v in halves.items()}} for h in HORIZONS}
    types = defaultdict(list)
    for r in rows:
        types[r.get("event_type") or "?"].append(r)
    for t, v in sorted(types.items(), key=lambda kv: -len(kv[1])):
        if len(v) >= 30:
            out["by_event_type"][t] = {"n": len(v), "short_all_24h": evaluate(v, "short_all", "r_24h"),
                                       "short_all_24h_second_half": evaluate([r for r in v if r["seen"] >= mid], "short_all", "r_24h")}
    passing = [(rule, h) for rule in out["rules"] for h in HORIZONS
               if (out["rules"][rule][h]["second"].get("ci95_sum") or [float("nan")])[0] > 0]
    out["passing"] = [f"{r} @ {h}" for r, h in passing]
    out["verdict"] = ("passes: " + ", ".join(out["passing"])) if passing else "rejected — no rule's second-half 95% interval is above zero after costs"
    out["generated_at"] = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=1, default=float)
    return out


if __name__ == "__main__":
    o = run()
    print(f"{o.get('n_items')} coin-day items; verdict: {o.get('verdict')}")
    for rule, hs in (o.get("rules") or {}).items():
        for h, parts in hs.items():
            a, s = parts["all"], parts["second"]
            print(f"  {rule:12} {h:5} all n={a.get('n',0):4} avg={a.get('avg_pct',0):+.3f}% hit={a.get('hit_pct',0)}%  | second half n={s.get('n',0):4} avg={s.get('avg_pct',0):+.3f}% sum={s.get('sum_pct')} ci={s.get('ci95_sum')}")
    for t, v in (o.get("by_event_type") or {}).items():
        print(f"  type {t:24} n={v['n']:4} short_all 24h avg={v['short_all_24h'].get('avg_pct')}% hit={v['short_all_24h'].get('hit_pct')}%  second-half ci={v['short_all_24h_second_half'].get('ci95_sum')}")
