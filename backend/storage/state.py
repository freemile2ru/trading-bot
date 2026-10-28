"""Durable coordination state (SQLite) — what used to live in Redis but isn't a cache.

Watchlist, cooldowns, daily budgets, Claude approval keys, autotrader per-symbol states,
the latest scan snapshot. Mirrors the subset of the Redis helper API those callers used
(get/set with TTL, delete, hashes, acquire_once, incr_with_expire, counter), so moving a
caller is a one-word change — and none of it is lost when Redis restarts or is down.

Expiry is enforced on read (an expired row is invisible) and swept periodically.
Operations are sub-millisecond, so they run inline under a lock rather than in threads.
"""

import json
import threading
import time
from typing import Any

from storage.db import STATE_DB, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL);
CREATE TABLE IF NOT EXISTS hashes (key TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
                                   PRIMARY KEY (key, field));
CREATE INDEX IF NOT EXISTS ix_kv_exp ON kv(expires_at);
"""

_lock = threading.Lock()
_con = None
_path = STATE_DB
_last_sweep = 0.0


def _db():
    global _con
    if _con is None:
        _con = connect(_path)
        _con.executescript(SCHEMA)
        _con.commit()
    return _con


def use(path: str) -> None:
    """Point at a different database (tests)."""
    global _con, _path
    with _lock:
        if _con is not None:
            _con.close()
        _con, _path = None, path


def key(*parts: str) -> str:
    return "state:" + ":".join(str(p) for p in parts)


def _sweep(con, now: float) -> None:
    global _last_sweep
    if now - _last_sweep > 300:
        con.execute("DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
        _last_sweep = now


# ── Sync core ──────────────────────────────────────────────────────────────────

def get_sync(k: str) -> Any | None:
    with _lock:
        row = _db().execute("SELECT value, expires_at FROM kv WHERE key=?", (k,)).fetchone()
    if not row or (row[1] is not None and row[1] <= time.time()):
        return None
    return json.loads(row[0])


def set_sync(k: str, value: Any, ttl: float | None = None) -> None:
    now = time.time()
    with _lock:
        con = _db()
        con.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                    (k, json.dumps(value, default=str), now + ttl if ttl else None))
        _sweep(con, now)
        con.commit()


def delete_sync(*keys: str) -> None:
    if not keys:
        return
    with _lock:
        con = _db()
        con.executemany("DELETE FROM kv WHERE key=?", [(k,) for k in keys])
        con.executemany("DELETE FROM hashes WHERE key=?", [(k,) for k in keys])
        con.commit()


# ── Async API (same names as data/cache.py) ────────────────────────────────────

async def get(k: str) -> Any | None:
    return get_sync(k)


async def set(k: str, value: Any, ttl: float | None = None) -> None:
    set_sync(k, value, ttl)


async def delete(*keys: str) -> None:
    delete_sync(*keys)


async def hgetall(k: str) -> dict:
    with _lock:
        rows = _db().execute("SELECT field, value FROM hashes WHERE key=?", (k,)).fetchall()
    return {f: json.loads(v) for f, v in rows}


async def hset(k: str, field: str, value: Any) -> None:
    with _lock:
        con = _db()
        con.execute("INSERT OR REPLACE INTO hashes VALUES (?,?,?)", (k, field, json.dumps(value, default=str)))
        con.commit()


async def hdel(k: str, *fields: str) -> None:
    if not fields:
        return
    with _lock:
        con = _db()
        con.executemany("DELETE FROM hashes WHERE key=? AND field=?", [(k, f) for f in fields])
        con.commit()


async def acquire_once(k: str, ttl: float) -> bool:
    """Atomic claim (like SET NX EX): True only if no live value exists."""
    now = time.time()
    with _lock:
        con = _db()
        row = con.execute("SELECT expires_at FROM kv WHERE key=?", (k,)).fetchone()
        if row and (row[0] is None or row[0] > now):
            return False
        con.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (k, json.dumps(int(now)), now + ttl))
        con.commit()
        return True


async def incr_with_expire(k: str, ttl: float) -> int:
    now = time.time()
    with _lock:
        con = _db()
        row = con.execute("SELECT value, expires_at FROM kv WHERE key=?", (k,)).fetchone()
        live = row and (row[1] is None or row[1] > now)
        n = (json.loads(row[0]) if live else 0) + 1
        expires = row[1] if live and row[1] else now + ttl
        con.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (k, json.dumps(n), expires))
        con.commit()
        return n


async def counter(k: str) -> int:
    v = get_sync(k)
    return int(v) if v else 0


async def healthy() -> bool:
    try:
        with _lock:
            _db().execute("SELECT 1")
        return True
    except Exception:
        return False
