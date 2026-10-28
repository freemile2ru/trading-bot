"""The $100-a-day programme (2026-10-28): capital follows evidence.

The books that predict direction (autotrader, Claude swing, fade-24, the trend books) stay at their $10k —
they're still running as asked, but they've shown no edge, so they get no more. The books whose return comes
from structure get the capital, because their income scales with notional and their risk is hedged:

  carry_q   quarterly basis, locked at entry        → $40k   (3× notional; converges by delivery)
  carry     BTC/ETH/gold funding                     → $30k   (3× notional)
  weekly_rs weekly near-high / near-low basket       → $30k   (2× — the researched size)
  events    listing / delisting pair shorts          → $25k   (10% legs, up to 10 pairs)

A deposit is a ledger entry, never a reset: a PaperWallet book's balance and starting balance both rise (so its
return % stays honest), a pair-ledger book's starting balance is raised through this module. Every deposit is
recorded with a timestamp so the target tracker can take it out of the day's P&L.

Circuit breaker (trading/target.py): if the 7-day average across all books falls below −TARGET, the programme
pauses — new sizing for the scaled books falls back to their base capital until someone resumes it. Nothing
is closed; positions already on just stop being topped up.
"""
from __future__ import annotations

import logging
import os
import time

from storage import state as kv

logger = logging.getLogger("trading.capital")

BASE = 10_000.0
TARGET_USD_DAY = float(os.getenv("DAILY_TARGET_USD", "100"))
PLAN: dict[str, float] = {"carry_q": 40_000.0, "carry": 30_000.0, "weekly_rs": 30_000.0, "events": 25_000.0}
PAIR_BOOKS = ("carry", "carry_q", "events", "fade24", "hold72", "unlocks", "vol_premium")       # own-ledger books (no PaperWallet)
PLAN_NOTE = "$100/day programme (2026-10-28)"


def _k(*parts: str) -> str:
    return kv.key("capital", *parts)


def added(book: str) -> float:
    """Capital deposited into `book` on top of its base balance."""
    return float(kv.get_sync(_k("added", book)) or 0.0)


def initial(book: str, base: float) -> float:
    """A pair-ledger book's starting balance: its constant plus what the programme added."""
    return base + added(book)


def deposits() -> list[dict]:
    return kv.get_sync(_k("ledger")) or []


def deposit(book: str, amount: float, note: str = PLAN_NOTE) -> dict:
    """Add `amount` to a book. PaperWallet books take it on the wallet; pair-ledger books through `added`."""
    from experiments import books
    amount = float(amount)
    if amount <= 0:
        raise ValueError("deposit must be positive")
    if book in books.WALLETS:
        after = books.WALLETS[book].deposit(amount, note)
    elif book in PAIR_BOOKS:
        after = initial(book, BASE) + amount
    else:
        raise ValueError(f"unknown book {book}")
    kv.set_sync(_k("added", book), added(book) + amount)
    entry = {"ts": time.time(), "book": book, "amount": amount, "note": note}
    kv.set_sync(_k("ledger"), deposits() + [entry])
    logger.info("Capital: +$%.0f into %s (%s) → %s", amount, book, note, after)
    return entry


def apply_plan() -> list[dict]:
    """Bring each planned book up to its programme balance (idempotent)."""
    out = []
    for book, target in PLAN.items():
        have = BASE + added(book)
        if target - have >= 1:
            out.append(deposit(book, target - have))
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
    programme's capital, so exposure drifts back to base as positions roll."""
    if paused() and added(book):
        return max(equity - added(book), 0.0)
    return equity
