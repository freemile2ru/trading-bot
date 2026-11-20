"""The tenant engine host — every trading engine run once per opted-in tenant, behind a flag (2026-11-20).

  TENANT_ENGINES=true        the flag (default off: nothing here runs, tenants are mirrored by tenancy/runner.py)
  feature "engines.own"      the tier that may opt in (Elite)
  settings.engine_mode       "mirror" (default) or "own": the tenant's choice, when the flag and tier allow

How it works without rewriting the engines: an engine's state lives in the kv store and its positions in a module-level
wallet. Inside `context.as_tenant(tid)` the kv store scopes every key to the tenant (storage/state.py) and the wallet
globals resolve to the tenant's own wallets (tenancy/proxies.py). So calling the engine's own tick inside that context
IS the tenant's run of it: their switches, their watchlist and cooldowns, their positions, sized from their capital,
with the owner's parameters as defaults. Market reads (the scan snapshot, Claude's budget) stay shared on purpose.

Known limits, stated: module-level in-memory caches inside the engines are shared across tenants (kv and wallets are
not); Claude-reviewing engines spend the one shared LLM budget, so the autotrader's Claude calls scale with opted-in
tenants; the carry books (their own SQLite ledger) and the stocks/futures books are not run per tenant yet.
"""
from __future__ import annotations

import logging
import os
import time

from tenancy import accounts, context, tiers, wallets

logger = logging.getLogger("tenancy.engines")

FLAG = os.getenv("TENANT_ENGINES", "false").lower() == "true"
FEATURE = "engines.own"
MODES = ("mirror", "own")

# (name, the books it trades, how to run one tick of it) — resolved lazily so importing this module imports no engine.
BOOK_TICKS: list[tuple[str, tuple[str, ...], str]] = [
    ("copy", ("copy_trading", "copy_stocks", "copy_futures"), "experiments.copy_trading:tick"),
    ("auction", ("auction",), "experiments.auction:tick"),
    ("prop", ("prop",), "experiments.prop:tick"),
    ("composite", ("composite",), "experiments.composite:tick"),
    ("burst", ("burst_up", "burst_dip", "burst_down", "burst_rip"), "experiments.burst:tick"),
    ("cascade", ("cascade",), "experiments.cascade:tick"),
    ("weekly_rs", ("weekly_rs",), "experiments.weekly_rs:rebalance"),
    ("reversal_index", ("reversal_index",), "experiments.reversal_index:tick"),
    ("unlocks", ("unlocks",), "experiments.unlocks:tick"),
    ("events", ("events",), "experiments.events:tick"),
]
_state = {"last_tick": None, "ticks": 0, "runs": 0, "errors": [], "per_tenant": {}}


def _load(ref: str):
    import importlib
    mod, fn = ref.split(":")
    return getattr(importlib.import_module(mod), fn)


def enabled_for(tenant: dict) -> bool:
    """Whether this tenant's engines run on their own account."""
    return FLAG and tiers.has(tenant.get("tier"), FEATURE) and accounts.settings(tenant["id"]).get("engine_mode") == "own"


def own_tenants() -> list[dict]:
    if not FLAG:
        return []
    return [t for t in accounts.active_tenants() if enabled_for(t)]


def modes_allowed(tenant: dict | None) -> list[str]:
    return ["mirror", "own"] if FLAG and tenant and tiers.has(tenant.get("tier"), FEATURE) else ["mirror"]


def _books_on(tid: str) -> set[str]:
    from trading import book_switches
    return {b for b in wallets.books_on(tid) if book_switches.enabled(b)}


async def _run(tenant: dict, label: str, coro_fn, *args) -> bool:
    try:
        await coro_fn(*args)
        _state["runs"] += 1
        return True
    except Exception as e:                                               # one engine's trouble never stops a tenant's others
        logger.warning("Tenant %s: %s failed: %s", tenant["id"], label, e)
        _state["errors"] = (_state["errors"] + [{"ts": time.time(), "tenant": tenant["id"], "engine": label, "error": repr(e)[:200]}])[-20:]
        return False


async def tick(now: float | None = None) -> dict:
    """Every minute: manage each opted-in tenant's open legs and run the ticks of the books they have on."""
    if not FLAG:
        return {"skipped": "off"}
    from autotrader import engine as autotrader
    from experiments import books
    from trading import monitor
    now = now or time.time()
    ran = {}
    for tenant in own_tenants():
        tid = tenant["id"]
        with context.as_tenant(tid):
            on = _books_on(tid)
            n = 0
            for name in on:
                if name == "auto":
                    n += await _run(tenant, "autotrader.monitor", autotrader.monitor_now)
                elif name in books.WALLETS:
                    n += await _run(tenant, f"monitor:{name}", monitor.run, books.WALLETS[name])
            for label, its_books, ref in BOOK_TICKS:
                if on & set(its_books):
                    n += await _run(tenant, label, _load(ref))
            ran[tid] = n
            _state["per_tenant"][tid] = {"ts": now, "engines": n, "books": sorted(on)}
    _state["last_tick"] = now
    _state["ticks"] += 1
    return {"tenants": ran}


async def on_scans(scans: list[dict], watched: set[str], global_market: dict | None) -> int:
    """After each scanner sweep: the autotrader's entry decision, once per opted-in tenant that runs it."""
    if not FLAG or not scans:
        return 0
    from autotrader import engine as autotrader
    n = 0
    for tenant in own_tenants():
        with context.as_tenant(tenant["id"]):
            if "auto" in _books_on(tenant["id"]):
                n += await _run(tenant, "autotrader.on_scans", autotrader.on_scans, scans, watched, global_market)
    return n


def status() -> dict:
    return {"flag": FLAG, "feature": FEATURE, "tenants_own": [t["id"] for t in own_tenants()], "last_tick": _state["last_tick"],
            "ticks": _state["ticks"], "runs": _state["runs"], "errors": _state["errors"][-10:], "per_tenant": _state["per_tenant"]}
