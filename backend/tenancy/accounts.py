"""Users, tenants, settings, exchange keys and billing events — accounts.db (a schema of its own on Postgres).

A tenant is a paying unit: one owner, their books and wallets, one Stripe customer. A user signs in with Google and
belongs to one tenant. The OWNER tenant is this instance's own data; the admins (OWNER_EMAILS) belong to it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time

from storage.db import ACCOUNTS_DB, connect
from tenancy import context, tiers

logger = logging.getLogger("tenancy.accounts")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'free', status TEXT NOT NULL DEFAULT 'active',
    owner_user_id TEXT, stripe_customer_id TEXT, stripe_subscription_id TEXT, current_period_end REAL,
    cancel_at_period_end INTEGER DEFAULT 0, created_ts REAL NOT NULL, settings TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, google_sub TEXT UNIQUE, name TEXT, picture TEXT,
    tenant_id TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'owner', created_ts REAL NOT NULL, last_login_ts REAL
);
CREATE TABLE IF NOT EXISTS exchange_keys (
    tenant_id TEXT NOT NULL, venue TEXT NOT NULL, api_key_enc TEXT NOT NULL, api_secret_enc TEXT NOT NULL,
    label TEXT, created_ts REAL NOT NULL, PRIMARY KEY (tenant_id, venue)
);
CREATE TABLE IF NOT EXISTS billing_events (id TEXT PRIMARY KEY, type TEXT NOT NULL, tenant_id TEXT, ts REAL NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS ix_users_tenant ON users(tenant_id);
CREATE INDEX IF NOT EXISTS ix_tenants_customer ON tenants(stripe_customer_id);
"""

OWNER_EMAILS = {e.strip().lower() for e in os.getenv("OWNER_EMAILS", "").split(",") if e.strip()}
STATUSES = ("active", "trialing", "past_due", "canceled")
PAYING = ("active", "trialing", "past_due")              # past_due keeps the lights on until Stripe gives up

_lock = threading.RLock()
_con = None
_path = ACCOUNTS_DB


def _db():
    global _con
    if _con is None:
        _con = connect(_path)
        _con.executescript(SCHEMA)
        _con.commit()
        _ensure_owner(_con)
    return _con


def use(path: str) -> None:
    """Point at a different database (tests)."""
    global _con, _path
    with _lock:
        if _con is not None:
            _con.close()
        _con, _path = None, path


def _ensure_owner(con) -> None:
    row = con.execute("SELECT id FROM tenants WHERE id=?", (context.OWNER,)).fetchone()
    if not row:
        con.execute("INSERT INTO tenants (id, name, tier, status, created_ts) VALUES (?,?,?,?,?)",
                    (context.OWNER, "Owner", tiers.ELITE, "active", time.time()))
        con.commit()


def _row(cur) -> dict | None:
    r = cur.fetchone()
    if r is None:
        return None
    return dict(zip([d[0] for d in cur.description], r))


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _tenant_out(t: dict | None) -> dict | None:
    if t is None:
        return None
    t = dict(t)
    t["settings"] = json.loads(t.get("settings") or "{}")
    t["cancel_at_period_end"] = bool(t.get("cancel_at_period_end"))
    return t


# ── Tenants ───────────────────────────────────────────────────────────────────

def tenant(tenant_id: str) -> dict | None:
    with _lock:
        return _tenant_out(_row(_db().execute("SELECT * FROM tenants WHERE id=?", (tenant_id,))))


def tenants() -> list[dict]:
    with _lock:
        return [_tenant_out(t) for t in _rows(_db().execute("SELECT * FROM tenants ORDER BY created_ts"))]


def active_tenants() -> list[dict]:
    """Subscribers whose books run: a paying tier in a paying status, never the owner (its engines are the source)."""
    return [t for t in tenants() if t["id"] != context.OWNER and t["tier"] != tiers.FREE and t["status"] in PAYING]


