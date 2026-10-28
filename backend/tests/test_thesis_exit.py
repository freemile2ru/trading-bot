import asyncio
import time

from storage import signals as signal_log
from trading import thesis_exit as TE
from trading.paper_trader import PaperWallet


def _reading(ts, conv, symbol="SOLUSDT"):
    signal_log.add_scan_features([{"symbol": symbol, "scanned_at": ts, "price": 100.0, "conviction": conv}])


def _open(tmp_path, label="signals_rules", hours_ago=3.0):
    w = PaperWallet(str(tmp_path / "w.db"), label=label)
    w.reset_portfolio(10_000)
    t = w.open_trade(symbol="SOLUSDT", direction="long", entry_price=100.0, stop_loss=98.0, take_profit_1=103.0,
                     take_profit_2=106.0, size_pct=10, timeframe="1h", hold_duration="N/A", extra={})
    w.annotate(t["id"], opened_ts=time.time() - hours_ago * 3600)
    return w, t


def test_fade_needs_two_recent_readings_after_two_hours(tmp_path):
    w, t = _open(tmp_path)
    trade = w.get_portfolio()["open_trades"][0]
    now = time.time()
    _reading(now - 2.9 * 3600, 0.02)                                    # before the 2h mark: ignored
    _reading(now - 600, 0.05)
    assert TE.fade_note(trade, now) is None                             # only one reading after 2h
    _reading(now - 60, 0.30)
    assert TE.fade_note(trade, now) is None                             # thesis back
    _reading(now - 30, 0.04)
    _reading(now - 10, -0.20)
    assert "thesis faded" in TE.fade_note(trade, now)
    assert TE.fade_note(trade, now + 3600) is None                      # readings stale: do nothing
    young = {**trade, "opened_ts": now - 3600}
    assert TE.fade_note(young, now) is None


def test_check_closes_faded_trades_and_leaves_a_twin(tmp_path, monkeypatch):
    w, t = _open(tmp_path)
    now = time.time()
    _reading(now - 120, 0.03)
    _reading(now - 60, 0.01)

    async def price(symbol):
        return 100.5
    from trading import monitor
    monkeypatch.setattr(monitor, "live_price", price)
    events = asyncio.run(TE.check(w))
    assert [e["trade"]["close_reason"] for e in events] == ["thesis_faded"]
    assert w.get_portfolio()["open_trades"] == []
    twin = next(s for s in w._snapshot()["shadows"] if s["claude_verdict"] == TE.TWIN)
    assert twin["trade_id"] == t["id"] and twin["entry_price"] == t["entry_price"] and twin["stop_loss"] == t["stop_loss"]
    other = PaperWallet(str(tmp_path / "m.db"), label="main")           # manual trades are never touched
    assert asyncio.run(TE.check(other)) == []
