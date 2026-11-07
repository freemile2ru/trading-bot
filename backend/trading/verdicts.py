"""Pass/fail rules for every book, fixed in advance — registered 2026-10-12, before the results they judge.

Why: deciding to keep or cut a book by eyeballing its record invites cutting on a bad week and keeping
on a lucky one. These rules are set once and applied the same way to every book:

  trade books   the average R of its closed trades (paper and live ledgers together, gated/declined
                shadows excluded), with a 90% confidence range from a block bootstrap (blocks of 5
                trades — our trades come in streaks, so resampling them one by one would overstate
                the certainty)
                  fewer than TARGET trades     collecting — no verdict yet (the provisional range is shown)
                  range entirely above 0       pass
                  range entirely below 0       fail — retire
                  straddles 0, < MAX trades    unclear — keep collecting
                  straddles 0, ≥ MAX trades    no edge — retire (it isn't earning its keep)
  carry         funding income per day on notional against the holdout's (BTC 1.0, ETH 0.57 bps/day),
                judged after CARRY_DAYS: pass at ≥ half the expected rate, fail at ≤ 0, otherwise keep
                going to twice CARRY_DAYS and then pass only if positive.
  main          manual trading isn't a strategy — no verdict.

The verdict is advice: nothing is switched off automatically. A "retire" shows on the Books tab.
"""
from __future__ import annotations

import os
import random

REGISTERED = "2026-10-12"
TARGET = int(os.getenv("VERDICT_TRADES", "40"))
MAX = int(os.getenv("VERDICT_MAX_TRADES", "120"))
LEVEL = 0.90
BLOCK = 5
RESAMPLES = 2000
CARRY_DAYS = 60
CARRY_EXPECTED_BPS_DAY = {"BTCUSDT": 1.015, "ETHUSDT": 0.571,      # research/carry_lab.py holdout
                          "XAUUSDT": 1.9}                            # gold: ~7%/yr, its last 3 months (tradfi_perp_lab)
NO_VERDICT = {"main"}


def rule_since(book: str) -> float:
    """When a book's current rule began (0 when it never changed): trades opened before it stay on its record but
    not in its verdict — the verdict judges the rule that runs, not the one it replaced."""
    if book == "cascade":
        from experiments import cascade
        return cascade.RULE_SINCE
    if book.startswith("copy_"):                  # the base books and the variants alike: the caps and the sizing apply to every copy
        from experiments import copy_trading
        return copy_trading.RULE_SINCE
    return 0.0


def bootstrap_ci(rs: list[float], seed: int = 7) -> tuple[float, float] | None:
    """90% range of the mean, resampling runs of BLOCK consecutive trades (circular)."""
    n = len(rs)
    if n < 2:
        return None
    rng, block = random.Random(seed + n), min(BLOCK, n)
    means = []
    for _ in range(RESAMPLES):
        draw = []
        while len(draw) < n:
            i = rng.randrange(n)
            draw += [rs[(i + k) % n] for k in range(block)]
        means.append(sum(draw[:n]) / n)
    means.sort()
    lo, hi = means[int(RESAMPLES * (1 - LEVEL) / 2)], means[int(RESAMPLES * (1 + LEVEL) / 2) - 1]
    return round(lo, 3), round(hi, 3)


def judge(rs: list[float]) -> dict:
    """A trade book's verdict from its closed trades' R, oldest first."""
    n = len(rs)
    mean = round(sum(rs) / n, 3) if n else None
    ci = bootstrap_ci(rs)
    if n < TARGET:
        status, note = "collecting", f"{n}/{TARGET} trades — no verdict before {TARGET}"
    elif ci[0] > 0:
        status, note = "pass", "the whole 90% range is above zero"
    elif ci[1] < 0:
        status, note = "fail", "the whole 90% range is below zero — retire it"
    elif n >= MAX:
        status, note = "no_edge", f"still no clear edge after {n} trades — retire it"
    else:
        status, note = "unclear", f"the range straddles zero — keep going to {MAX} trades"
    return {"rule": "trades", "status": status, "note": note, "trades": n, "target": TARGET, "max": MAX,
            "avg_r": mean, "ci": ci, "level": LEVEL, "registered": REGISTERED}


def judge_carry(positions: list[dict]) -> dict:
    """Carry's verdict from its positions' funding income against the holdout's rate."""
    import time
    now = time.time()
    got, expected, days = [], [], 0.0
    for p in positions:
        d = ((p.get("closed_ts") or now) - p["opened_ts"]) / 86400
        if d <= 0 or not p.get("notional"):
            continue
        days = max(days, d)
        got.append(p["funding"] / p["notional"] * 1e4 / d)
        expected.append(CARRY_EXPECTED_BPS_DAY.get(p["symbol"], 0.0))
    live = round(sum(got) / len(got), 3) if got else None
    exp = round(sum(expected) / len(expected), 3) if expected else None
    base = {"rule": "carry", "days": round(days, 1), "target_days": CARRY_DAYS, "funding_bps_day": live,
            "expected_bps_day": exp, "registered": REGISTERED}
    if live is None or days < CARRY_DAYS:
        return {**base, "status": "collecting", "note": f"{days:.0f}/{CARRY_DAYS} days — no verdict before {CARRY_DAYS}"}
    if live >= exp / 2:
        return {**base, "status": "pass", "note": "funding is paying at least half the holdout's rate"}
    if live <= 0:
        return {**base, "status": "fail", "note": "funding hasn't paid — retire it"}
    if days >= 2 * CARRY_DAYS:
        return {**base, "status": "pass", "note": "funding pays, though below half the holdout's rate"}
    return {**base, "status": "unclear", "note": f"funding pays but below half the holdout's rate — keep going to {2 * CARRY_DAYS} days"}


def for_books(rows: list[dict]) -> dict[str, dict]:
    """Every book's verdict from overview rows (any ledger mix the caller chose)."""
    rs: dict[str, list[tuple[float, float]]] = {}
    since: dict[str, float] = {}
    for r in rows:
        if r.get("status") == "open" or r.get("r") is None:
            continue
        book = r["system"].removesuffix("_live")
        if book not in since:
            since[book] = rule_since(book)
        if (r.get("opened_ts") or 0) < since[book]:
            continue
        rs.setdefault(book, []).append((r.get("closed_ts") or 0, r["r"]))
    out = {b: judge([x for _, x in sorted(v)]) for b, v in rs.items() if b not in NO_VERDICT}
    try:
        from experiments import carry
        out["carry"] = judge_carry(carry._rows("SELECT * FROM carry"))
    except Exception:
        pass
    return out
