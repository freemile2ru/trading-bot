"""The daily target tracker: are all the paper books, together, making TARGET a day?

Measured, not hoped: each book's P&L per UTC day comes from its equity curve (deposits taken out), rolled up
into 7- and 30-day averages against the target. Each book also carries an *expected* $/day from the evidence
behind it — funding actually received, basis actually locked, the weekly basket's researched +1.36%/week, the
event study's −20% per listing over 30 days — so the gap between what the books should earn and what they do
earn is visible, and so is which books have no expectation at all (the prediction books).

Circuit breaker: a 7-day average below −TARGET pauses the capital programme (trading/capital.py) and says so.

The pair-ledger books (carry, carry Q, events, fade-24) had no equity curve of their own; `tick` writes one
every 15 minutes into the same table the paper wallets use, so the Portfolio page's curves cover them too.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

from storage import state as kv
from storage.db import SWING_DB
from trading import capital

logger = logging.getLogger("trading.target")

GOAL = capital.GOAL_USD_DAY               # the $200/day ambition — shown beside the target, never scored against
TARGET_FLOOR = float(os.getenv("DAILY_TARGET_FLOOR_USD", "25"))
TRIP_AVG7 = -GOAL                         # 7-day average below this pauses the programme (a loss limit, not the target)


def evidence_target(books: list[dict], verdicts_: dict[str, dict] | None = None) -> tuple[float, str]:
    """Today's target (2026-11-02, re-based with the user after the first full review): what the evidence supports —
    the lab-expected $/day of the books with a measured rate, plus the measured 30-day $/day of any book whose
    40-trade verdict says "edge" — never below TARGET_FLOOR. The ladder (trading/allocator.py) moves capital to the
    books that earn a verdict, which raises their expected rate, which raises the target: it grows on evidence."""
    expected = sum(r.get("expected") or 0.0 for r in books)
    earned = sum(r.get("avg30") or 0.0 for r in books                                   # a book counts once: its lab rate, else its earned rate
                 if not r.get("expected") and (verdicts_ or {}).get(r["book"], {}).get("status") == "edge" and (r.get("avg30") or 0) > 0)
    target = max(TARGET_FLOOR, round(expected + earned, 2))
    parts = [f"${expected:,.0f}/day expected from the books with a measured rate"]
    if earned:
        parts.append(f"${earned:,.0f}/day earned by books with an edge verdict")
    if target == TARGET_FLOOR and expected + earned < TARGET_FLOOR:
        parts.append(f"floor ${TARGET_FLOOR:,.0f}")
    return target, "; ".join(parts)
SUSPECT_DAY_FRAC = 0.30                   # a day moving more than this fraction of the book is an artefact, not a result
EVIDENCE = {                              # expected return per unit, from the labs
    "weekly_rs": 0.0136 / 7,              # +1.36% a week on equity (research/signal_rebuild_lab.py)
    "multi_factor": 5.79 / 1e4,           # +5.79 bps a day on equity, risk-parity combination, test half (research/multi_factor_lab.py)
    "events": 0.20 / 30,                  # ≈ −20% per listing vs BTC over 30 days, net of funding (event_study_lab)
    "unlocks": 0.09 / 14,                 # Keyrock: median −9% in the two weeks before a cliff unlock
    "vol_premium": 0.018 / 30,            # vrp_lab: ≈ +1.8% of notional per 30 days for the 1σ/2σ condor
    "carry_default_bps_day": 1.16,        # BTC/ETH/gold blend when a position has no funding history yet
}
PAIR_LEDGERS = ("carry", "carry_q", "events", "fade24", "unlocks", "vol_premium")
_cache: dict = {"at": 0.0, "report": None}
# The profit machine (2026-10-31): once a day the allocator's plan — score drift plus the verdict ladder — becomes the
# plan in force and the money moves. CAPITAL_AUTO=false leaves it advisory (the Portfolio panel's button).
AUTO = os.getenv("CAPITAL_AUTO", "true").lower() != "false"
AUTO_HOUR_UTC = int(os.getenv("CAPITAL_AUTO_HOUR_UTC", "1"))    # after the day's books have settled, before the US session


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ── Equity log for the pair-ledger books ──────────────────────────────────────

def log_pair_equity(now: float | None = None) -> dict[str, float]:
    from experiments import carry, carry_q, events, fade24, unlocks, vol_premium
    now = now or time.time()
    ts = datetime.fromtimestamp(now, timezone.utc).isoformat()
    out = {}
    for name, mod in (("carry", carry), ("carry_q", carry_q), ("events", events), ("fade24", fade24), ("unlocks", unlocks), ("vol_premium", vol_premium)):
        try:
            out[name] = round(mod.equity(), 2)
        except Exception as e:                       # a ledger that isn't initialised yet
            logger.debug("%s equity unavailable: %s", name, e)
    from storage.db import connect
    con = connect(SWING_DB)
    with con:
        con.executemany("INSERT OR IGNORE INTO equity VALUES (?,?,?)", [(n, ts, eq) for n, eq in out.items()])
    con.close()
    return out


# ── Daily P&L from the equity curves ──────────────────────────────────────────

def daily_pnl(series: list[tuple[float, float]], deposits: list[dict]) -> dict[str, float]:
    """{day: P&L} — the day's last equity less the previous day's, less capital deposited in between.
    A deposit only counts if the curve was already being recorded when it landed: a book whose curve
    starts after the deposit has it in its first point, not in the day's move."""
    if not series:
        return {}
    by_day: dict[str, tuple[float, float, float, float]] = {}       # day → (first_ts, first, last_ts, last)
    for ts, eq in series:
        if not ts:
            continue
        d = _day(ts)
        f_ts, first, _, _ = by_day.get(d, (ts, eq, ts, eq))
        by_day[d] = (f_ts, first, ts, eq)
    out, prev = {}, None                                             # prev: (ts, equity) of the last point so far
    for d in sorted(by_day):
        f_ts, first, l_ts, last = by_day[d]
        base_ts, base = prev if prev else (f_ts, first)
        # A move's ledger entry can land a few seconds after the equity snapshot it caused: allow that.
        dep = sum(x["amount"] for x in deposits if base_ts <= x["ts"] <= l_ts + 5)
        pnl = last - base - dep
        if base > 0 and abs(pnl) > SUSPECT_DAY_FRAC * base:
            # A move of a third of the book in a day is a ledger artefact (a deposit the curve missed, a reset), not
            # trading — it is left out of the averages rather than allowed to trip the breaker.
            logger.warning("Suspect day %s: %+.0f on a %.0f base — excluded from the averages", d, pnl, base)
            prev = (l_ts, last)
            continue
        out[d] = round(pnl, 2)
        prev = (l_ts, last)
    return out


