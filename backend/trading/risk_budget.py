"""The daily risk budget — how much the programme may lose in a day, split across the books that trade it, and the
return it is asked for (2026-11-14, the user: "assuming I have a balance of 50K in prod, and I want to risk at least 5K
everyday … target at least $500 return per day").

Before this every book sized itself: a reversal leg risked 0.2% of the ledger (autotrader/sizing.py), a copy 0.5% of its
book (experiments/copy_trading.py), each with its own caps. The user's framing is a day's risk, not a leg's, so:

  budget      DAILY_RISK_PCT of the base ledger's equity (BASE_BOOK, the autotrader's $50k on prod) — $5,000 a day at 10%.
  shares      split across BOOKS by each book's realised record: weight = its shrunken average R over its last RECORD_N
              closed trades, never below FLOOR_R, so a book with no edge yet still gets a measuring share and a book
              with a record gets most of the day. Recomputed every minute from the ledgers.
  per leg     a book's share ÷ the legs it opens in a day (its trailing LOOKBACK_DAYS average, never below MIN_LEGS):
              the dollars one leg may lose at its stop. The sizers turn that into notional through the leg's own stop.
  the stop    once the sharing books together are down the budget on the day (equity now against 00:00 UTC), no sharing
              book opens anything until the next UTC day. The stop is on the day's P&L, open and closed, so a day that
              gives back its morning is caught too.
  the target  DAILY_TARGET_USD, shown against what the shares and records say to expect: Σ share × realised R. The
              target is the ask; the expected figure is the evidence — the gap between them is not closed by size.

The caps the user agreed to scale with the budget (2026-11-14): the lane's net-exposure cap 25 → 100% of equity, its leg
cap 8 → 25%, its heat cap from the budget share; the copy leg cap 25 → 100% of the book and gross 150 → 300%. The 4% coin
cap and one-trade-one-system stay. DAILY_RISK_PCT=0 turns all of this off and every book sizes as before.
"""
from __future__ import annotations

import logging
import os
import time

from storage import state as kv

logger = logging.getLogger("trading.risk_budget")

DAILY_RISK_PCT = float(os.getenv("DAILY_RISK_PCT", "10"))
DAILY_TARGET_USD = float(os.getenv("DAILY_TARGET_USD", "500"))
BASE_BOOK = os.getenv("DAILY_RISK_BASE_BOOK", "auto")
ENABLED = DAILY_RISK_PCT > 0
BOOKS = tuple(b.strip() for b in os.getenv("DAILY_RISK_BOOKS", "auto:reversal,copy_trading,copy_stocks").split(",") if b.strip())
MIN_LEGS = {"auto:reversal": 4.0, "copy_trading": 1.0, "copy_stocks": 1.0}
FLOOR_R = 0.02
RECORD_N = 20
MIN_RECORD_N = 10          # a book weighs above the floor only once it has this many closed trades (first prod read: copy_stocks took 55% of the day on 9 copies)
SHARE_CAP = float(os.getenv("DAILY_RISK_SHARE_CAP", "0.5"))   # no book takes more than this share of the day
MATURITY_N = int(os.getenv("DAILY_RISK_MATURITY_N", "40"))    # 2026-11-17: a leg's risk is scaled by min(1, closed trades / MATURITY_N) — the history review
                                                              # found outcome falling with size on both instances because the budget put its largest legs on the
                                                              # thinnest records (copy_stocks 50% of the day, $2.5k a leg, on 12 trades; the auction book's two
                                                              # NVDA legs at n = 0 and 1 were −$924). Replayed on every closed trade: prod −$318 → +$316, local
                                                              # −$4,372 → −$3,258; on the four budget days alone it is slightly worse (27 and 46 legs), so it is
                                                              # judged forward: each leg carries its maturity, and leg_risk_full_usd is the unscaled counterfactual.
SHRINK_N = 20
LOOKBACK_DAYS = 14.0
CACHE_S = 60.0
_cache: dict = {"at": 0.0, "report": None}


def _ts(s) -> float:
    from experiments.control import _ts
    return _ts(s)


