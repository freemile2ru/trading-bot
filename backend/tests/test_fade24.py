import asyncio
import time

import pytest

from experiments import books, fade24
from trading import futures

PRICES = {"BTCUSDT": 100_000.0, "SOLUSDT": 200.0, "ETHUSDT": 4_000.0, "AVAXUSDT": 30.0, "XRPUSDT": 2.0, "ADAUSDT": 1.0}


@pytest.fixture
def f(monkeypatch):
    monkeypatch.setattr(books, "ENABLED", True)
    prices = dict(PRICES)

    async def live(symbol):
        return prices.get(symbol)
    monkeypatch.setattr(fade24.monitor, "live_price", live)

    async def funding(symbol, start_ms):
        return []
    import data.binance_client as bc
    monkeypatch.setattr(bc, "fetch_funding_history", funding)
    return prices


def _scan(symbol, conviction):
    return {"symbol": symbol, "conviction": conviction, "price": PRICES[symbol], "confidence": 70}


def test_trades_against_the_scanners_conviction(f):
    opened = asyncio.run(fade24.on_scans([_scan("SOLUSDT", 0.6), _scan("ETHUSDT", -0.4), _scan("AVAXUSDT", 0.2),
                                          _scan("BTCUSDT", 0.9)]))
    assert {(p["symbol"], p["direction"]) for p in opened} == {("SOLUSDT", "short"), ("ETHUSDT", "long")}
    sol = next(p for p in opened if p["symbol"] == "SOLUSDT")
    assert sol["btc_entry"] > PRICES["BTCUSDT"]                                  # the BTC hedge is bought


def test_closes_after_24h_and_wins_when_the_move_reverses(f):
    asyncio.run(fade24.on_scans([_scan("SOLUSDT", 0.6)], now=time.time() - 25 * 3600))
    f["SOLUSDT"] = 190.0                                                          # SOL −5% vs a flat BTC
    closed = asyncio.run(fade24.monitor_pairs())
    assert closed[0]["close_reason"] == "hold_complete" and closed[0]["net"] > 0
    assert closed[0]["fees"] == pytest.approx(4 * closed[0]["notional"] * futures.TAKER_FEE, rel=1e-6)


def test_caps_pairs_facing_one_way(f, monkeypatch):
    monkeypatch.setattr(fade24, "MAX_PER_SIDE", 2)
    opened = asyncio.run(fade24.on_scans([_scan(s, 0.5) for s in ("SOLUSDT", "ETHUSDT", "AVAXUSDT")] + [_scan("XRPUSDT", -0.5)]))
    assert sorted((p["symbol"], p["direction"]) for p in opened) == [("ETHUSDT", "short"), ("SOLUSDT", "short"), ("XRPUSDT", "long")]


def test_shows_up_in_books_with_a_verdict(f):
    """Retired 2026-10-28: the book is on the retired list (no switch), its record intact."""
    from trading import book_switches
    asyncio.run(fade24.on_scans([_scan("SOLUSDT", 0.6)]))
    cat = book_switches.catalog()
    assert "fade24" not in {b["name"] for b in cat["books"]}
    assert next(r for r in cat["retired"] if r["name"] == "fade24")["closed"] == 0
    from experiments import lab
    assert "fade24" not in {e["key"] for e in lab.summary()["experiments"]}