def set_tier(tenant_id: str, tier: str, status: str = "active", **stripe) -> dict:
    if tier not in tiers.TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with _lock:
        con = _db()
        sets, vals = ["tier=?", "status=?"], [tier, status]
        for k in ("stripe_customer_id", "stripe_subscription_id", "current_period_end", "cancel_at_period_end"):
            if k in stripe:
                sets.append(f"{k}=?")
                vals.append(int(bool(stripe[k])) if k == "cancel_at_period_end" else stripe[k])
        con.execute(f"UPDATE tenants SET {', '.join(sets)} WHERE id=?", (*vals, tenant_id))
        con.commit()
    logger.info("Tenant %s → %s (%s)", tenant_id, tier, status)
    return tenant(tenant_id)


def set_customer(tenant_id: str, customer_id: str) -> None:
    with _lock:
        con = _db()
        con.execute("UPDATE tenants SET stripe_customer_id=? WHERE id=?", (customer_id, tenant_id))
        con.commit()


def tenant_by_customer(customer_id: str) -> dict | None:
    with _lock:
        return _tenant_out(_row(_db().execute("SELECT * FROM tenants WHERE stripe_customer_id=?", (customer_id,))))


def settings(tenant_id: str) -> dict:
    """The tenant's own settings with the tier's defaults filled in."""
    t = tenant(tenant_id) or {"tier": tiers.FREE, "settings": {}}
    lim = tiers.limits(t["tier"])
    s = {"risk_x": 1.0, "paper_capital_usd": lim["paper_capital_usd"], "engine_mode": "mirror",
         "books": {b: (b in tiers.DEFAULT_BOOKS_ON) for b in tiers.books_allowed(t["tier"])}}
    s.update({k: v for k, v in (t.get("settings") or {}).items() if k in ("risk_x", "paper_capital_usd", "books", "engine_mode")})
    s["books"] = {b: bool(v) for b, v in s["books"].items() if b in tiers.ALL_BOOKS}
    return s


def update_settings(tenant_id: str, **changes) -> dict:
    t = tenant(tenant_id)
    if t is None:
        raise KeyError(tenant_id)
    s = settings(tenant_id)
    allowed = tiers.books_allowed(t["tier"])
    lim = tiers.limits(t["tier"])
    if "risk_x" in changes and changes["risk_x"] is not None:
        s["risk_x"] = max(0.25, min(2.0, float(changes["risk_x"])))
    if "paper_capital_usd" in changes and changes["paper_capital_usd"] is not None:
        cap = float(changes["paper_capital_usd"])
        s["paper_capital_usd"] = max(1_000.0, min(cap, lim["paper_capital_usd"] * 2 if lim["paper_capital_usd"] else 0.0))
    if changes.get("engine_mode"):
        from tenancy import engines
        mode = changes["engine_mode"]
        if mode not in engines.MODES:
            raise ValueError(f"unknown engine mode {mode!r}")
        if mode not in engines.modes_allowed(t):
            raise ValueError("running the engines on your own account needs the Elite plan and the tenant-engines flag")
        s["engine_mode"] = mode
    if "books" in changes and changes["books"]:
        for b, on in changes["books"].items():
            if b in allowed:
                s["books"][b] = bool(on)
        on_books = [b for b, v in s["books"].items() if v]
        if lim["paper_books"] is not None and len(on_books) > lim["paper_books"]:
            raise ValueError(f"the {t['tier']} plan runs up to {lim['paper_books']} books; {len(on_books)} are on")
    with _lock:
        con = _db()
        con.execute("UPDATE tenants SET settings=? WHERE id=?", (json.dumps(s), tenant_id))
        con.commit()
    return settings(tenant_id)


# ── Users ─────────────────────────────────────────────────────────────────────

def user(user_id: str) -> dict | None:
    with _lock:
        return _row(_db().execute("SELECT * FROM users WHERE id=?", (user_id,)))


def user_by_email(email: str) -> dict | None:
    with _lock:
        return _row(_db().execute("SELECT * FROM users WHERE email=?", (email.lower(),)))


def users_of(tenant_id: str) -> list[dict]:
    with _lock:
        return _rows(_db().execute("SELECT * FROM users WHERE tenant_id=? ORDER BY created_ts", (tenant_id,)))


