"""One-hour continuation — the scanner's one input with a positive coefficient: does a coin's last hour carry on
for the next four, by enough to pay for the trade?

Why (2026-11-04, research/signal_scores.py on 145,000 labelled readings over 37 days): ret_1h had IC +0.12 (t 6.2)
against the 4-hour forward move, while conviction, composite and the TA score were all negative (t ≈ −5).

Run: venv/bin/python -m research.continuation_lab   (signals.db scan_features × scan_labels;
writes research_cache/continuation_results.json)

  slot      the first sweep in each 4-hour UTC slot (positions do not overlap)
  rules     "top_bottom": long the TOP_K coins by ret_1h, short the bottom TOP_K, 4 hours
            "top_long": the longs only;  "bottom_short": the shorts only
            each also "vs BTC" (xs_4h: the coin's move minus BTC's) — the market-neutral reading
  deciles   ret_1h decile → average forward 4h move, all readings (the raw shape of the signal)
Costs 16 bps a round trip. Halves by time; the second half judges with a bootstrap of per-slot sums (95% interval).
Verdict: a rule passes only if its second-half interval is above zero after costs.
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import defaultdict

from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "continuation_results.json")
COST_PCT = 0.16
TOP_K = 3
SLOT_S = 4 * 3600
MIN_COINS = 12


def readings() -> list[dict]:
    from storage import signals as signal_log
    out = []
    for r in signal_log.labelled_scans(0):
        f = r.get("features") or {}
        if r.get("fwd_4h") is None or f.get("ret_1h") is None:
            continue
        out.append({"ts": float(r["ts"]), "symbol": r["symbol"], "ret_1h": float(f["ret_1h"]), "fwd_4h": float(r["fwd_4h"]),
                    "xs_4h": float(r["xs_4h"]) if r.get("xs_4h") is not None else None})
    return out


def slots(rows: list[dict]) -> list[list[dict]]:
    """The first sweep (readings within 5 minutes of its first reading) in each 4-hour slot, with one row per coin."""
    by_slot: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_slot[int(r["ts"] // SLOT_S)].append(r)
    out = []
    for k in sorted(by_slot):
        v = sorted(by_slot[k], key=lambda r: r["ts"])
        t0 = v[0]["ts"]
        sweep: dict[str, dict] = {}
        for r in v:
            if r["ts"] - t0 > 300:
                break
            sweep.setdefault(r["symbol"], r)
        if len(sweep) >= MIN_COINS:
            out.append(list(sweep.values()))
    return out


def legs(sweep: list[dict], rule: str, target: str) -> list[float]:
    ranked = sorted((r for r in sweep if r.get(target) is not None), key=lambda r: r["ret_1h"])
    longs = ranked[-TOP_K:] if rule in ("top_bottom", "top_long") else []
    shorts = ranked[:TOP_K] if rule in ("top_bottom", "bottom_short") else []
    return [r[target] - COST_PCT for r in longs] + [-r[target] - COST_PCT for r in shorts]


def bootstrap(vals: list[float], n: int = 2000, seed: int = 7) -> tuple[float, float]:
    if len(vals) < 5:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    sums = sorted(sum(rng.choice(vals) for _ in vals) for _ in range(n))
    return sums[int(0.025 * n)], sums[int(0.975 * n)]


def evaluate(sw: list[list[dict]], rule: str, target: str) -> dict:
    per_slot = [sum(legs(s, rule, target)) for s in sw]
    all_legs = [x for s in sw for x in legs(s, rule, target)]
    if not all_legs:
        return {"n": 0}
    lo, hi = bootstrap(per_slot)
    return {"slots": len(sw), "legs": len(all_legs), "avg_leg_pct": round(sum(all_legs) / len(all_legs), 3),
            "hit_pct": round(sum(1 for x in all_legs if x > 0) / len(all_legs) * 100, 1),
            "sum_pct": round(sum(per_slot), 2), "ci95_sum": [round(lo, 2), round(hi, 2)]}


def deciles(rows: list[dict]) -> list[dict]:
    rs = sorted(rows, key=lambda r: r["ret_1h"])
    n = len(rs)
    out = []
    for d in range(10):
        chunk = rs[d * n // 10:(d + 1) * n // 10]
        if chunk:
            out.append({"decile": d + 1, "n": len(chunk), "ret_1h_from": round(chunk[0]["ret_1h"], 2), "ret_1h_to": round(chunk[-1]["ret_1h"], 2),
                        "fwd_4h_bps": round(sum(r["fwd_4h"] for r in chunk) / len(chunk) * 100, 1),
                        "xs_4h_bps": round(sum(r["xs_4h"] or 0 for r in chunk) / len(chunk) * 100, 1)})
    return out


def run() -> dict:
    rows = readings()
    if not rows:
        return {"n": 0, "verdict": "no labelled readings"}
    sw = slots(rows)
    mid = len(sw) // 2
    halves = {"first": sw[:mid], "second": sw[mid:]}
    out: dict = {"readings": len(rows), "slots": len(sw), "period": [rows[0]["ts"], rows[-1]["ts"]], "cost_pct": COST_PCT, "top_k": TOP_K,
                 "deciles": deciles(rows), "rules": {}}
    for rule in ("top_bottom", "top_long", "bottom_short"):
        for target, label in (("fwd_4h", "raw"), ("xs_4h", "vs_btc")):
            out["rules"][f"{rule}_{label}"] = {"all": evaluate(sw, rule, target), **{k: evaluate(v, rule, target) for k, v in halves.items()}}
    passing = [k for k, v in out["rules"].items() if (v["second"].get("ci95_sum") or [float("nan")])[0] > 0]
    out["passing"] = passing
    out["verdict"] = ("passes: " + ", ".join(passing)) if passing else "rejected — no rule's second-half 95% interval is above zero after costs"
    out["generated_at"] = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=1, default=float)
    return out


if __name__ == "__main__":
    o = run()
    print(f"{o.get('readings')} readings, {o.get('slots')} 4h slots; verdict: {o.get('verdict')}")
    for d in o.get("deciles") or []:
        print(f"  decile {d['decile']:2} ret_1h {d['ret_1h_from']:+6.2f}…{d['ret_1h_to']:+6.2f}%  fwd_4h {d['fwd_4h_bps']:+7.1f} bps  vs BTC {d['xs_4h_bps']:+7.1f} bps  n={d['n']}")
    for k, v in (o.get("rules") or {}).items():
        a, s = v["all"], v["second"]
        print(f"  {k:20} all legs={a.get('legs',0):4} avg={a.get('avg_leg_pct',0):+.3f}% hit={a.get('hit_pct',0)}% sum={a.get('sum_pct')} | second half legs={s.get('legs',0):4} avg={s.get('avg_leg_pct',0):+.3f}% sum={s.get('sum_pct')} ci={s.get('ci95_sum')}")
