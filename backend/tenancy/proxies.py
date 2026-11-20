"""Tenant-aware stand-ins for the module-level wallets the engines were written against.

Every engine reaches its wallet through a module global — `autotrader.engine.auto_wallet`, `experiments.books.WALLETS[name]`,
`markets.engine.WALLETS[name]`. Replacing those globals with these proxies makes the same engine code act on the
CURRENT TENANT's wallet when the tenant engine host (tenancy/engines.py) runs it inside a tenant's context, and on the
owner's wallet — the very object it always was — otherwise. No engine changes; the indirection is here.
"""
from __future__ import annotations

from typing import Any

from tenancy import context


def _tenant_wallet(label: str):
    from tenancy import wallets
    return wallets.get(context.tenant_id(), label)


class WalletProxy:
    """Forwards every attribute to the owner's wallet, or to the current tenant's wallet of the same label."""

    def __init__(self, owner_wallet: Any, label: str):
        object.__setattr__(self, "_owner", owner_wallet)
        object.__setattr__(self, "_label", label)

    def _resolve(self):
        return self._owner if context.is_owner() else _tenant_wallet(self._label)

    def __getattr__(self, name: str):
        return getattr(self._resolve(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._resolve(), name, value)

    def __repr__(self) -> str:
        return f"<WalletProxy {self._label} → {self._resolve()!r}>"

    @property
    def owner(self):
        return self._owner


class TenantWallets(dict):
    """A {name: wallet} mapping whose lookups resolve to the current tenant's wallets outside the owner context."""

    def __getitem__(self, name: str):
        w = dict.__getitem__(self, name)
        return w if context.is_owner() else _tenant_wallet(name)

    def get(self, name: str, default: Any = None):
        if not dict.__contains__(self, name):
            return default
        return self[name]

    def items(self):
        return [(n, self[n]) for n in dict.keys(self)]

    def values(self):
        return [self[n] for n in dict.keys(self)]

    def owner_items(self):
        return list(dict.items(self))
