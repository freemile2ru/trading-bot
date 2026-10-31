"""The auction book: one closed bar at a time — accept, confirm, gate, enter, exit through the level."""
import asyncio
import sys
import os

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_auction_levels import _bars                       # noqa: E402

from experiments import auction, books                      # noqa: E402
from storage import state as kv                             # noqa: E402
from trading import book_switches, monitor                  # noqa: E402
from trading.paper_trader import PaperWallet                # noqa: E402


@pytest.fixture
def book(tmp_path, monkeypatch):
    w = PaperWallet(str(tmp_path / "a.db"), label="auction", initial_balance=1_000)
    monkeypatch.setitem(books.WALLETS, "auction", w)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    monkeypatch.setattr(auction, "SYMBOLS", ("TESTUSDT",))
    monkeypatch.setattr(auction, "VARIANT", {"min_target_bps": 0, "min_grade": 0})
    monkeypatch.setattr(auction, "gates", lambda sym, now: None)
    kv.delete_sync(auction._k("state", "TESTUSDT"), auction._k("log"))
    prices = {"px": 101.3}

    async def price(sym):
        return prices["px"]
    monkeypatch.setattr(monitor, "live_price", price)
    return w, prices


def _frame():
    pre = [99.0 + i / 575 for i in range(576)]
    day1 = [100 + i / 90 for i in range(90)]
    night = [98.5 + 2.5 * i / 197 for i in range(198)]
    day2 = [101.0, 101.05, 101.2, 101.3, 101.5, 101.9, 102.3, 102.8, 103.4, 104.0, 104.5, 104.8, 105.0]
    return _bars(pre + day1 + night + day2)


def test_the_book_walks_accept_confirm_enter_and_exits_through_the_level(book):
    w, prices = book
    df = _frame()
    k = len(df) - 13                                           # day 2 starts here (09:30 ET)
    now = float(df.index[k + 2].timestamp()) + 300            # right after the 101.2 bar closes
    ev = asyncio.run(auction.step("TESTUSDT", df.iloc[:k + 3], now))
    assert ev and ev["event"] == "accepted" and ev["direction"] == "long" and ev["level_name"] in ("PDH", "ONH")
    ev = asyncio.run(auction.step("TESTUSDT", df.iloc[:k + 4], now + 300))         # 101.3 holds above the level
    assert ev and ev["event"] == "confirmed" and 0 <= ev["grade"] <= 8
    ev = asyncio.run(auction.step("TESTUSDT", df.iloc[:k + 5], now + 600))         # entry at the live price
    assert ev and ev["event"] == "opened"
    t = w.get_portfolio()["open_trades"][0]
    assert t["symbol"] == "TESTUSDT" and t["direction"] == "long" and t["entry_price"] == pytest.approx(101.3, rel=1e-3)
    assert t["stop_loss"] < t["entry_price"] < t["take_profit_1"] == t["take_profit_2"] and t["expires_at"]
    assert t["notional_usd"] <= 1_000 * auction.MAX_GROSS and t["level_name"] in ("PDH", "ONH")
    # the same bar again: nothing (already decided)
    assert asyncio.run(auction.step("TESTUSDT", df.iloc[:k + 5], now + 601)) is None
    # a later close back through the level: the book closes it
    back = df.iloc[:k + 6].copy()
    back.iloc[-1, back.columns.get_loc("close")] = 100.5
    prices["px"] = 100.5
    ev = asyncio.run(auction.step("TESTUSDT", back, now + 900))
    assert ev and ev["event"] == "exit_through_level" and w.get_portfolio()["open_trades"] == []
    assert w.all_trades()["closed"][0]["close_reason"] == "exit_through_level"
    assert [e["event"] for e in auction.status()["log"]][0] == "opened"


def test_calendar_gate_blocks_the_entry(book, monkeypatch):
    w, prices = book
    monkeypatch.setattr(auction, "gates", lambda sym, now: "macro blackout: FOMC in 1.0h")
    df = _frame()
    k = len(df) - 13
    now = float(df.index[k + 2].timestamp()) + 300
    asyncio.run(auction.step("TESTUSDT", df.iloc[:k + 3], now))
    asyncio.run(auction.step("TESTUSDT", df.iloc[:k + 4], now + 300))
    assert asyncio.run(auction.step("TESTUSDT", df.iloc[:k + 5], now + 600)) is None
    assert w.get_portfolio()["open_trades"] == []
    assert auction.status()["log"][0]["event"] == "gated" and "FOMC" in auction.status()["log"][0]["why"]


def test_tick_only_acts_in_the_session_window():
    # a Saturday afternoon in New York: nothing happens, no feeds are called
    sat = pd.Timestamp("2026-06-06 14:00", tz="America/New_York").timestamp()
    assert asyncio.run(auction.tick(sat)) == []