def count_users() -> int:
    with _lock:
        return _db().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def sign_in(email: str, name: str | None = None, picture: str | None = None, google_sub: str | None = None) -> dict:
    """Find or create the user (and their tenant). OWNER_EMAILS become admins on the owner tenant; so does the very
    first local dev sign-in when no admin is configured, so the instance's owner is never locked out of it."""
    email = email.lower().strip()
    now = time.time()
    with _lock:
        con = _db()
        u = _row(con.execute("SELECT * FROM users WHERE email=?", (email,)))
        if u:
            con.execute("UPDATE users SET name=COALESCE(?, name), picture=COALESCE(?, picture), google_sub=COALESCE(?, google_sub), last_login_ts=? WHERE id=?",
                        (name, picture, google_sub, now, u["id"]))
            con.commit()
            return user(u["id"])
        admin = email in OWNER_EMAILS or (not OWNER_EMAILS and count_users() == 0 and not _production())
        uid = secrets.token_hex(8)
        if admin:
            tid, role = context.OWNER, "admin"
        else:
            tid, role = secrets.token_hex(6), "owner"
            con.execute("INSERT INTO tenants (id, name, tier, status, owner_user_id, created_ts, settings) VALUES (?,?,?,?,?,?,?)",
                        (tid, name or email.split("@")[0], tiers.FREE, "active", uid, now, "{}"))
        con.execute("INSERT INTO users (id, email, google_sub, name, picture, tenant_id, role, created_ts, last_login_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uid, email, google_sub, name, picture, tid, role, now, now))
        con.commit()
    logger.info("New user %s on tenant %s (%s)", email, tid, role)
    return user(uid)


def _production() -> bool:
    return os.getenv("APP_ENV") == "production"


# ── Exchange keys (encrypted at rest) ──────────────────────────────────────────

def _fernet():
    from cryptography.fernet import Fernet
    from tenancy.sessions import secret
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("keys:" + secret()).encode()).digest()))


def set_exchange_key(tenant_id: str, venue: str, api_key: str, api_secret: str) -> None:
    f = _fernet()
    with _lock:
        con = _db()
        con.execute("INSERT OR REPLACE INTO exchange_keys (tenant_id, venue, api_key_enc, api_secret_enc, label, created_ts) VALUES (?,?,?,?,?,?)",
                    (tenant_id, venue, f.encrypt(api_key.encode()).decode(), f.encrypt(api_secret.encode()).decode(),
                     f"…{api_key[-4:]}" if len(api_key) >= 4 else "set", time.time()))
        con.commit()


def delete_exchange_key(tenant_id: str, venue: str) -> None:
    with _lock:
        con = _db()
        con.execute("DELETE FROM exchange_keys WHERE tenant_id=? AND venue=?", (tenant_id, venue))
        con.commit()


def exchange_keys(tenant_id: str) -> dict:
    """What the UI may see: configured flags and a masked label, never the secret."""
    with _lock:
        rows = _rows(_db().execute("SELECT venue, label FROM exchange_keys WHERE tenant_id=?", (tenant_id,)))
    return {r["venue"]: {"configured": True, "label": r["label"] or "set"} for r in rows}


def exchange_secret(tenant_id: str, venue: str) -> tuple[str, str] | None:
    """The decrypted pair — for the execution layer only, never a route."""
    with _lock:
        r = _row(_db().execute("SELECT api_key_enc, api_secret_enc FROM exchange_keys WHERE tenant_id=? AND venue=?", (tenant_id, venue)))
    if not r:
        return None
    f = _fernet()
    return f.decrypt(r["api_key_enc"].encode()).decode(), f.decrypt(r["api_secret_enc"].encode()).decode()


# ── Billing events (idempotency) ───────────────────────────────────────────────

def record_event(event_id: str, type_: str, tenant_id: str | None, data: dict | None = None) -> bool:
    """True when the event is new; False when it was already handled."""
    with _lock:
        con = _db()
        cur = con.execute("INSERT OR IGNORE INTO billing_events (id, type, tenant_id, ts, data) VALUES (?,?,?,?,?)",
                          (event_id, type_, tenant_id, time.time(), json.dumps(data or {})[:4000]))
        con.commit()
        return cur.rowcount == 1