def _wallet(book: str):
    if book.startswith("auto:") or book == "auto":
        from trading import history
        return history.WALLETS["auto"]
    from experiments import books
    return books.WALLETS.get(book)


def closed_trades(book: str) -> list[dict]:
    """The book's closed trades, oldest first, with opened_ts (the autotrader's by lane, bookkeeping closes left out)."""
    w = _wallet(book)
    if w is None:
        return []
    rows = w.all_trades()["closed"]
    if book.startswith("auto:"):
        from autotrader import lanes
        lane = book.split(":", 1)[1]
        rows = [t for t in rows if lanes.lane_of(t) == lane and t.get("close_reason") not in lanes.ADMIN_CLOSES]
    out = [{**t, "opened_ts": t.get("opened_ts") or _ts(t.get("opened_at"))} for t in rows]
    return sorted((t for t in out if t["opened_ts"]), key=lambda t: t["opened_ts"])


def record_r(closed: list[dict]) -> tuple[float, int]:
    """The shrunken average R of the last RECORD_N closed trades: avg × n / (n + SHRINK_N) — a short record counts less."""
    rs = [(t.get("pnl_usd") or 0.0) / t["risk_usd"] for t in closed[-RECORD_N:] if t.get("risk_usd")]
    if not rs:
        return 0.0, 0
    return sum(rs) / len(rs) * len(rs) / (len(rs) + SHRINK_N), len(rs)


def legs_per_day(book: str, closed: list[dict], now: float) -> float:
    start = now - LOOKBACK_DAYS * 86400
    recent = [t for t in closed if t["opened_ts"] >= start]
    span_days = min(LOOKBACK_DAYS, max(1.0, (now - min(t["opened_ts"] for t in closed)) / 86400)) if closed else LOOKBACK_DAYS
    return max(MIN_LEGS.get(book, 1.0), len(recent) / span_days)


def base_equity() -> float:
    w = _wallet(BASE_BOOK)
    return float(w.get_portfolio()["total_equity"]) if w is not None else 0.0


def _day_start_equity(label: str, start: float) -> float | None:
    from trading import overview
    series = overview._equity_series(label)
    before = [eq for ts, eq in series if ts and ts < start]
    if before:
        return before[-1]
    after = [eq for ts, eq in series if ts and ts >= start]
    return after[0] if after else None


def day_pnl(now: float) -> dict[str, float]:
    """Each sharing wallet's P&L on the UTC day, open and closed: equity now against its equity at 00:00 UTC."""
    start = now - now % 86400
    out: dict[str, float] = {}
    for label in sorted({"auto" if b.startswith("auto:") else b for b in BOOKS}):
        w = _wallet(label)
        if w is None:
            continue
        eq0 = _day_start_equity(label, start)
        if eq0 is None:
            continue
        out[label] = round(float(w.get_portfolio()["total_equity"]) - eq0, 2)
    return out


