"""Regression tests from the full review: stale data and scan inputs that trades are decided on."""

import asyncio

from autotrader import engine as autotrader
from data import cache, feeds
from scanner import engine as scan_engine
from trading.paper_trader import PaperWallet


def _capture_stale_ttls(monkeypatch) -> dict:
    seen = {}

    async def fake_cached(k, ttl, loader, *, stale_ttl=None):
        seen[k] = stale_ttl
        return [] if ":ohlcv:" in k else 1.0
    monkeypatch.setattr(cache, "cached", fake_cached)
    return seen


def test_prices_and_candles_are_never_served_hours_old(monkeypatch):
    # The default stale mirror keeps a value for 6 hours; stops, entries and TA must not use one.
    seen = _capture_stale_ttls(monkeypatch)
    asyncio.run(feeds.cached_futures_price("SOLUSDT"))
    asyncio.run(feeds.cached_ticker("SOLUSDT"))
    asyncio.run(feeds.cached_order_book("SOLUSDT"))
    asyncio.run(feeds.cached_ohlcv("SOLUSDT", "15m"))
    asyncio.run(feeds.cached_ohlcv("SOLUSDT", "4h"))
    by = {k.split(":")[2] + (":" + k.split(":")[4] if ":ohlcv:" in k else ""): v for k, v in seen.items()}
    assert by["fprice"] <= 60 and by["ticker"] <= 300 and by["depth"] <= 300
    assert by["ohlcv:15m"] <= 900 and by["ohlcv:4h"] <= 4 * 3600


def test_watchlist_scans_use_the_same_timeframes_as_the_full_sweep():
    # 4h carries the most weight in the blend, sets the regime and is the first setup timeframe.
    assert "4h" in scan_engine.LIGHT_INTERVALS
    assert set(scan_engine.LIGHT_INTERVALS) == set(scan_engine.FULL_INTERVALS)


def test_price_sitting_on_a_level_counts_as_near_it():
    t = scan_engine._extract_triggers({"1h": {"distance_to_r1_pct": 0.0, "distance_to_s1_pct": 3.0}}, "1h", 1.0)
    assert t["near_level"] and t["distance_to_level_pct"] == 0.0
    far = scan_engine._extract_triggers({"1h": {}}, "1h", 1.0)
    assert not far["near_level"]


def test_heat_room_is_measured_on_the_wallet_being_adjusted(tmp_path):
    busy = PaperWallet(str(tmp_path / "w.db"), label="busy")
    busy.reset_portfolio(10_000)
    busy.open_trade("SOLUSDT", "long", 100.0, 95.0, 107.5, 112.5, risk_pct=3.0)
    idle = PaperWallet(str(tmp_path / "w.db"), label="idle")
    idle.reset_portfolio(10_000)
    assert autotrader._heat_room_pct(busy) < autotrader._heat_room_pct(idle) - 2.5
