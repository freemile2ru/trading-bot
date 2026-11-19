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
  carry_q       the marked basis income per day on notional against the rate LOCKED at entry (2026-11-17), the same
                clock and bars as carry: pass at ≥ half the locked rate after CARRY_DAYS, fail at ≤ 0.
  main          manual trading isn't a strategy — no verdict.
  sized         (2026-11-19) the R rule strips size out on purpose — it asks whether the signal has an edge. Beside it,
                the same trades in dollars at the plan's own sizing: the worst UTC day and the deepest drawdown of the
                closed record, as a share of the book's planned capital, against the programme's floors (1% a day,
                5% drawdown — the limits the user set for the single system). A book whose R range passes but whose
                drawdown at its own size went past the floor is "oversized": an edge, but not deployable at this size
                — the line says what fraction of the size would have kept it inside. The worst day is shown against
                its floor but does not gate: a day is the risk budget's stop, a drawdown is the book's.

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
SIZED_DAY_PCT = float(os.getenv("VERDICT_SIZED_DAY_PCT", "1"))      # the programme's floors (2026-11-18, the user: "1% a day, 5% drawdown")
SIZED_DD_PCT = float(os.getenv("VERDICT_SIZED_DD_PCT", "5"))


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


def sized(trades: list[tuple[float, float]], plan_usd: float | None) -> dict | None:
    """The record in dollars at the plan's own sizing: `trades` are (closed_ts, net P&L). The worst UTC day and the deepest
    drawdown, as a share of `plan_usd` when the book has a plan; `ok` is whether the drawdown stayed inside SIZED_DD_PCT
    (None without a plan), `fit_x` the fraction of the size that would have kept it there."""
    import time
    if not trades:
        return None
    by_day: dict[str, float] = {}
    cum = peak = dd = 0.0
    for ts, pnl in sorted(trades):
        day = time.strftime("%Y-%m-%d", time.gmtime(ts or 0))
        by_day[day] = by_day.get(day, 0.0) + pnl
        cum += pnl
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    worst_day, worst = min(by_day.items(), key=lambda kv: kv[1])
    out = {"plan_usd": plan_usd, "days": len(by_day), "worst_day": worst_day, "worst_day_usd": round(worst, 2), "max_dd_usd": round(dd, 2),
           "day_floor_pct": SIZED_DAY_PCT, "dd_floor_pct": SIZED_DD_PCT, "worst_day_pct": None, "max_dd_pct": None, "ok": None, "fit_x": None}
    if plan_usd and plan_usd > 0:
        day_pct, dd_pct = round(worst / plan_usd * 100, 2), round(dd / plan_usd * 100, 2)
        out.update(worst_day_pct=day_pct, max_dd_pct=dd_pct, ok=-dd_pct <= SIZED_DD_PCT, day_ok=-day_pct <= SIZED_DAY_PCT,
                   fit_x=round(min(1.0, SIZED_DD_PCT / -dd_pct), 2) if dd_pct < 0 else 1.0)
    return out


def judge(rs: list[float], sized_: dict | None = None) -> dict:
    """A trade book's verdict from its closed trades' R, oldest first; `sized_` (from sized()) can turn a pass into
    "oversized" when the dollar drawdown at the plan's size broke the floor."""
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
    if status == "pass" and sized_ and sized_.get("ok") is False:
        status = "oversized"
        note = (f"an edge in R, but at the plan's ${sized_['plan_usd']:,.0f} the drawdown reached {sized_['max_dd_pct']:+.2f}% "
                f"(floor {SIZED_DD_PCT:g}%) — deployable at {sized_['fit_x']:.2f}× this size, not at it")
    return {"rule": "trades", "status": status, "note": note, "trades": n, "target": TARGET, "max": MAX,
            "avg_r": mean, "ci": ci, "level": LEVEL, "registered": REGISTERED, "sized": sized_}


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


def judge_carry_q(positions: list[dict]) -> dict:
    """Carry Q's verdict (2026-11-17: it had none, so the Books tab said "collecting, 0 trades" for ever): each position's
    marked P&L per day on its notional against the annualised basis it locked at entry, on carry's clock."""
    got, expected, days = [], [], 0.0
    for p in positions:
        d = p.get("days") or 0.0
        if d <= 0 or not p.get("notional"):
            continue
        days = max(days, d)
        got.append((p.get("pnl") or 0.0) / p["notional"] * 1e4 / d)
        expected.append((p.get("basis_locked_pct") or 0.0) / 100 * 1e4 / 365)
    live = round(sum(got) / len(got), 3) if got else None
    exp = round(sum(expected) / len(expected), 3) if expected else None
    base = {"rule": "carry_q", "days": round(days, 1), "target_days": CARRY_DAYS, "basis_bps_day": live,
            "locked_bps_day": exp, "registered": "2026-11-17"}
    if live is None or exp is None or days < CARRY_DAYS:
        return {**base, "status": "collecting", "note": f"{days:.0f}/{CARRY_DAYS} days — no verdict before {CARRY_DAYS}"}
    if live >= exp / 2:
        return {**base, "status": "pass", "note": "the basis is paying at least half the rate locked at entry"}
    if live <= 0:
        return {**base, "status": "fail", "note": "the basis hasn't paid — retire it"}
    if days >= 2 * CARRY_DAYS:
        return {**base, "status": "pass", "note": "the basis pays, though below half the locked rate"}
    return {**base, "status": "unclear", "note": f"the basis pays but below half the locked rate — keep going to {2 * CARRY_DAYS} days"}


def for_books(rows: list[dict]) -> dict[str, dict]:
    """Every book's verdict from overview rows (any ledger mix the caller chose)."""
    rs: dict[str, list[tuple[float, float, float]]] = {}
    since: dict[str, float] = {}
    try:
        from trading import capital
        plans = {**capital.PLAN, **capital.plan()}
    except Exception:
        plans = {}
    for r in rows:
        if r.get("status") == "open" or r.get("r") is None:
            continue
        book = r["system"].removesuffix("_live")
        if book not in since:
            since[book] = rule_since(book)
        if (r.get("opened_ts") or 0) < since[book]:
            continue
        rs.setdefault(book, []).append((r.get("closed_ts") or 0, r["r"], r.get("net_pnl") or 0.0))
    out = {b: judge([x for _, x, _ in sorted(v)], sized([(ts, p) for ts, _, p in v], plans.get(b)))
           for b, v in rs.items() if b not in NO_VERDICT}
    try:
        from experiments import carry
        out["carry"] = judge_carry(carry._rows("SELECT * FROM carry"))
    except Exception:
        pass
    try:
        from experiments import carry_q
        out["carry_q"] = judge_carry_q(carry_q.status()["positions"])
    except Exception:
        pass
    return out
