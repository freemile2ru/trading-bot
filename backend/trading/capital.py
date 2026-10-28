"""Capital across the paper books: $50k in total, placed where the evidence is (2026-10-28).

Every book starts from the same $10k seed (BASE). The plan below says what each should hold; apply_plan()
moves the difference in either direction — a deposit when a book is below its plan, a withdrawal when it is
above. Withdrawals from a PaperWallet book are limited to its free cash (the margin on open positions stays
until they close), so a book that was cut while holding positions catches up over the following ticks.

Where the $50k goes, and why (last 30 days live + the research behind each book):
  carry / carry_q   $8k each   funding and locked basis: hedged, small but positive, passed an untouched holdout
  events            $8k        listing / delisting shorts: the strongest researched edge (t −3…−5), fires on announcements
  futures_trend     $5.5k      +$10/day live over 7 days; sized by gross exposure, not spread thin over 20 names
  stocks_trend      $5.5k      same rule, same live sign; "on notice" from the 20-year ETF replay
  unlocks           $4k        cliff-unlock shorts, positive so far, two months from its own event study
  vol_premium       $3k        weekly condors, early
  claude_swing      $2.5k      the only book with a +2R win; negative expectancy overall
  auto              $2k        no edge shown; stays small until it does
  copy_trading      $1.5k      two copies so far
  weekly_rs         $1k        was $30k: −$26/day over the week and its replay edge had faded — back to a measuring size
  cascade / main    $500 each  cascade is measuring; main is the manual wallet

Leverage round (2026-10-29, asked for: "7–10×" on the $50k, target $200/day): carry and carry Q 8× notional, events and
unlocks 35% a leg (~7× gross when full), condors 15% of equity at risk per structure, the directional books 3% of
equity at the stop, copies 15% each with a 10× cap. The trend books (2.8× gross) and the weekly basket (2.5×) stop
short of the ask: their 15% / 30% disaster stops cap a position's leverage at 3× (liquidation must sit beyond the
stop), so the margin for more does not exist. Wins and losses both scale with it; nothing here changes expectancy.

A move is a ledger entry, never a reset: a PaperWallet book's balance and starting balance change together (so
its return % stays honest); a pair-ledger book's starting balance is adjusted through `added`. Every move is
recorded with a timestamp so the target tracker takes it out of the day's P&L.

Circuit breaker (trading/target.py): if the 7-day average across all books falls below −TARGET, the programme
pauses — books above base size size new positions from base until someone resumes it. Withdrawals still run.
"""
from __future__ import annotations

import logging
import os
import time

from storage import state as kv

logger = logging.getLogger("trading.capital")

BASE = 10_000.0
TOTAL = float(os.getenv("CAPITAL_TOTAL_USD", "50000"))
TARGET_USD_DAY = float(os.getenv("DAILY_TARGET_USD", "200"))    # $200/day since 2026-10-29 (0.4% of the $50k a day)
PLAN: dict[str, float] = {
    "carry": 8_000.0, "carry_q": 8_000.0, "events": 8_000.0,
    "futures_trend": 5_500.0, "stocks_trend": 5_500.0,
    "unlocks": 4_000.0, "vol_premium": 3_000.0,
    "claude_swing": 2_500.0, "auto": 2_000.0, "copy_trading": 1_500.0,
    "weekly_rs": 1_000.0, "cascade": 500.0, "main": 500.0,
}
assert abs(sum(PLAN.values()) - 50_000) < 1, "the plan must add up to $50k"
PAIR_BOOKS = ("carry", "carry_q", "events", "fade24", "hold72", "unlocks", "vol_premium")       # own-ledger books (no PaperWallet)
PLAN_NOTE = "$50k across the books (2026-10-28)"


def _k(*parts: str) -> str:
    return kv.key("capital", *parts)


def added(book: str) -> float:
    """Capital moved into (+) or out of (−) `book` on top of its base balance."""
    return float(kv.get_sync(_k("added", book)) or 0.0)


def initial(book: str, base: float) -> float:
    """A pair-ledger book's starting balance: its constant plus what the programme moved."""
    return base + added(book)


def deposits() -> list[dict]:
    return kv.get_sync(_k("ledger")) or []


def _wallet(book: str):
    """The PaperWallet behind a book, or None for a pair-ledger book."""
    from experiments import books
    if book in books.WALLETS:
        return books.WALLETS[book]
    from markets import engine as markets
    if book in markets.WALLETS:
        return markets.WALLETS[book]
    if book in ("main", "auto"):
        from live import swing as live_swing
        return live_swing.paper(book)
    return None


def _move(book: str, amount: float, note: str) -> dict | None:
    """Move `amount` (+ in, − out). Returns the ledger entry, or None when nothing could move."""
    ts = time.time()                    # stamped before the move, so it is never later than the wallet's equity snapshot
    w = _wallet(book)
    if w is not None:
        moved = amount if amount > 0 else -w.withdraw(-amount, note)
        if amount > 0:
            w.deposit(amount, note)
    elif book in PAIR_BOOKS:
        moved = amount
    else:
        raise ValueError(f"unknown book {book}")
    if abs(moved) < 0.01:
        return None
    kv.set_sync(_k("added", book), added(book) + moved)
    entry = {"ts": ts, "book": book, "amount": round(moved, 2), "note": note}
    kv.set_sync(_k("ledger"), deposits() + [entry])
    logger.info("Capital: %+.0f %s (%s) → %.0f", moved, book, note, BASE + added(book))
    return entry


def deposit(book: str, amount: float, note: str = PLAN_NOTE) -> dict | None:
    if float(amount) <= 0:
        raise ValueError("deposit must be positive")
    return _move(book, float(amount), note)


def withdraw(book: str, amount: float, note: str = PLAN_NOTE) -> dict | None:
    """Take capital out. A PaperWallet gives up at most its free cash; the rest waits for positions to close."""
    if float(amount) <= 0:
        raise ValueError("withdrawal must be positive")
    return _move(book, -float(amount), note)


def shortfall(book: str) -> float:
    """How far a book still is from its plan (+ owed to it, − still to take out); 0 when not planned."""
    target = PLAN.get(book)
    if target is None:
        return 0.0
    gap = target - (BASE + added(book))
    return round(gap, 2) if abs(gap) >= 1 else 0.0


def apply_plan(withdraw_only: bool = False) -> list[dict]:
    """Bring every planned book to its plan (idempotent; run every tracker tick so withdrawals catch up)."""
    out = []
    for book in PLAN:
        gap = shortfall(book)
        try:
            entry = deposit(book, gap) if gap > 0 and not withdraw_only else withdraw(book, -gap) if gap < 0 else None
        except Exception as e:
            logger.warning("Capital plan for %s failed: %s", book, e)
            continue
        if entry:
            out.append(entry)
    return out


# ── Pause (the circuit breaker's lever) ───────────────────────────────────────

def paused() -> dict | None:
    return kv.get_sync(_k("paused"))


def pause(reason: str) -> dict:
    p = {"at": time.time(), "reason": reason}
    kv.set_sync(_k("paused"), p)
    logger.warning("Capital programme paused: %s", reason)
    return p


def resume() -> None:
    kv.delete_sync(_k("paused"))
    logger.info("Capital programme resumed")


def sizing_equity(book: str, equity: float) -> float:
    """What a book should size new positions from: its equity, or — while paused — its equity without the
    programme's extra capital, so exposure drifts back to base as positions roll. Books cut below base are unchanged."""
    extra = added(book)
    if paused() and extra > 0:
        return max(equity - extra, 0.0)
    return equity
