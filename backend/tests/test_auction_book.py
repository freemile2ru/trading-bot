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
    monkeypatch.setattr(auction, "VARIANT", {"min_target_bps": 0, "min_grade": 0, "entry": "next_open", "target_mode": "next"})
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


def test_snapshot_shows_levels_context_grades_and_the_book(book, monkeypatch):
    w, prices = book
    df = _frame()

    async def fake_bars(sym, now):
        return df
    monkeypatch.setattr(auction, "bars", fake_bars)
    auction._snap_cache.update(at=0.0, data=None)
    snap = asyncio.run(auction.snapshot(float(df.index[-1].timestamp()) + 300))
    inst = snap["instruments"][0]
    assert inst["symbol"] == "TESTUSDT" and inst["price"] == 105.0 and inst["in_session"]
    names = {l["name"] for l in inst["levels"]}
    assert {"PDH", "PDL", "ONH", "ONL"} <= names and all("bps" in l for l in inst["levels"])
    assert inst["levels"] == sorted(inst["levels"], key=lambda l: -l["price"])
    assert 0 <= inst["grade_long"]["score"] <= 8 and isinstance(inst["grade_long"]["stand_down"], bool)
    assert inst["next_above"]["price"] > inst["price"] > inst["next_below"]["price"]
    assert snap["book"]["equity"] == 1_000 and snap["verdict"]["status"] == "collecting" and snap["config"]["symbols"] == ["TESTUSDT"]
    assert asyncio.run(auction.snapshot(float(df.index[-1].timestamp()) + 301)) is snap            # cached for a minute


def test_chart_returns_bars_levels_lines_signals_and_trades(book, monkeypatch):
    w, prices = book
    df = _frame()

    async def fake_bars(sym, now):
        return df
    monkeypatch.setattr(auction, "bars", fake_bars)
    out = asyncio.run(auction.chart("TESTUSDT", 400, float(df.index[-1].timestamp()) + 300))      # a window that reaches back past day 2's open
    assert len(out["bars"]) == 400 and set(out["bars"][0]) == {"t", "o", "h", "l", "c", "v", "session"}
    assert len(out["lines"]["vwap"]) == 400 and len(out["levels"]["PDH"]) == 400
    assert any(v is not None for v in out["levels"]["PDH"]) and {"PDH", "PDL", "ONH", "ONL"} <= {l["name"] for l in out["current_levels"]}
    kinds = {e["kind"] for e in out["study_events"]}
    assert {"accept", "confirm", "entry"} <= kinds                       # the synthetic day's acceptance shows up as the study's signal
    assert out["trades"] == []


def test_stock_perps_trade_the_cash_session_and_tag_the_entry_slot(book):
    """2026-11-05: the session ends at the cash close (16:00 ET, not the study's 17:00), the trade expires there, and each
    trade carries the slot it was entered in — the first hour is judged on its own."""
    from trading import auction_levels as AL
    w, prices = book
    df = _frame()
    k = len(df) - 13
    now = float(df.index[k + 2].timestamp()) + 300
    for n, dt in ((3, 0), (4, 300), (5, 600)):
        asyncio.run(auction.step("TESTUSDT", df.iloc[:k + n], now + dt))
    t = w.get_portfolio()["open_trades"][0]
    end = pd.Timestamp(t["expires_at"], unit="s", tz="UTC").tz_convert("America/New_York")
    assert (end.hour, end.minute) == auction.SESSION_END == (16, 0) and t["entry_slot"] == "first_hour"
    assert "16:00 ET" in t["hold_duration"]
    fr = AL.session_frame(_bars([100.0] * 120, start="2026-06-01 19:00"), session_end=auction.SESSION_END)      # 15:00 → 01:00 ET
    hm = fr["et"].dt.hour * 60 + fr["et"].dt.minute
    assert fr["in_session"][(hm >= 15 * 60) & (hm < 16 * 60)].all() and not fr["in_session"][(hm >= 16 * 60) | (hm < 9 * 60)].any()
    assert AL.session_frame(_bars([100.0] * 120, start="2026-06-01 19:00"))["in_session"][hm == 16 * 60 + 30].all()  # the study's own frame keeps 17:00


def test_nothing_is_accepted_or_entered_after_the_entry_cutoff(book, monkeypatch):
    w, prices = book
    df = _frame()
    k = len(df) - 13
    now = float(df.index[k + 2].timestamp()) + 300
    monkeypatch.setattr(auction, "ENTRY_CUTOFF", (9, 40))                       # the breakout bar (09:40) is at the cutoff
    assert asyncio.run(auction.step("TESTUSDT", df.iloc[:k + 3], now)) is None
    assert w.get_portfolio()["open_trades"] == [] and not auction._state("TESTUSDT").get("pending")
    # accepted and confirmed before the cutoff, the entry bar lands on it: gated, logged
    kv.delete_sync(auction._k("state", "TESTUSDT"))
    monkeypatch.setattr(auction, "ENTRY_CUTOFF", (9, 50))
    for n, dt in ((3, 0), (4, 300), (5, 600)):
        asyncio.run(auction.step("TESTUSDT", df.iloc[:k + n], now + dt))
    assert w.get_portfolio()["open_trades"] == []
    assert auction.status()["log"][0]["event"] == "gated" and "entry cutoff" in auction.status()["log"][0]["why"]


def test_a_fill_that_ran_past_the_close_is_skipped_and_weekly_levels_are_not_traded(book, monkeypatch):
    w, prices = book
    df = _frame()
    k = len(df) - 13
    now = float(df.index[k + 2].timestamp()) + 300
    prices["px"] = 101.3 * 1.004                                                  # 40 bps above the bar's close (limit 15)
    for n, dt in ((3, 0), (4, 300), (5, 600)):
        asyncio.run(auction.step("TESTUSDT", df.iloc[:k + n], now + dt))
    assert w.get_portfolio()["open_trades"] == []
    assert auction.status()["log"][0]["event"] == "skipped" and "past the bar's close" in auction.status()["log"][0]["why"]
    # a better price than the close is fine
    kv.delete_sync(auction._k("state", "TESTUSDT"))
    prices["px"] = 101.3 * 0.999
    for n, dt in ((3, 0), (4, 300), (5, 600)):
        asyncio.run(auction.step("TESTUSDT", df.iloc[:k + n], now + dt))
    assert len(w.get_portfolio()["open_trades"]) == 1
    # the level the frame breaks here is PDH/ONH; declared skipped, nothing is accepted
    w.close_trade_manually(w.get_portfolio()["open_trades"][0]["id"], 101.3, "test")
    kv.delete_sync(auction._k("state", "TESTUSDT"))
    monkeypatch.setattr(auction, "SKIP_LEVELS", {"WKH", "WKL", "PDH", "ONH"})
    assert asyncio.run(auction.step("TESTUSDT", df.iloc[:k + 3], now)) is None
    assert "WKL" in auction.SKIP_LEVELS and "WKH" in auction.SKIP_LEVELS
