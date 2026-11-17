"""The 2026-11-16 data-review builds: the budget share cap, the burst_up 24h book, the weekly trail shadow, the BTC-up-day boost."""
import asyncio

import pytest

from experiments import burst, entry_cells as ec
from trading import exits
from trading import risk_budget as rb


def test_no_book_takes_more_than_half_the_day(monkeypatch):
    now = 1_794_900_000.0
    mk = lambda n, r: [{"id": f"t{i}", "pnl_usd": r * 100, "risk_usd": 100.0, "opened_ts": now - i * 7200} for i in range(n)]
    rows = {"auto:reversal": mk(20, 0.0), "copy_trading": mk(20, 0.0), "copy_stocks": mk(20, 2.0)}          # one hot record
    monkeypatch.setattr(rb, "closed_trades", lambda b: rows[b])
    monkeypatch.setattr(rb, "base_equity", lambda: 50_000.0)
    monkeypatch.setattr(rb, "day_pnl", lambda now_: {})
    monkeypatch.setattr(rb, "ENABLED", True)
    monkeypatch.setattr(rb, "DAILY_RISK_PCT", 10.0)
    b = rb.compute(now)["books"]
    assert b["copy_stocks"]["share_pct"] == 50.0 and b["auto:reversal"]["share_pct"] == 25.0 and b["copy_trading"]["share_pct"] == 25.0
    assert sum(x["share_usd"] for x in b.values()) == pytest.approx(5000.0, abs=0.05)


def test_the_burst_books_hold_four_hours_unless_a_book_says_otherwise():
    assert "burst_up_24h" not in burst.BOOKS                                            # built and withdrawn 2026-11-16 (the independent review)
    assert all(cfg.get("hold_h", burst.HOLD_H) == 4.0 for cfg in burst.BOOKS.values())


def test_a_weekly_leg_with_an_atr_gets_the_trail_shadow(monkeypatch):
    monkeypatch.setattr(exits, "LOCK_MODE", "live")
    leg = {"id": "w1", "symbol": "SOLUSDT", "direction": "long", "entry_price": 100.0, "stop_loss": 70.0, "take_profit_1": 190.0, "take_profit_2": 195.0,
           "quantity": 10.0, "notional_usd": 1000.0, "risk_usd": 300.0, "timeframe": "1w", "leverage": 2, "opened_at": "2026-11-16T00:00:00+00:00",
           "opened_ts": 1_794_900_000.0, "exit_policy": "manual_lock", "shadows": False, "strategy": "weekly_rs", "atr_pct": 6.0, "checked_from_ms": None}
    sh = exits.make_shadows(leg, "weekly_rs")
    assert [s["exit_policy"] for s in sh] == ["manual_lock_trail"] and sh[0]["expires_at"] == pytest.approx(1_794_900_000.0 + 168 * 3600)
    assert exits.make_shadows({**leg, "atr_pct": None}, "weekly_rs") == []                                     # no ATR, no trail to shadow
    mirror = {**leg, "strategy": "auto", "lane": "weekly"}
    assert [s["exit_policy"] for s in exits.make_shadows(mirror, "auto")] == ["manual_lock_trail"]


def test_the_btc_boost_waits_for_the_cells_verdict(monkeypatch):
    monkeypatch.setattr(ec, "BTC_BOOST", True)

    async def btc24(now):
        return 3.1
    monkeypatch.setattr(ec, "btc24_pct", btc24)
    monkeypatch.setattr(ec, "btc_cell_live", lambda now=None: False)
    b = asyncio.run(ec.short_boost(1_794_900_000.0))
    assert b["x"] == 1.0 and b["in_cell"] and not b["live"] and "until the cell" in b["why"]
    monkeypatch.setattr(ec, "btc_cell_live", lambda now=None: True)
    b = asyncio.run(ec.short_boost(1_794_900_000.0))
    assert b["x"] == ec.BOOST_X == 1.5 and b["live"]

    async def flat(now):
        return 0.4
    monkeypatch.setattr(ec, "btc24_pct", flat)
    assert asyncio.run(ec.short_boost(1_794_900_000.0))["x"] == 1.0
    monkeypatch.setattr(ec, "BTC_BOOST", False)
    assert asyncio.run(ec.short_boost(1_794_900_000.0))["why"] == "BTC boost off"
