import asyncio
import time

import numpy as np
import pandas as pd
import pytest

from experiments import books, hold72
from trading import book_switches, futures

PRICES = {"BTCUSDT": 100_000.0, "SOLUSDT": 200.0, "ETHUSDT": 4_000.0, "AVAXUSDT": 30.0}


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(books, "ENABLED", True)
    prices = dict(PRICES)

    async def live(symbol):
        return prices.get(symbol)
    monkeypatch.setattr(hold72.monitor, "live_price", live)

    async def funding(symbol, start_ms):
        return [{"time": start_ms + 8 * 3600_000, "rate": 0.0001, "mark": None}]
    import data.binance_client as bc
    monkeypatch.setattr(bc, "fetch_funding_history", funding)
    return prices


def _scan(symbol, conviction):
    return {"symbol": symbol, "conviction": conviction, "price": PRICES[symbol], "confidence": 70}


def test_spread_is_the_coin_leg_minus_the_btc_leg_in_the_pairs_direction():
    p = {"direction": "long", "coin_entry": 100.0, "btc_entry": 1000.0}
    assert hold72.spread_pct(p, 110.0, 1050.0) == pytest.approx(5.0)          # coin +10%, BTC +5%
    assert hold72.spread_pct({**p, "direction": "short"}, 110.0, 1050.0) == pytest.approx(-5.0)
    assert hold72.fill(100.0, "long", True) > 100.0 > hold72.fill(100.0, "long", False)


def test_opens_one_hedged_pair_per_high_conviction_coin(h):
    opened = asyncio.run(hold72.on_scans([_scan("SOLUSDT", 0.6), _scan("ETHUSDT", -0.5), _scan("AVAXUSDT", 0.3),
                                          _scan("BTCUSDT", 0.9)]))
    assert {(p["symbol"], p["direction"]) for p in opened} == {("SOLUSDT", "long"), ("ETHUSDT", "short")}
    sol = next(p for p in opened if p["symbol"] == "SOLUSDT")
    assert sol["notional"] == pytest.approx(hold72.INITIAL_BALANCE * hold72.LEG_PCT / 100)
    assert sol["btc_entry"] < PRICES["BTCUSDT"]                                 # the BTC leg is sold
    assert asyncio.run(hold72.on_scans([_scan("SOLUSDT", 0.7)])) == []           # already holding SOL


def test_switched_off_opens_nothing(h):
    asyncio.run(book_switches.set_enabled("hold72", False))
    assert asyncio.run(hold72.on_scans([_scan("SOLUSDT", 0.6)])) == []


def test_pairs_close_after_72h_net_of_four_fills_and_funding(h):
    t0 = time.time() - 73 * 3600
    asyncio.run(hold72.on_scans([_scan("SOLUSDT", 0.6)], now=t0))
    h["SOLUSDT"], h["BTCUSDT"] = 220.0, 101_000.0                                # coin +10%, BTC +1%
    closed = asyncio.run(hold72.monitor_pairs())
    assert len(closed) == 1 and closed[0]["close_reason"] == "hold_complete"
    c, n = closed[0], closed[0]["notional"]
    assert c["fees"] == pytest.approx(4 * n * futures.TAKER_FEE, rel=1e-6)
    assert c["funding"] == pytest.approx(0.0, abs=1e-9)                        # long coin pays, short BTC receives
    assert 0.08 * n < c["gross"] < 0.09 * n                                     # ~9% spread less slippage
    assert c["net"] == pytest.approx(c["gross"] - c["fees"] - c["funding"])
    assert hold72.balance() == pytest.approx(hold72.INITIAL_BALANCE + c["net"])


def test_disaster_stop_closes_a_pair_down_12_percent(h):
    asyncio.run(hold72.on_scans([_scan("SOLUSDT", 0.6)]))
    h["SOLUSDT"] = 170.0                                                        # −15% vs flat BTC
    closed = asyncio.run(hold72.monitor_pairs())
    assert closed[0]["close_reason"] == "disaster_stop"
    row = next(r for r in hold72.rows() if r["status"] == "closed")
    assert row["system"] == "hold72" and row["r"] < -1.0                        # past the 1R disaster line


