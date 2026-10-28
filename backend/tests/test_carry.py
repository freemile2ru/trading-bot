import asyncio
import time

import pytest

from experiments import books, carry
from trading import book_switches, futures

_REAL_PRICES = carry._prices                                   # captured before any fixture stubs it
PX = {"BTCUSDT": (100_000.0, 100_050.0), "ETHUSDT": (4_000.0, 4_002.0)}     # (spot, perp)


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(carry, "COINS", ["BTCUSDT", "ETHUSDT"])
    prices = {k: list(v) for k, v in PX.items()}

    async def px(symbol):
        return tuple(prices[symbol])
    monkeypatch.setattr(carry, "_prices", px)
    monkeypatch.setattr(carry, "_last_funding_check", {"at": 0.0})

    async def funding(symbol, start_ms):
        return [{"time": start_ms + 3_600_000, "rate": 0.0001, "mark": None}]
    import data.binance_client as bc
    monkeypatch.setattr(bc, "fetch_funding_history", funding)
    return prices


def test_opens_long_spot_short_perp_per_coin(c):
    now = time.time() - 7200
    asyncio.run(carry.tick(now))
    ps = {p["symbol"]: p for p in carry.open_positions()}
    assert set(ps) == {"BTCUSDT", "ETHUSDT"}
    btc = ps["BTCUSDT"]
    assert btc["notional"] == pytest.approx(carry.INITIAL_BALANCE * carry.NOTIONAL_X / 2, rel=1e-3)
    assert btc["spot_entry"] > 100_000 and btc["perp_entry"] < 100_050        # bought spot, sold perp, slippage each way


def test_price_moves_cancel_and_funding_is_earned(c):
    asyncio.run(carry.tick(time.time() - 7200))
    c["BTCUSDT"][:] = [110_000.0, 110_055.0]                                   # +10% on both legs
    asyncio.run(carry.tick(time.time()))
    p = next(p for p in carry.open_positions() if p["symbol"] == "BTCUSDT")
    assert p["funding"] == pytest.approx(0.0001 * p["qty"] * 110_055.0, rel=1e-6)   # one settlement, short receives
    pnl = carry.mark_pnl(p, 110_000.0, 110_055.0)
    assert abs(pnl) < p["notional"] * 0.005                                    # a 10% move barely registers


def test_switching_off_unwinds_both_legs(c):
    asyncio.run(carry.tick(time.time() - 3600))
    asyncio.run(book_switches.set_enabled("carry", False))
    asyncio.run(carry.tick(time.time()))
    assert carry.open_positions() == []
    closed = carry.closed_positions()
    assert {x["close_reason"] for x in closed} == {"switched_off"}
    assert all(x["fees"] == pytest.approx(2 * (x["qty"] * x["spot_entry"] * carry.SPOT_FEE + x["qty"] * x["perp_entry"]
                                                  * futures.TAKER_FEE), rel=0.02) for x in closed)
    assert carry.balance() < carry.INITIAL_BALANCE                             # fees and slippage on 4 fills, no time to earn


def test_book_appears_in_trades_books_and_lab(c):
    asyncio.run(carry.tick(time.time()))
    from trading import overview
    rows, initial = overview.all_rows()
    assert sum(r["system"] == "carry" for r in rows) == 2 and initial["carry"] == carry.INITIAL_BALANCE
    cat = {b["name"]: b for b in book_switches.catalog()["books"]}
    assert cat["carry"]["open"] == 2 and cat["carry"]["wallets"][0]["system"] == "carry"
    from experiments import lab
    e = next(x for x in lab.summary()["experiments"] if x["key"] == "carry")
    assert e["carry"]["open"] == 2


def test_raising_the_target_tops_positions_up_instead_of_reopening(c, monkeypatch):
    monkeypatch.setattr(carry, "NOTIONAL_X", 1.0)
    asyncio.run(carry.tick(time.time() - 7200))
    first = {p["id"] for p in carry.open_positions()}
    asyncio.run(carry.tick(time.time() - 3600))
    assert {p["id"] for p in carry.open_positions()} == first                  # at target: nothing new
    monkeypatch.setattr(carry, "NOTIONAL_X", 2.0)
    asyncio.run(carry.tick(time.time()))
    ps = carry.open_positions()
    assert first <= {p["id"] for p in ps} and len(ps) == 4                      # originals kept, one top-up per coin
    btc = sum(p["notional"] for p in ps if p["symbol"] == "BTCUSDT")
    assert btc == pytest.approx(carry.target_notional(), rel=0.01)


def test_gold_leg_prices_its_spot_from_paxg(c, monkeypatch):
    monkeypatch.setattr(carry, "COINS", ["XAUUSDT"])
    asked = []

    async def ticker(symbol):
        asked.append(symbol)
        return {"price": 4000.0}
    from data import feeds
    monkeypatch.setattr(feeds, "cached_ticker", ticker)

    async def perp(symbol):
        return 4002.0
    monkeypatch.setattr(carry.monitor, "live_price", perp)
    monkeypatch.setattr(carry, "_prices", _REAL_PRICES)
    asyncio.run(carry.tick(time.time() - 7200))
    p = carry.open_positions()[0]
    assert p["symbol"] == "XAUUSDT" and set(asked) == {"PAXGUSDT"} and p["spot_entry"] > 4000.0 > 0.999 * p["perp_entry"]
