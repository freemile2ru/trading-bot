import asyncio
import sqlite3
import time

import pytest

from experiments import books, cascade
from scalper import config as C
from trading.paper_trader import PaperWallet


@pytest.fixture
def cas(tmp_path, monkeypatch):
    monkeypatch.setattr(books, "ENABLED", True)
    db = str(tmp_path / "market.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE liquidations (ts REAL, symbol TEXT, side TEXT, price REAL, avg_price REAL, qty REAL, notional REAL)")
    now = time.time()
    rows = []
    for d in range(30 * 288):                                   # 30 days of quiet 5-minute buckets at $1k
        rows.append((now - d * 300 - 10, "SOLUSDT", "SELL", 100.0, 100.0, 10, 1000.0))
    con.executemany("INSERT INTO liquidations VALUES (?,?,?,?,?,?,?)", rows)
    con.commit(); con.close()
    monkeypatch.setattr(C, "MARKET_DB", db)
    monkeypatch.setattr(C, "SYMBOLS", ["SOLUSDT"])
    monkeypatch.setattr(cascade, "_thresholds", {"at": 0.0, "by": {}})
    w = PaperWallet(str(tmp_path / "w.db"), label="cascade")
    w.reset_portfolio(10_000)
    monkeypatch.setattr(books, "WALLETS", {**books.WALLETS, "cascade": w})

    async def price(symbol):
        return 100.0
    monkeypatch.setattr(cascade.monitor, "live_price", price)
    return db, w


def _burst(db, now, side, notional):
    start = (int(now) // 300 - 1) * 300 + 30                    # inside the last completed bucket
    con = sqlite3.connect(db)
    con.execute("INSERT INTO liquidations VALUES (?,?,?,?,?,?,?)", (start, "SOLUSDT", side, 100.0, 100.0, 1, notional))
    con.execute("INSERT INTO liquidations VALUES (?,?,?,?,?,?,?)", (now - 5, "SOLUSDT", "BUY", 100.0, 100.0, 1, 1.0))   # stream alive
    con.commit(); con.close()


def test_a_long_flush_is_bought_for_four_hours_once(cas):
    db, w = cas
    now = time.time()
    assert asyncio.run(cascade.tick(now - 300)) == []             # the previous, quiet bucket: nothing
    _burst(db, now, "SELL", 500_000.0)                            # longs liquidated → price flushed down
    opened = asyncio.run(cascade.tick(now))
    assert [(t["symbol"], t["direction"]) for t in opened] == [("SOLUSDT", "long")]
    t = w.get_portfolio()["open_trades"][0]
    assert t["expires_at"] == pytest.approx(now + cascade.HOLD_HOURS * 3600) and t["stop_loss"] == pytest.approx(97.0)
    assert asyncio.run(cascade.tick(now + 60)) == []              # same bucket: not twice


def test_a_short_squeeze_is_sold_and_a_dead_stream_does_nothing(cas):
    db, w = cas
    now = time.time()
    _burst(db, now, "BUY", 500_000.0)
    assert [t["direction"] for t in asyncio.run(cascade.tick(now))] == ["short"]
    assert asyncio.run(cascade.tick(now + 2 * 3600)) == []        # newest liquidation is 2h old: stream stale