def _avg(pnl: dict[str, float], days: int, now: float) -> float:
    """Average over the last `days` calendar days; days with no data count as zero."""
    start = _day(now - (days - 1) * 86400)
    return round(sum(v for d, v in pnl.items() if d >= start) / days, 2)


# ── Expected $/day per book ───────────────────────────────────────────────────

def expected(book: str, equity: float, now: float) -> tuple[float | None, str]:
    try:
        if book == "carry":
            from experiments import carry
            pos = carry.open_positions()
            if not pos:
                return 0.0, "no positions"
            notional = sum(p["notional"] or 0 for p in pos)
            exposure = sum((p["notional"] or 0) * max((now - p["opened_ts"]) / 86400, 0.5) for p in pos)
            funding = sum(p["funding"] or 0 for p in pos)
            bps = funding / exposure * 1e4 if exposure else EVIDENCE["carry_default_bps_day"]
            if funding <= 0 and exposure < notional * 2:
                bps = EVIDENCE["carry_default_bps_day"]
            return round(bps / 1e4 * notional, 2), f"{bps:.2f} bps/day on ${notional:,.0f}"
        if book == "carry_q":
            from experiments import carry_q
            pos = carry_q.open_positions()
            if not pos:
                return 0.0, "no positions"
            per_day = sum((p["basis_ann"] or 0) * (p["notional"] or 0) / 365 for p in pos)
            return round(per_day, 2), f"locked basis on ${sum(p['notional'] or 0 for p in pos):,.0f}"
        if book == "weekly_rs":
            return round(EVIDENCE["weekly_rs"] * equity, 2), "+1.36%/week on equity (lab)"
        if book == "multi_factor":
            return round(EVIDENCE["multi_factor"] * equity, 2), "+5.8 bps/day on equity (lab, risk parity, test half)"
        if book == "events":
            from experiments import events
            pos = events.open_positions()
            notional = sum(p["notional"] or 0 for p in pos)
            return round(EVIDENCE["events"] * notional, 2), f"{len(pos)} pair(s) · −20%/30d on ${notional:,.0f}"
        if book == "unlocks":
            from experiments import unlocks
            pos = unlocks.open_positions()
            notional = sum(p["notional"] or 0 for p in pos)
            return round(EVIDENCE["unlocks"] * notional, 2), f"{len(pos)} pair(s) · −9%/14d on ${notional:,.0f}"
        if book == "vol_premium":
            from experiments import vol_premium
            pos = vol_premium.open_positions()
            notional = sum((p["underlying"] or 0) * (p["qty"] or 0) for p in pos)
            return round(EVIDENCE["vol_premium"] * notional, 2), f"{len(pos)} condor(s) · +1.8%/30d on ${notional:,.0f}"
    except Exception as e:
        logger.debug("expected(%s) failed: %s", book, e)
        return None, "unavailable"
    return None, "no evidence of an edge"


