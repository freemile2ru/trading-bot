"""A tenant's own paper wallets, one per book, under data/tenants/<tenant>/swing_trading.db (its own schema on Postgres).

`view(mapping)` is how the owner-era routes become tenant-aware: given the module-level {system: wallet} they were
written against, it returns the same mapping for the owner and the current tenant's wallets (only the books the tenant
runs) for a subscriber — so the Trades, Portfolio, Books and position routes show a subscriber their own record.
"""
from __future__ import annotations

import os
import threading

from storage.engine import DATA_DIR
from tenancy import accounts, context, tiers
from trading.paper_trader import PaperWallet

TENANTS_DIR = os.getenv("TENANTS_DIR", os.path.join(DATA_DIR, "tenants"))

_lock = threading.Lock()
_cache: dict[tuple[str, str], PaperWallet] = {}


def path_for(tenant_id: str) -> str:
    return os.path.join(TENANTS_DIR, tenant_id, "swing_trading.db")


def get(tenant_id: str, book: str) -> PaperWallet:
    """The tenant's wallet for `book`, created on first use with the tenant's paper capital."""
    key = (tenant_id, book)
    with _lock:
        w = _cache.get(key)
        if w is None:
            cap = accounts.settings(tenant_id)["paper_capital_usd"] or tiers.limits(tiers.PRO)["paper_capital_usd"]
            w = PaperWallet(path_for(tenant_id), label=book, initial_balance=float(cap), history_cap=2000, min_snapshot_seconds=120, tenant=tenant_id)
            _cache[key] = w
    return w


def books_on(tenant_id: str) -> list[str]:
    """The books this tenant runs: switched on in their settings and allowed by their tier."""
    t = accounts.tenant(tenant_id)
    if not t:
        return []
    allowed = set(tiers.books_allowed(t["tier"]))
    s = accounts.settings(tenant_id)
    return [b for b, on in s["books"].items() if on and b in allowed]


def existing(tenant_id: str) -> list[str]:
    """Books with a wallet on disk (open or closed trades), on or off."""
    return sorted({b for (t, b) in _cache if t == tenant_id} | set(books_on(tenant_id)))


def view(mapping: dict) -> dict:
    """The owner's {system: wallet} for the owner; a subscriber's own wallets for the books they run."""
    if context.is_owner():
        return mapping
    tid = context.tenant_id()
    return {b: get(tid, b) for b in existing(tid) if b in mapping}


def forget(tenant_id: str | None = None) -> None:
    """Drop cached wallets (tests, or after a tenant is removed)."""
    with _lock:
        for k in [k for k in _cache if tenant_id is None or k[0] == tenant_id]:
            del _cache[k]


def equity(tenant_id: str) -> float | None:
    books = existing(tenant_id)
    if not books:
        return None
    return round(sum(get(tenant_id, b).get_portfolio()["total_equity"] for b in books), 2)
