"""Copy SQLite databases into Postgres, file → schema, table → table (storage/engine.py's mapping).

    DATABASE_URL=postgresql://… python scripts/sqlite_to_postgres.py data/state.db data/swing_trading.db
    DATABASE_URL=postgresql://… python scripts/sqlite_to_postgres.py --all          # every *.db under backend/data

The tables are created from the SQLite DDL (translated like the app's own schemas), rows are copied in batches with
ON CONFLICT DO NOTHING, so the script can be re-run to top up after a first pass (take the app down for the final one:
rows written to SQLite after the copy would be missed). Indexes are recreated afterwards.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from storage import engine  # noqa: E402

BATCH = 5_000


def copy(path: str) -> None:
    if not engine.enabled():
        sys.exit("DATABASE_URL is not set")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    dst = engine.connect(path)
    schema = dst.schema
    tables = [(n, sql) for n, sql in src.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    indexes = [sql for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")]
    print(f"{path} → schema {schema}: {len(tables)} tables")
    for name, ddl in tables:
        dst.execute(engine.translate_ddl(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1)))
        dst.commit()
        cols = [r[1] for r in src.execute(f'PRAGMA table_info("{name}")')]
        total = src.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        placeholders = ",".join("?" * len(cols))
        insert = f'INSERT OR IGNORE INTO {name} ({",".join(cols)}) VALUES ({placeholders})'
        done, t0 = 0, time.time()
        cur = src.execute(f'SELECT {",".join(cols)} FROM "{name}"')
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            dst.executemany(insert, [tuple(r) for r in rows])
            dst.commit()
            done += len(rows)
            print(f"  {name}: {done}/{total}", end="\r", flush=True)
        print(f"  {name}: {done}/{total} rows in {time.time() - t0:.1f}s")
        # serial columns: move the sequence past the copied ids
        for c in cols:
            if c in ("id", "scan_id"):
                try:
                    dst.execute(f"SELECT setval(pg_get_serial_sequence('{schema}.{name}', '{c}'), COALESCE((SELECT MAX({c}) FROM {name}), 0) + 1, false)")
                    dst.commit()
                except Exception:
                    dst.rollback()
    for ddl in indexes:
        try:
            dst.execute(engine.translate_ddl(ddl.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1).replace("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS", 1)))
            dst.commit()
        except Exception as e:
            dst.rollback()
            print(f"  index skipped: {e}")
    dst.close()
    src.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--all"]:
        args = sorted(os.path.join(engine.DATA_DIR, f) for f in os.listdir(engine.DATA_DIR) if f.endswith(".db"))
    if not args:
        sys.exit(__doc__)
    for p in args:
        copy(p)