# ── The report ────────────────────────────────────────────────────────────────

def _ledger_equity(book: str) -> float | None:
    try:
        if book in capital.PAIR_BOOKS:
            import importlib
            return float(importlib.import_module(f"experiments.{book}").equity())
        from experiments import books
        if book in books.WALLETS:
            return float(books.WALLETS[book].get_portfolio()["total_equity"])
    except Exception as e:
        logger.debug("equity(%s) unavailable: %s", book, e)
    return None


def report(now: float | None = None, max_age_s: float = 60) -> dict:
    now = now or time.time()
    if _cache["report"] and now - _cache["at"] < max_age_s:
        return _cache["report"]
    from trading import overview
    pf = overview.portfolio(False)
    deps = capital.deposits()
    today = _day(now)
    books, tot = [], {"today": 0.0, "avg7": 0.0, "avg30": 0.0, "expected": 0.0, "equity": 0.0, "added": 0.0}
    for name, m in pf["systems"].items():
        if name in ("all", "everything") or name.endswith("_live") or name in overview.RETIRED_SYSTEMS or name in overview.SIGNAL_SYSTEMS:   # aggregates / signal ledgers, not books
            continue
        pnl = daily_pnl(overview._equity_series(name), [d for d in deps if d["book"] == name])
        exp, why = expected(name, m["equity"], now)
        row = {"book": name, "label": m["label"], "equity": m["equity"], "added": capital.added(name),
               "planned": capital.plan().get(name), "shortfall": capital.shortfall(name),
               "today": pnl.get(today, 0.0), "avg7": _avg(pnl, 7, now),
               "avg30": _avg(pnl, 30, now), "days": len(pnl), "since": min(pnl) if pnl else None,
               "expected": exp, "expected_why": why, "open_positions": m["open_positions"]}
        books.append(row)
        for k in ("today", "avg7", "avg30", "equity", "added"):
            tot[k] += row[k]
        tot["expected"] += exp or 0.0
    # Planned books that haven't traded yet aren't in the overview; they still belong on the board.
    for name in capital.plan():
        if any(r["book"] == name for r in books) or name in overview.SIGNAL_SYSTEMS:
            continue
        eq = _ledger_equity(name)
        if eq is None:
            continue
        exp, why = expected(name, eq, now)
        books.append({"book": name, "label": overview.SYSTEMS.get(name, name), "equity": round(eq, 2), "added": capital.added(name),
                      "planned": capital.plan()[name], "shortfall": capital.shortfall(name),
                      "today": 0.0, "avg7": 0.0, "avg30": 0.0, "days": 0, "since": None,
                      "expected": exp, "expected_why": why, "open_positions": 0})
        tot["equity"] += eq
        tot["added"] += capital.added(name)
        tot["expected"] += exp or 0.0
    books.sort(key=lambda r: (r["expected"] is None, -(r["expected"] or 0), r["book"]))
    tot = {k: round(v, 2) for k, v in tot.items()}
    try:
        verdicts_ = book_verdicts()
    except Exception:
        verdicts_ = {}
    target, target_why = evidence_target(books, verdicts_)
    out = {"target": target, "target_why": target_why, "goal": GOAL, "now": now, "books": books, "totals": tot,
           "gap7": round(target - tot["avg7"], 2), "gap_expected": round(target - tot["expected"], 2), "gap_goal7": round(GOAL - tot["avg7"], 2),
           "paused": capital.paused(), "trip_avg7": TRIP_AVG7, "plan": capital.plan(), "plan_note": kv.get_sync(capital._k("plan_note")), "deposits": deps[-20:],
           "auto": {"on": AUTO, "hour_utc": AUTO_HOUR_UTC, "last": kv.get_sync(kv.key("capital", "auto", "last"))},
           "measuring_since": min((r["since"] for r in books if r["since"]), default=None)}
    _cache.update(at=now, report=out)
    return out


