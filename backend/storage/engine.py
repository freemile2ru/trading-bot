"""One switch for every database the app opens through storage.db.connect().

  no DATABASE_URL   SQLite files under backend/data — local development and the tests, exactly as before
  DATABASE_URL      PostgreSQL (production): each SQLite file becomes a schema of its own in the one database
                    (state.db → schema "state", swing_trading.db → "swing_trading", …) so the tables keep their
                    names and the migration tool (scripts/sqlite_to_postgres.py) maps file → schema 1:1.

The modules keep writing the SQLite dialect they were written in; PgConnection rewrites what differs on the way
through — `?` placeholders, INSERT OR REPLACE / OR IGNORE (→ ON CONFLICT), SQLite's column types and AUTOINCREMENT
in DDL, PRAGMAs — and gives the sqlite3 connection API the callers use: execute/executemany/executescript returning
cursors, commit/rollback, `with con:` as a transaction (NOT a close, which is what psycopg's own context manager
does), rowcount, lastrowid, description. Statements outside a `with con:` block autocommit, so a caught error (the news store's "ALTER TABLE … ADD COLUMN"
probes) never poisons anyone else's work; one shared connection per process serves every schema (see _Shared).

Staged migration (2026-11-03, with the user): the callers of storage.db.connect() move first — the kv state, the
paper and live wallets, signals, the pair books, the news store; the modules that open sqlite3 themselves (copy
log, cascade, positioning, funding venues, the research labs) follow book by book.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
from typing import Any, Iterable

logger = logging.getLogger("storage.engine")

DATABASE_URL = os.getenv("DATABASE_URL") or None
DATA_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "data"))


def enabled() -> bool:
    return bool(DATABASE_URL)


def schema_for(path: str) -> str:
    """The Postgres schema a SQLite path maps to: the file's stem for the app's own data directory, the stem plus a
    hash of the path anywhere else (tests point every module at throwaway files — each gets its own schema)."""
    real = os.path.realpath(path)
    stem = re.sub(r"[^a-z0-9_]", "_", os.path.splitext(os.path.basename(real))[0].lower()) or "db"
    if os.path.dirname(real) == DATA_DIR:
        return stem
    return f"{stem}_{hashlib.sha1(real.encode()).hexdigest()[:8]}"


def connect(path: str) -> Any:
    """A connection for the database that lives at `path` under SQLite — on Postgres when DATABASE_URL is set."""
    if not enabled():
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        con = sqlite3.connect(path, timeout=10, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=10000")
        return con
    return PgConnection(DATABASE_URL, schema_for(path))


# ── SQL translation ───────────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"'(?:[^']|'')*'|\?")
_INSERT = re.compile(r"^\s*INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
# Type keywords are matched case-sensitively: the schemas write types in upper case and columns in lower case
# (a column named `blob` or `real` must stay what it is).
_DDL_TYPES = (
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b"), "BIGSERIAL PRIMARY KEY"),
    (re.compile(r"\b(\w+)\s+INTEGER\s+PRIMARY\s+KEY\b(?!\s*\()"), r"\1 BIGSERIAL PRIMARY KEY"),
    (re.compile(r"\bINTEGER\b"), "BIGINT"),                    # SQLite's INTEGER is 64-bit (epoch milliseconds live in them)
    (re.compile(r"\bREAL\b"), "DOUBLE PRECISION"),
    (re.compile(r"\bBLOB\b"), "BYTEA"),
    (re.compile(r"\bWITHOUT\s+ROWID\b"), ""),
)
_NAMED = re.compile(r"'(?:[^']|'')*'|:([A-Za-z_]\w*)")
_BOOL_SUM = re.compile(r"\bSUM\(\s*([A-Za-z_]\w*\s*(?:=|>=|<=|<>|!=|>|<)\s*(?:'[^']*'|\?|[\w.]+))\s*\)", re.IGNORECASE)
_PERCENT = re.compile(r"'(?:[^']|'')*'|%")


def named_placeholders(sql: str) -> str:
    """`:name` → `%(name)s` (sqlite3's named style with a dict of parameters), string literals kept (`%` escaped)."""
    return _NAMED.sub(lambda m: f"%({m.group(1)})s" if m.group(1) else _escape_percent(m.group(0)), _escape_outside(sql))


def scalar_minmax(sql: str) -> str:
    """SQLite's two-argument MAX(a, b) / MIN(a, b) are GREATEST / LEAST on Postgres; the aggregates stay."""
    out, i = [], 0
    while True:
        m = re.search(r"\b(MAX|MIN)\(", sql[i:], re.IGNORECASE)
        if not m:
            out.append(sql[i:])
            break
        start = i + m.start()
        j, depth, commas, quoted = i + m.end(), 1, 0, False
        while j < len(sql) and depth:
            ch = sql[j]
            if ch == "'":
                quoted = not quoted
            elif not quoted:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "," and depth == 1:
                    commas += 1
            j += 1
        fn = ("GREATEST" if m.group(1).upper() == "MAX" else "LEAST") if commas else m.group(1)
        out.append(sql[i:start] + fn + "(")
        i = i + m.end()
    return "".join(out)


def bool_sums(sql: str) -> str:
    """SUM(col = 'x') counts matches on SQLite; Postgres cannot sum booleans."""
    return _BOOL_SUM.sub(lambda m: f"SUM(CASE WHEN {m.group(1)} THEN 1 ELSE 0 END)", sql)


def _escape_percent(sql: str) -> str:
    """A literal % (LIKE 'Binance%') must be %% once psycopg's %s placeholders are in play — inside literals too."""
    return sql.replace("%", "%%")


def placeholders(sql: str) -> str:
    """`?` → `%s`, string literals kept (their `%` escaped)."""
    return _PLACEHOLDER.sub(lambda m: "%s" if m.group(0) == "?" else _escape_percent(m.group(0)), _escape_outside(sql))


def _escape_outside(sql: str) -> str:
    """`%` outside string literals → `%%` (a `?` is left for the placeholder pass)."""
    return _PERCENT.sub(lambda m: "%%" if m.group(0) == "%" else m.group(0), sql)


def translate_ddl(sql: str) -> str:
    out = sql
    for rx, rep in _DDL_TYPES:
        out = rx.sub(rep, out)
    return out


def split_script(script: str) -> list[str]:
    """SQLite's executescript takes several statements; Postgres wants them one at a time."""
    return [s.strip() for s in script.split(";") if s.strip()]


class _Column:
    """A DB-API description entry callers index with [0] (sqlite3 returns 7-tuples)."""
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __getitem__(self, i: int):
        return (self.name, None, None, None, None, None, None)[i]

    def __len__(self) -> int:
        return 7


class PgCursor:
    """The slice of sqlite3.Cursor the app uses, over a psycopg cursor."""

    def __init__(self, con: "PgConnection", cur):
        self._con, self._cur = con, cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, n: int = 1):
        return self._cur.fetchmany(n)

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self):
        d = self._cur.description
        return [_Column(c.name) for c in d] if d else None

    @property
    def lastrowid(self):
        """The serial key the last INSERT drew (sqlite3's lastrowid) — lastval() of this session's sequence."""
        try:
            return self._con._pool.run(self._con.schema, lambda raw: raw.execute("SELECT lastval()").fetchone()[0])
        except Exception:
            return None

    def close(self) -> None:
        self._cur.close()