def compute(now: float | None = None) -> dict:
    """The whole picture, from the ledgers: the budget, the shares, each book's per-leg risk, the day's use, the expected."""
    now = now or time.time()
    if not ENABLED:
        return {"on": False, "pct": DAILY_RISK_PCT, "target_usd": DAILY_TARGET_USD}
    equity = base_equity()
    budget = round(equity * DAILY_RISK_PCT / 100, 2)
    books = {}
    for b in BOOKS:
        closed = closed_trades(b)
        r, n = record_r(closed)
        books[b] = {"record_r": round(r, 4), "record_n": n, "weight": max(r, FLOOR_R) if n >= MIN_RECORD_N else FLOOR_R,
                    "legs_per_day": round(legs_per_day(b, closed, now), 2),
                    "record_total_n": len(closed), "maturity": round(min(1.0, len(closed) / MATURITY_N), 3) if MATURITY_N > 0 else 1.0}
    total_w = sum(x["weight"] for x in books.values()) or 1.0
    shares = {b: x["weight"] / total_w for b, x in books.items()}
    # No book takes more than SHARE_CAP of the day (2026-11-16: copy_stocks held 62% on an 11-trade record); the excess
    # goes to the others pro rata.
    for _ in range(len(shares)):
        over = {b: s - SHARE_CAP for b, s in shares.items() if s > SHARE_CAP + 1e-9}
        if not over or len(shares) < 2:
            break
        rest = {b: s for b, s in shares.items() if b not in over}
        rest_total = sum(rest.values()) or 1.0
        for b in over:
            shares[b] = SHARE_CAP
        for b in rest:
            shares[b] += sum(over.values()) * rest[b] / rest_total
    expected = 0.0
    for b, x in books.items():
        x["share_usd"] = round(budget * shares[b], 2)
        x["share_pct"] = round(100 * shares[b], 1)
        x["leg_risk_full_usd"] = round(x["share_usd"] / x["legs_per_day"], 2)
        x["leg_risk_usd"] = round(x["leg_risk_full_usd"] * x["maturity"], 2)              # a thin record risks a thin leg
        x["expected_usd_day"] = round(x["share_usd"] * x["record_r"] * x["maturity"], 2)
        expected += x["expected_usd_day"]
    pnl = day_pnl(now)
    used = round(max(0.0, -sum(pnl.values())), 2)
    blocked = used >= budget > 0
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if blocked and kv.get_sync(kv.key("risk_budget", "blocked", day)) is None:
        kv.set_sync(kv.key("risk_budget", "blocked", day), {"at": now, "used": used, "budget": budget}, 3 * 86400)
        logger.warning("Daily risk budget hit: −$%.0f on the day against $%.0f — no sharing book opens anything until 00:00 UTC", used, budget)
    return {"on": True, "pct": DAILY_RISK_PCT, "base_book": BASE_BOOK, "base_equity": round(equity, 2), "budget_usd": budget, "books": books,
            "day_pnl": pnl, "day_pnl_total": round(sum(pnl.values()), 2), "used_usd": used, "left_usd": round(max(0.0, budget - used), 2), "blocked": blocked,
            "target_usd": DAILY_TARGET_USD, "expected_usd_day": round(expected, 2), "needed_r": round(DAILY_TARGET_USD / budget, 3) if budget else None,
            "floor_r": FLOOR_R, "record_n": RECORD_N, "maturity_n": MATURITY_N, "at": now}


def report(now: float | None = None, max_age_s: float = CACHE_S) -> dict:
    now = now or time.time()
    if _cache["report"] and now - _cache["at"] < max_age_s:
        return _cache["report"]
    rep = compute(now)
    _cache.update(at=now, report=rep)
    return rep


def leg_risk(book: str, now: float | None = None) -> dict:
    """What one leg of `book` may lose at its stop right now — or that the book is blocked for the day. {"on": False} when
    the budget is off (the sizer uses its own rule)."""
    try:
        rep = report(now)
    except Exception as e:                                   # a ledger that cannot be read: the sizer falls back to its own rule
        logger.warning("Risk budget unavailable (%s) — %s sizes by its own rule", e, book)
        return {"on": False}
    if not rep.get("on") or book not in rep["books"]:
        return {"on": False}
    x = rep["books"][book]
    mature = x["maturity"] >= 1.0
    return {"on": True, "risk_usd": x["leg_risk_usd"], "risk_full_usd": x["leg_risk_full_usd"], "maturity": x["maturity"], "record_total_n": x["record_total_n"],
            "share_usd": x["share_usd"], "legs_per_day": x["legs_per_day"], "record_r": x["record_r"],
            "budget_usd": rep["budget_usd"], "used_usd": rep["used_usd"], "blocked": rep["blocked"],
            "why": (f"risk budget: ${x['leg_risk_usd']:,.0f} a leg = {x['share_pct']:g}% of ${rep['budget_usd']:,.0f}/day (record {x['record_r']:+.3f}R over {x['record_n']}) "
                    f"÷ {x['legs_per_day']:g} legs a day" + ("" if mature else f" × {x['maturity']:.2f} ({x['record_total_n']} of {MATURITY_N} trades)")
                    + (f"; BLOCKED: −${rep['used_usd']:,.0f} on the day" if rep["blocked"] else f"; ${rep['left_usd']:,.0f} of the day left"))}
