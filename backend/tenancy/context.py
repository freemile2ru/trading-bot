"""Who is asking. The middleware sets the tenant and user for a request; engine loops run with nothing set and are
the owner — the instance's own data, exactly as before accounts existed."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

OWNER = "owner"                                   # the tenant that is this instance's own data

_tenant: ContextVar[str | None] = ContextVar("tenant", default=None)
_user: ContextVar[dict | None] = ContextVar("user", default=None)


def tenant_id() -> str:
    return _tenant.get() or OWNER


def is_owner() -> bool:
    return tenant_id() == OWNER


def user() -> dict | None:
    return _user.get()


def is_admin() -> bool:
    u = _user.get()
    return bool(u and u.get("role") == "admin")


def set_current(tenant: str | None, user_: dict | None):
    """Set both for the current context; returns the tokens for reset()."""
    return _tenant.set(tenant), _user.set(user_)


def reset(tokens) -> None:
    t, u = tokens
    _tenant.reset(t)
    _user.reset(u)


@contextmanager
def as_tenant(tenant: str, user_: dict | None = None):
    tokens = set_current(tenant, user_)
    try:
        yield
    finally:
        reset(tokens)