class _Shared:
    """One psycopg connection per process, shared by every schema and every caller under one lock.

    Why: a PaperWallet opens two connections and the app has ~30 wallets plus a connection per module — fine as
    SQLite files, but an RDS db.t4g.micro allows ~80 connections and the test suite opens hundreds of throwaway
    databases. Every statement runs as `SET search_path` + the statement under the lock (autocommit), and a
    `with con:` block holds the lock for the whole transaction. A broken connection is reopened once."""

    def __init__(self, url: str):
        self.url = url
        self.lock = threading.RLock()
        self.raw = None
        self.schemas: set[str] = set()
        self.tables: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
        self.tx_schema: str | None = None                     # the schema whose transaction holds the lock, if any

    def connection(self):
        import psycopg
        if self.raw is None or self.raw.closed:
            self.raw = psycopg.connect(self.url, autocommit=True)
            self.schemas.clear()
        return self.raw

    def use(self, schema: str) -> None:
        raw = self.connection()
        if schema not in self.schemas:
            raw.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            self.schemas.add(schema)
        if self.tx_schema is None or self.tx_schema != schema:
            raw.execute(f'SET search_path TO "{schema}"')

    def in_error(self) -> bool:
        """True while the connection sits in an aborted transaction (every statement fails until ROLLBACK)."""
        from psycopg.pq import TransactionStatus
        return self.raw is not None and not self.raw.closed and self.raw.info.transaction_status == TransactionStatus.INERROR

    def run(self, schema: str, fn, what: str = ""):
        """fn(raw) under the lock, on this schema; a dropped connection is reopened and the call retried once.

        An aborted transaction that no `with con:` block owns is rolled back first: it would otherwise poison every
        later statement on the shared connection (2026-11-04: one swallowed failure inside a wallet snapshot's
        BEGIN…COMMIT took the whole app down with InFailedSqlTransaction until a restart)."""
        import psycopg
        with self.lock:
            if self.tx_schema is None and self.in_error():
                logger.warning("Postgres connection was left in an aborted transaction — rolling it back")
                self.raw.execute("ROLLBACK")
            try:
                self.use(schema)
                return fn(self.connection())
            except psycopg.OperationalError:
                if self.tx_schema is not None or (self.raw is not None and not self.raw.closed):
                    raise
                self.raw = None
                self.use(schema)
                return fn(self.connection())
            except psycopg.Error as e:
                if self.tx_schema is not None:
                    logger.warning("Postgres statement failed inside a transaction on %s (%s): %s", schema, (what or "?")[:160], e)
                raise