# ── The loop ──────────────────────────────────────────────────────────────────

async def tick(now: float | None = None) -> dict:
    """Every 15 minutes: log the pair books' equity, refresh the report, trip the breaker if it's due."""
    import asyncio
    now = now or time.time()
    await asyncio.to_thread(log_pair_equity, now)
    try:
        # Keep every book at its plan: deposits unless the breaker has paused them, withdrawals always
        # (a cut book gives up its free cash now and the rest as positions close).
        moved = await asyncio.to_thread(capital.apply_plan, bool(capital.paused()))
        if moved:
            logger.info("Capital plan applied: %s", [(m["book"], m["amount"]) for m in moved])
    except Exception as e:
        logger.warning("Capital plan not applied: %s", e)
    rep = await asyncio.to_thread(report, now, 0)
    try:
        auto = await asyncio.to_thread(auto_allocate, now, rep)
        if auto:
            rep = await asyncio.to_thread(report, now, 0)
            rep["auto_last"] = auto
    except Exception as e:
        logger.warning("Automatic allocation skipped: %s", e)
    avg7, days = rep["totals"]["avg7"], max((r["days"] for r in rep["books"]), default=0)
    if days >= 7 and avg7 < TRIP_AVG7 and not capital.paused():
        reason = f"7-day average {avg7:+.0f}/day is below {TRIP_AVG7:+.0f} — programme capital paused for new sizing"
        capital.pause(reason)
        from ops.watchdog import _notify
        await _notify("Daily target: breaker tripped", reason)
        rep["paused"] = capital.paused()
    kv.set_sync(kv.key("target", "last"), {"at": now, "avg7": avg7, "today": rep["totals"]["today"]}, 7 * 86400)
    return rep


def book_verdicts() -> dict[str, dict]:
    """Every book's own 40-trade verdict from the unified trade record (the allocator's ladder input)."""
    from trading import allocator, overview
    rows, _ = overview.all_rows()
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["system"], []).append(r)
    return allocator.verdicts(by)


def suggestion(rep: dict | None = None) -> dict:
    """The allocator's full suggestion (score drift + verdict ladder) for the tracker's current rows."""
    from trading import allocator
    rep = rep or report(None, 0)
    return allocator.plan_for(rep["books"], book_verdicts())


def auto_allocate(now: float, rep: dict | None = None) -> dict | None:
    """Once a day at AUTO_HOUR_UTC: make the suggestion the plan in force and move the money. Returns what moved."""
    if not AUTO or capital.paused():
        return None
    day = datetime.fromtimestamp(now, timezone.utc)
    if day.hour < AUTO_HOUR_UTC:
        return None
    key = kv.key("capital", "auto", day.strftime("%Y-%m-%d"))
    if kv.get_sync(key):
        return None
    kv.set_sync(key, {"at": now}, 3 * 86400)
    sug = suggestion(rep)
    if not sug["changed"]:
        kv.set_sync(kv.key("capital", "auto", "last"), {"at": now, "moves": [], "note": "no change: nothing eligible moved"}, 30 * 86400)
        return None
    capital.set_plan(sug["suggested"], f"automatic allocation {day:%Y-%m-%d}: " + "; ".join(f"{m['book']} {m['delta']:+.0f}" for m in sug["moves"]))
    moved = capital.apply_plan()
    out = {"at": now, "moves": sug["moves"], "applied": [(m["book"], m["amount"]) for m in moved],
           "verdicts": {b: v for b, v in sug.get("verdicts", {}).items() if v in ("edge", "no_edge")}}
    kv.set_sync(kv.key("capital", "auto", "last"), out, 30 * 86400)
    logger.info("Automatic allocation: %s", [(m["book"], m["delta"]) for m in sug["moves"]])
    return out