def test_the_book_shows_up_in_trades_books_and_lab(h):
    asyncio.run(hold72.on_scans([_scan("SOLUSDT", 0.6)]))
    from trading import overview
    rows, initial = overview.all_rows()
    assert any(r["system"] == "hold72" for r in rows) and initial["hold72"] == hold72.INITIAL_BALANCE
    cat = book_switches.catalog()
    assert "hold72" not in {b["name"] for b in cat["books"]}                    # retired: no switch any more
    assert next(r for r in cat["retired"] if r["name"] == "hold72")["closed"] == 0
    from experiments import lab
    assert "hold72" not in {e["key"] for e in lab.summary()["experiments"]}     # retired books drop off the Lab
    assert hold72.config()["hold_hours"] == 24 and hold72.config()["watch_below"] == 0.25


def test_replay_rule_counts_two_legs_of_cost_and_skips_btc():
    from research import hold_lab
    ts = pd.date_range("2026-01-01", periods=60, freq="D", tz="UTC")
    rows = []
    for t in ts:
        rows.append({"ts": t, "symbol": "SOLUSDT", "conviction": 0.6, "f72": 300.0, "vb72": 200.0, "va72": 150.0})
        rows.append({"ts": t, "symbol": "BTCUSDT", "conviction": 0.6, "f72": 100.0, "vb72": 0.0, "va72": 0.0})
    df = pd.DataFrame(rows)
    r = hold_lab.evaluate(df, 0.45, 72, "vs_btc")
    assert r["trades"] == 60 and r["mean_bps_day"] == pytest.approx(200 - 2 * hold_lab.LEG_COST_BPS)
    assert r["passed"] and r["by_side_bps"] == {"long": pytest.approx(168.0)}
    assert not np.isnan(r["ci_low"])


def test_at_most_max_per_side_pairs_face_one_way(h, monkeypatch):
    monkeypatch.setattr(hold72, "MAX_PER_SIDE", 2)
    opened = asyncio.run(hold72.on_scans([_scan("SOLUSDT", 0.6), _scan("ETHUSDT", 0.55), _scan("AVAXUSDT", 0.5)]))
    assert [p["symbol"] for p in opened] == ["SOLUSDT", "ETHUSDT"]                 # the strongest two longs
    h["XRPUSDT"] = 2.0
    PRICES["XRPUSDT"] = 2.0
    try:
        later = asyncio.run(hold72.on_scans([_scan("AVAXUSDT", 0.7), _scan("XRPUSDT", -0.5)]))
    finally:
        PRICES.pop("XRPUSDT")
    assert [(p["symbol"], p["direction"]) for p in later] == [("XRPUSDT", "short")]  # long side full, short side open


def test_pairs_close_after_24h_now(h):
    assert hold72.HOLD_HOURS == 24
    asyncio.run(hold72.on_scans([_scan("SOLUSDT", 0.6)], now=time.time() - 23 * 3600))
    assert asyncio.run(hold72.monitor_pairs()) == []
    asyncio.run(hold72.on_scans([_scan("ETHUSDT", 0.6)], now=time.time() - 25 * 3600))
    assert [p["symbol"] for p in asyncio.run(hold72.monitor_pairs())] == ["ETHUSDT"]


def test_watcher_closes_a_pair_once_its_conviction_fades(h):
    from storage import signals
    t0 = time.time() - 3 * 3600
    asyncio.run(hold72.on_scans([_scan("SOLUSDT", 0.6)], now=t0))
    add = lambda ts, c: signals.add_scan_features([{"symbol": "SOLUSDT", "scanned_at": ts, "price": 200.0, "conviction": c}])
    add(time.time() - 600, 0.40)
    add(time.time() - 300, 0.20)
    assert asyncio.run(hold72.monitor_pairs()) == []                 # one weak reading isn't enough
    add(time.time() - 60, 0.10)
    closed = asyncio.run(hold72.monitor_pairs())
    assert [c["close_reason"] for c in closed] == ["thesis_faded"]