_shared: dict[str, _Shared] = {}


def _pool(url: str) -> _Shared:
    if url not in _shared:
        _shared[url] = _Shared(url)
    return _shared[url]


class PgConnection:
    """A handle on one schema over the process's shared connection, with the sqlite3 surface the app uses."""

    def __init__(self, url: str, schema: str):
        self.schema = schema
        self._pool = _pool(url)
        self._tx = None
        self._pool.run(schema, lambda raw: None)              # creates the schema

    @property
    def _raw(self):
        return self._pool.connection()

    # ── the sqlite3 surface ──
    def execute(self, sql: str, params: Iterable[Any] = ()) -> PgCursor:
        word = sql.strip().rstrip(";").upper()
        if word in ("BEGIN", "BEGIN TRANSACTION", "BEGIN DEFERRED", "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"):
            # sqlite3's explicit transaction (a wallet's consistent snapshot) becomes the pool's: the lock is held for
            # its whole span, so no other thread's statement can run — or fail — inside it.
            if self._tx is None:
                self.__enter__()
            return PgCursor(self, None)
        if word in ("COMMIT", "END", "END TRANSACTION", "COMMIT TRANSACTION"):
            if self._tx is not None:
                self.__exit__(None, None, None)
            return PgCursor(self, None)
        if word in ("ROLLBACK", "ROLLBACK TRANSACTION"):
            if self._tx is not None:
                self.__exit__(RuntimeError, RuntimeError("rollback"), None)
            return PgCursor(self, None)
        stmt, params = self._prepare(sql, params)
        if stmt is None:
            return PgCursor(self, self._raw.cursor())

        def go(raw):
            cur = raw.cursor()
            cur.execute(stmt, _params(params))
            return PgCursor(self, cur)
        return self._pool.run(self.schema, go, stmt)

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> PgCursor:
        rows = [_params(p) for p in seq]
        stmt, _ = self._prepare(sql, ())
        if stmt is None or not rows:
            return PgCursor(self, self._raw.cursor())

        def go(raw):
            cur = raw.cursor()
            cur.executemany(stmt, rows)
            return PgCursor(self, cur)
        return self._pool.run(self.schema, go, stmt)

    def executescript(self, script: str) -> PgCursor:
        def go(raw):
            cur = raw.cursor()
            for stmt in split_script(script):
                if stmt.upper().startswith("PRAGMA"):
                    continue
                cur.execute(translate_ddl(stmt))
            self._pool.tables = {k: v for k, v in self._pool.tables.items() if k[0] != self.schema}
            return PgCursor(self, cur)
        return self._pool.run(self.schema, go, "script")

    def cursor(self) -> PgCursor:
        return PgCursor(self, self._raw.cursor())

    def commit(self) -> None:
        pass                                                   # autocommit outside `with con:` blocks

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass                                                   # the connection is the process's; nothing to release

    def __enter__(self) -> "PgConnection":
        self._pool.lock.acquire()
        try:
            self._pool.use(self.schema)
            self._tx = self._raw.transaction()
            self._tx.__enter__()
            self._pool.tx_schema = self.schema
        except Exception:
            self._pool.lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None and self._pool.in_error():
                # a failure inside the block was caught there: the transaction is aborted, so this is a rollback
                exc_type, exc = RuntimeError, RuntimeError("aborted transaction")
            self._tx.__exit__(exc_type, exc, tb)
        finally:
            self._tx = None
            self._pool.tx_schema = None
            if self._pool.in_error():
                self._pool.raw.execute("ROLLBACK")
            self._pool.lock.release()

    # ── translation ──
    def _prepare(self, sql: str, params: Iterable[Any]) -> tuple[str | None, Iterable[Any]]:
        s = sql.strip()
        if s.upper().startswith("PRAGMA"):
            return None, params
        if s[:6].upper() in ("CREATE", "ALTER "):
            s = translate_ddl(s)                               # a schema given to execute() rather than executescript()
        m = _INSERT.match(s)
        if m:
            mode, table = m.group(1).upper(), m.group(2)
            body = s[m.end():]
            s = f"INSERT INTO {table}{body}"
            if mode == "IGNORE":
                s += " ON CONFLICT DO NOTHING"
            else:
                cols, pk = self._table_info(table)
                updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in pk) or None
                s += f" ON CONFLICT ({', '.join(pk)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING")
        s = bool_sums(scalar_minmax(s))
        if isinstance(params, dict):
            return named_placeholders(s), params
        return placeholders(s), params

    def _table_info(self, table: str) -> tuple[list[str], list[str]]:
        key = (self.schema, table)
        if key not in self._pool.tables:
            def go(raw):
                cur = raw.cursor()
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                            (self.schema, table))
                cols = [r[0] for r in cur.fetchall()]
                cur.execute("""SELECT kcu.column_name FROM information_schema.table_constraints tc
                               JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
                               WHERE tc.table_schema=%s AND tc.table_name=%s AND tc.constraint_type='PRIMARY KEY' ORDER BY kcu.ordinal_position""",
                            (self.schema, table))
                return cols, [r[0] for r in cur.fetchall()]
            cols, pk = self._pool.run(self.schema, go)
            if not cols or not pk:
                raise RuntimeError(f"INSERT OR REPLACE into {self.schema}.{table}: the table needs a primary key on Postgres")
            self._pool.tables[key] = (cols, pk)
        return self._pool.tables[key]


def _params(params: Any) -> Any:
    """SQLite stores booleans as integers; Postgres refuses a boolean in an INTEGER column."""
    if isinstance(params, dict):
        return {k: (int(v) if isinstance(v, bool) else v) for k, v in params.items()}
    return [int(p) if isinstance(p, bool) else p for p in params]
