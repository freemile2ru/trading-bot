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


def test_a_long_flush_is_bought_an_hour_later_for_four_hours_once(cas):
    db, w = cas
    now = time.time()
    cascade.kv.delete_sync(cascade.PENDING_KEY)
    assert asyncio.run(cascade.tick(now - 300)) == []             # the previous, quiet bucket: nothing
    _burst(db, now, "SELL", 500_000.0)                            # longs liquidated → price flushed down
    assert asyncio.run(cascade.tick(now)) == []                   # noted, not traded: the cascade is still running
    q = cascade.pending()
    assert [(x["symbol"], x["direction"]) for x in q] == [("SOLUSDT", "long")]
    fire = q[0]["fire_at"]
    assert fire == pytest.approx((int(now) // 300 - 1) * 300 + 300 + cascade.ENTRY_DELAY_MIN * 60)
    assert asyncio.run(cascade.tick(fire - 60)) == [] and w.get_portfolio()["open_trades"] == []
    opened = asyncio.run(cascade.tick(fire + 30))
    assert [(t["symbol"], t["direction"], t["rule"]) for t in opened] == [("SOLUSDT", "long", "v2")]
    t = w.get_portfolio()["open_trades"][0]
    assert t["expires_at"] == pytest.approx(fire + 30 + cascade.HOLD_HOURS * 3600) and t["stop_loss"] == pytest.approx(92.0)
    assert cascade.pending() == []
    assert asyncio.run(cascade.tick(now + 60)) == []              # same bucket: not twice
    assert cascade.pending() == []


def test_a_short_squeeze_is_sold_and_a_dead_stream_queues_nothing(cas):
    db, w = cas
    now = time.time()
    cascade.kv.delete_sync(cascade.PENDING_KEY)
    _burst(db, now, "BUY", 500_000.0)
    assert asyncio.run(cascade.tick(now)) == [] and cascade.pending()[0]["direction"] == "short"
    assert [t["direction"] for t in asyncio.run(cascade.tick(cascade.pending()[0]["fire_at"]))] == ["short"]
    cascade.kv.delete_sync(cascade.PENDING_KEY)
    assert asyncio.run(cascade.tick(now + 2 * 3600)) == []        # newest liquidation is 2h old: stream stale
    assert cascade.pending() == []


def test_a_fade_missed_by_more_than_ten_minutes_is_dropped(cas):
    db, w = cas
    now = time.time()
    cascade.kv.delete_sync(cascade.PENDING_KEY)
    _burst(db, now, "SELL", 500_000.0)
    asyncio.run(cascade.tick(now))
    fire = cascade.pending()[0]["fire_at"]
    assert asyncio.run(cascade.tick(fire + 900)) == []            # the server was down at the time: no late entry
    assert cascade.pending() == [] and w.get_portfolio()["open_trades"] == []
