"""Storage: SQLite state (TTL semantics), signals DB, wallet persistence and JSON import."""
import pytest

import asyncio
import json
import time

from storage import signals, state
from trading.paper_trader import PaperWallet


def run(coro):
    return asyncio.run(coro)


def test_state_ttl_and_atomic_claim():
    run(state.set("a", {"x": 1}, ttl=0.05))
    assert run(state.get("a")) == {"x": 1}
    time.sleep(0.06)
    assert run(state.get("a")) is None                                   # expired → invisible
    assert run(state.acquire_once("lock", 60)) is True
    assert run(state.acquire_once("lock", 60)) is False                  # already held
    run(state.delete("lock"))
    assert run(state.acquire_once("lock", 60)) is True


def test_state_counters_and_hashes():
    assert [run(state.incr_with_expire("c", 60)) for _ in range(3)] == [1, 2, 3]
    assert run(state.counter("c")) == 3
    run(state.hset("h", "SOLUSDT", {"conviction": 0.4}))
    run(state.hset("h", "ETHUSDT", {"conviction": 0.2}))
    run(state.hdel("h", "ETHUSDT"))
    assert run(state.hgetall("h")) == {"SOLUSDT": {"conviction": 0.4}}


def test_signals_db_keeps_everything():
    for i in range(150):
        signals.add_oracle_signal({"ts": f"2026-09-26T00:{i // 60:02d}:{i % 60:02d}", "symbol": "BTCUSDT", "action": "BUY"})
    assert signals.oracle_count() == 150                                  # the JSON file capped at 100
    now = time.time()
    signals.add_fired({"symbol": "WLDUSDT", "action": "BUY", "fired_at": now - 90_000})   # older than a day
    signals.add_fired({"symbol": "SUIUSDT", "action": "BUY", "fired_at": now - 60})
    assert [f["symbol"] for f in signals.recent_fired()] == ["SUIUSDT"]


def test_wallet_survives_reopen(tmp_path):
    db = str(tmp_path / "swing.db")
    w = PaperWallet(db, label="main")
    w.reset_portfolio(10_000)
    t = w.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1)
    w.close_trade_manually(t["id"], 101)
    t2 = w.open_trade("ETHUSDT", "short", 100, 102, 97, 95, risk_pct=1)
    w2 = PaperWallet(db, label="main")                                   # fresh process, same DB
    p = w2.get_portfolio()
    assert p["balance"] == w.get_portfolio()["balance"] != 10_000
    assert [x["id"] for x in p["open_trades"]] == [t2["id"]]
    assert len(p["closed_trades"]) == 1
    other = PaperWallet(db, label="auto")                                 # separate wallet, same file
    assert other.get_portfolio()["open_trades"] == []


def test_legacy_json_is_imported_once(tmp_path):
    legacy = tmp_path / "old.json"
    legacy.write_text(json.dumps({
        "open": [], "closed": [{"id": "x1", "symbol": "BTCUSDT", "direction": "long", "pnl_usd": 12.5,
                                "opened_at": "2026-09-20T00:00:00+00:00", "closed_at": "2026-09-20T01:00:00+00:00",
                                "entry_price": 1, "quantity": 1, "stop_loss": 0.9}],
        "balance": 10_012.5, "initial_balance": 10_000,
        "equity_history": [{"ts": "2026-09-20T00:00:00+00:00", "equity": 10_000}],
    }))
    w = PaperWallet(str(tmp_path / "swing.db"), label="main", legacy_json=str(legacy))
    p = w.get_portfolio()
    assert p["balance"] == 10_012.5 and p["closed_trades"][0]["id"] == "x1"
    assert not legacy.exists() and (tmp_path / "old.migrated.json").exists()


def test_fired_tally_counts_by_window_and_direction():
    import time
    from storage import signals
    now = time.time()
    for ts, sym, act in ((now - 60, "XUSDT", "BUY"), (now - 3 * 86400, "XUSDT", "SELL"),
                         (now - 30 * 86400, "XUSDT", "BUY"), (now - 10, "YUSDT", "BUY")):
        signals.add_fired({"fired_at": ts, "symbol": sym, "action": act})
    t = signals.fired_tally()
    assert t["XUSDT"] == {"d1": 1, "d7": 2, "total": 3, "buys": 2, "sells": 1, "last": pytest.approx(now - 60)}
    assert t["YUSDT"]["d1"] == 1


def test_wallet_reads_do_not_wait_for_the_write_lock(tmp_path):
    """A live wallet holds its lock through Binance calls; reads on the event loop used to block."""
    import threading
    from trading.paper_trader import PaperWallet
    w = PaperWallet(str(tmp_path / "w.db"), label="x")
    w.open_trade("SOLUSDT", "long", 100.0, 98.0, 103.0, 105.0, risk_pct=1)
    held, done = threading.Event(), threading.Event()

    def writer():
        with w._lock:
            held.set()
            done.wait(5)
    t = threading.Thread(target=writer)
    t.start()
    held.wait(5)
    start = __import__("time").time()
    assert len(w.get_portfolio()["open_trades"]) == 1
    assert __import__("time").time() - start < 1
    done.set()
    t.join()
