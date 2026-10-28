import asyncio
import time

import pytest

from experiments import books, events
from news import store
from storage import state as kv

PRICES = {"BTCUSDT": 100_000.0, "ABCUSDT": 1.0, "NEWUSDT": 2.0}


@pytest.fixture
def ev(monkeypatch):
    monkeypatch.setattr(books, "ENABLED", True)
    prices = dict(PRICES)

    async def live(symbol):
        return prices.get(symbol)
    monkeypatch.setattr(events.monitor, "live_price", live)
    funding = {"rates": []}

    async def fh(symbol, start_ms):
        return [{"time": start_ms + 1, "rate": r, "mark": None} for r in funding["rates"]] if symbol != "BTCUSDT" else []
    import data.binance_client as bc
    monkeypatch.setattr(bc, "fetch_funding_history", fh)
    kv.set_sync(events._seen_key(), time.time() - 3600)                       # not the first run
    return prices, funding


def _announce(title, source="Binance listings", ago=60):
    store.add([{"title": title, "source": source, "seen": time.time(), "published": time.time() - ago, "url": "u"}])


def test_classifier_matches_the_study():
    from research.event_study_lab import classify
    assert classify("Binance Will List Abc Coin (ABC) with Seed Tag Applied") == ("spot listing", ["ABC"])
    assert classify("Binance Futures Will Launch USDⓈ-Margined NEWUSDT Perpetual Contract") == ("futures launch", ["NEW"])
    assert classify("Binance Futures Will Delist ABCUSDT and XYZUSDT USDⓈ-M Perpetual Contracts (2026-10-30)") == ("delisting", ["ABC", "XYZ"])
    assert classify("Binance Futures Will Launch Multiple TradFi USDⓈ-Margined Perpetual Contracts") is None
    assert classify("Notice on New Trading Pairs & Trading Bots Services on Binance Spot") is None


def test_spot_listing_opens_a_hedged_short_and_closes_at_the_deadline(ev):
    prices, _ = ev
    _announce("Binance Will List Abc Coin (ABC)")
    asyncio.run(events.tick())
    ps = events.open_positions()
    assert [(p["symbol"], p["kind"]) for p in ps] == [("ABCUSDT", "spot listing")]
    p = ps[0]
    assert p["coin_entry"] < 1.0 < p["btc_entry"] / 100_000 * 1.0 + 1          # sold the coin, bought BTC (slippage each way)
    assert p["deadline_ts"] == pytest.approx(p["announced_ts"] + events.HOLD_DAYS_BY_KIND["spot listing"] * 86400)
    prices["ABCUSDT"] = 0.8                                                    # coin −20%, BTC flat
    closed = asyncio.run(events.tick(now=p["deadline_ts"] + 60))
    assert closed[0]["close_reason"] == "hold_complete" and closed[0]["net"] > 0
    assert asyncio.run(events.tick()) == [] and not events.open_positions()


def test_delisting_closes_before_the_delist_date_and_a_funding_spike_kills(ev):
    prices, funding = ev
    import datetime as dt
    day = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).strftime("%Y-%m-%d")
    _announce(f"Binance Futures Will Delist ABCUSDT USDⓈ-M Perpetual Contracts ({day})", source="Binance delistings")
    asyncio.run(events.tick())
    p = events.open_positions()[0]
    expected = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() - events.DELIST_MARGIN_H * 3600
    assert p["deadline_ts"] == pytest.approx(expected)
    funding["rates"] = [-0.0001, -0.009]                                       # one settlement costing the short 0.9%
    events._last_funding_check["at"] = 0.0
    closed = asyncio.run(events.tick())
    assert closed[0]["close_reason"] == "funding_kill"


def test_futures_launch_waits_an_hour_after_the_perp_starts_trading(ev):
    prices, _ = ev
    _announce("Binance Futures Will Launch USDⓈ-Margined NEWUSDT Perpetual Contract")
    prices.pop("NEWUSDT")                                                      # not trading yet
    t0 = time.time()
    asyncio.run(events.tick(now=t0))
    assert events.pending() and not events.open_positions()
    prices["NEWUSDT"] = 2.0                                                    # starts trading
    asyncio.run(events.tick(now=t0 + 60))
    assert not events.open_positions()                                         # first seen: wait an hour
    asyncio.run(events.tick(now=t0 + 60 + events.LAUNCH_WAIT_S + 1))
    assert [p["kind"] for p in events.open_positions()] == ["futures launch"]


def test_first_run_and_stale_announcements_are_ignored(ev):
    kv.delete_sync(events._seen_key())
    _announce("Binance Will List Abc Coin (ABC)")
    asyncio.run(events.tick())
    assert not events.pending() and not events.open_positions()                # first run only marks the time
    _announce("Binance Will List New Coin (NEW)", ago=5 * 3600)                # too old to trade
    asyncio.run(events.tick())
    assert "NEWUSDT" not in {p["symbol"] for p in events.pending() + events.open_positions()}
