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
TOTAL = float(os.getenv("CAPITAL_TOTAL_USD", "195500"))      # $50k across the books + $56.5k copy books + $30k mentor's book + $49k autotrader as executor (2026-11-01) + $10k burst books (2026-11-06)
TARGET_USD_DAY = float(os.getenv("DAILY_TARGET_USD", "200"))    # $200/day since 2026-10-29 (0.4% of the $50k a day)
PLAN: dict[str, float] = {
    "carry": 8_000.0, "carry_q": 8_000.0, "events": 8_000.0,
    "futures_trend": 5_500.0,
    "unlocks": 4_000.0, "vol_premium": 3_000.0,
    "auto": 50_000.0, "copy_trading": 32_750.0, "copy_stocks": 32_750.0,
    "weekly_rs": 1_000.0, "multi_factor": 1_000.0, "cascade": 500.0, "main": 1_000.0, "auction": 30_000.0,
    "burst_up": 5_000.0, "burst_dip": 5_000.0,
}
# 2026-11-06: the two burst books join at $5,000 each (new money) — the spike lab's post-hoc rules on a forward test;
# local only until a verdict.
# 2026-11-02 (consolidation, the user's call after the first full review): claude_swing retired (12 trades, −0.16R,
# 24 days) — its $500 parked in main; the nine $611.11 copy-variant wallets folded into the three market books
# (+$1,833.33 each) so the variants are judged as rules on one wallet instead of nine books with 0–15 trades each.
# 2026-11-03: copy_futures retired (zero copies in a week) — its $21,833.34 split between the crypto and stock copy books.
GOAL_USD_DAY = TARGET_USD_DAY                                   # the $200/day ambition; trading/target.py sets today's target from the evidence
# 2026-11-01 (later): the mentor's book goes to $30,000 (new money) — the user's live experiment with real-world sizes and a
# $200–500/day goal (experiments/auction.DAILY_GOAL).
# 2026-11-01: the autotrader is the executor of the evidence books (autotrader/engine.EXECUTOR) and the user moved it to
# its paper wallet so trades can carry real-world sizes: $50,000 (new money). The signal books' entries (events 8k,
# unlocks 4k, weekly 1k, cascade 500, multi_factor 1k) are its lane budgets; the scanner and reversal lanes get a fixed
# measuring budget each (autotrader/engine.ENTRY_BUDGET_USD); the rest is the ledger's reserve.
# 2026-10-30: the stocks trend book failed its 40-trade verdict and was retired (its lifetime −$375 stays on its ledger,
# off the plan). Its $5,500 went, as asked, entirely to the nine copy-trading variant books ($611.11 each).
# 2026-10-29 (later): copy books for stock and commodity perps join at $1k each, taken from the autotrader's scanner
# wallet (2k → 1k) and Claude swing (1.5k → 0.5k) — the two directional books with the weakest records.
# 2026-10-29: the multi-factor basket joins at $1k, taken from Claude swing (the worst-scoring directional book, which the
# allocator was already suggesting to cut).
# 2026-10-31: the three copy books go to $20,000 each — the budget the user will put behind copy trading while it works
# (the rules variants stay at measuring size). That is new money: the total rises from $50,000 to $106,500.
assert abs(sum(PLAN.values()) - TOTAL) < 1, f"the plan must add up to ${TOTAL:,.0f}"
PAIR_BOOKS = ("carry", "carry_q", "events", "fade24", "hold72", "unlocks", "vol_premium")       # own-ledger books (no PaperWallet)
PLAN_NOTE = "$50k across the books (2026-10-28)"


def _k(*parts: str) -> str:
    return kv.key("capital", *parts)


def plan() -> dict[str, float]:
    """The plan in force: PLAN, or the last suggestion the user applied (trading/allocator.py), which keeps the total.
    On a production instance only the promoted books are in it (trading/book_switches.promoted): the others hold no
    capital there — their money is not reallocated, it is simply not on this instance (2026-11-04)."""
    from trading import book_switches
    override = kv.get_sync(_k("plan"))
    if override and abs(sum(override.values()) - sum(PLAN.values())) < 1 and set(override) == set(PLAN):
        full = {k: float(v) for k, v in override.items()}
    else:
        full = dict(PLAN)
    return {k: v for k, v in full.items() if book_switches.promoted(k)}


def set_plan(new: dict[str, float], note: str) -> dict[str, float]:
    """Replace the plan in force (same books, same total). The tracker's next tick moves the money."""
    if set(new) != set(PLAN) or abs(sum(new.values()) - sum(PLAN.values())) >= 1:
        raise ValueError("a plan must cover the same books and keep the same total")
    kv.set_sync(_k("plan"), {k: round(float(v), 2) for k, v in new.items()})
    kv.set_sync(_k("plan_note"), {"at": time.time(), "note": note})
    logger.info("Capital plan replaced (%s): %s", note, new)
    return plan()


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
    target = plan().get(book)
    if target is None:
        return 0.0
    gap = target - (BASE + added(book))
    return round(gap, 2) if abs(gap) >= 1 else 0.0


def apply_plan(withdraw_only: bool = False) -> list[dict]:
    """Bring every planned book to its plan (idempotent; run every tracker tick so withdrawals catch up). Signal ledgers
    (trading/overview.SIGNAL_SYSTEMS) keep their plan entry as the autotrader lane's budget but hold no plan money."""
    from trading import overview
    out = []
    for book in plan():                                            # unpromoted books are not in the plan here
        if book in overview.SIGNAL_SYSTEMS:
            continue
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
    # Capital the plan has cut but that is still tied up as margin (withdrawals only take free cash) is on its way
    # out: size as if it were already gone, or a cut book keeps opening positions for its old balance (2026-10-29:
    # the weekly book's $1,000 legs on a $1,000 plan).
    gap = shortfall(book) if extra else 0.0          # only once the programme has started moving this book's capital
    if gap < 0:
        return max(equity + gap, 0.0)
    return equity
