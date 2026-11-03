"""storage/engine.py: the SQLite dialect the modules write, carried onto Postgres. The translation tests run always;
the round trips need TEST_DATABASE_URL (CI runs the whole suite on Postgres too)."""
import os

import pytest

from storage import engine


def test_placeholders_skip_string_literals():
    assert engine.placeholders("SELECT ? FROM t WHERE a='x?y' AND b=?") == "SELECT %s FROM t WHERE a='x?y' AND b=%s"
    assert engine.placeholders("INSERT INTO t VALUES (?,?,?)") == "INSERT INTO t VALUES (%s,%s,%s)"


def test_ddl_translation_covers_what_the_schemas_use():
    assert engine.translate_ddl("id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL") == "id BIGSERIAL PRIMARY KEY, ts DOUBLE PRECISION NOT NULL"
    assert engine.translate_ddl("scan_id INTEGER PRIMARY KEY, p0 REAL") == "scan_id BIGSERIAL PRIMARY KEY, p0 DOUBLE PRECISION"
    assert engine.translate_ddl("PRIMARY KEY (label, id)) WITHOUT ROWID") == "PRIMARY KEY (label, id)) "
    assert engine.translate_ddl("blob BLOB, label TEXT, funding_through_ms INTEGER") == "blob BYTEA, label TEXT, funding_through_ms BIGINT"
    assert engine.split_script("CREATE TABLE a (x INT);\n\nCREATE INDEX IF NOT EXISTS i ON a(x);\n") == ["CREATE TABLE a (x INT)", "CREATE INDEX IF NOT EXISTS i ON a(x)"]


def test_schema_names_are_the_file_stem_in_the_data_dir_and_hashed_elsewhere(tmp_path):
    assert engine.schema_for(os.path.join(engine.DATA_DIR, "swing_trading.db")) == "swing_trading"
    a, b = engine.schema_for(str(tmp_path / "state.db")), engine.schema_for(str(tmp_path / "sub" / "state.db"))
    assert a.startswith("state_") and b.startswith("state_") and a != b


def test_named_placeholders_scalar_minmax_and_boolean_sums():
    assert engine.named_placeholders("INSERT INTO e (id, t) VALUES (:id, :t) -- 'at 12:30'") == "INSERT INTO e (id, t) VALUES (%(id)s, %(t)s) -- 'at 12:30'"
    assert engine.scalar_minmax("UPDATE e SET best=MAX(best, ?), worst=MIN(worst, ?), n=MAX(COALESCE(newest, 0), COALESCE(?, 0)) WHERE id=?") \
        == "UPDATE e SET best=GREATEST(best, ?), worst=LEAST(worst, ?), n=GREATEST(COALESCE(newest, 0), COALESCE(?, 0)) WHERE id=?"
    assert engine.scalar_minmax("SELECT MAX(fired_at) AS last, MIN(seen) FROM t") == "SELECT MAX(fired_at) AS last, MIN(seen) FROM t"
    assert engine.bool_sums("SELECT SUM(action = 'BUY') AS buys, SUM(x), SUM(fired_at >= ?) AS d1 FROM t") \
        == "SELECT SUM(CASE WHEN action = 'BUY' THEN 1 ELSE 0 END) AS buys, SUM(x), SUM(CASE WHEN fired_at >= ? THEN 1 ELSE 0 END) AS d1 FROM t"
    assert engine.placeholders("SELECT 1 FROM t WHERE source LIKE 'Binance%' AND a=? AND b LIKE ?") == "SELECT 1 FROM t WHERE source LIKE 'Binance%%' AND a=%s AND b LIKE %s"


def test_booleans_become_integers_like_sqlite():
    assert engine._params([True, False, 1.5, None, "x"]) == [1, 0, 1.5, None, "x"]


pg = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")


@pg
def test_postgres_round_trip_with_the_sqlite_dialect(tmp_path):
    con = engine.connect(str(tmp_path / "rt.db"))
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL);
        CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, flag INTEGER);
        CREATE INDEX IF NOT EXISTS ix_kv ON kv(expires_at);
    """)
    con.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", ("a", "1", None))
    con.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", ("a", "2", 5.0))            # replaces
    con.execute("INSERT OR IGNORE INTO kv VALUES (?,?,?)", ("a", "3", None))             # ignored
    con.commit()
    assert con.execute("SELECT value, expires_at FROM kv WHERE key=?", ("a",)).fetchone() == ("2", 5.0)
    cur = con.execute("INSERT INTO log (ts, flag) VALUES (?,?)", (1.0, True))             # a bool into INTEGER
    assert cur.lastrowid == 1
    con.executemany("INSERT INTO log (ts, flag) VALUES (?,?)", [(2.0, False), (3.0, True)])
    with con:                                                                             # a transaction, not a close
        con.execute("DELETE FROM log WHERE flag=?", (0,))
    rows = con.execute("SELECT id, flag FROM log ORDER BY id").fetchall()
    assert rows == [(1, 1), (3, 1)] and [d[0] for d in con.execute("SELECT id, ts FROM log").description] == ["id", "ts"]
    assert con.execute("DELETE FROM log WHERE id > ?", (1,)).rowcount == 1
    with pytest.raises(Exception):
        con.execute("ALTER TABLE log ADD COLUMN ts REAL")                                  # duplicate column
    assert con.execute("SELECT COUNT(*) FROM log").fetchone()[0] == 1                      # the connection still works
    con.close()


@pg
def test_the_kv_store_and_a_paper_wallet_work_on_postgres(tmp_path):
    from storage import state as kv
    from trading.paper_trader import PaperWallet
    kv.use(str(tmp_path / "state.db"))
    kv.set_sync("k", {"a": 1}, 60)
    assert kv.get_sync("k") == {"a": 1}
    w = PaperWallet(str(tmp_path / "w.db"), label="pg", initial_balance=1_000)
    t = w.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1, timeframe="1h", extra={"exit_policy": "manual"})
    assert not t.get("error")
    w.close_trade_manually(t["id"], 102.0, "test")
    p = w.get_portfolio()
    assert p["open_trades"] == [] and len(w.all_trades()["closed"]) == 1 and p["balance"] > 1_000
