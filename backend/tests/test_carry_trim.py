"""Carry books follow their capital: top up when under it, reduce (partially) when over it."""
import asyncio

import pytest

from experiments import carry, carry_q
from storage import state as kv
from trading import book_switches, capital


@pytest.fixture
def prices(monkeypatch):
    async def _p(symbol, contract=None):
        return 100.0, 100.5
    monkeypatch.setattr(carry, "_prices", _p)
    monkeypatch.setattr(carry_q, "_prices", _p)

    async def _pick(symbol, now):
        return f"{symbol[:-4]}USDT_261226", now + 60 * 86400
    monkeypatch.setattr(carry_q, "pick_contract", _pick)

    async def _no_funding(p, now):
        return 0.0
    monkeypatch.setattr(carry, "_settle_funding", _no_funding)


def _enable(monkeypatch, book):
    from experiments import books
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    kv.delete_sync(capital._k("added", book), capital._k("paused"))


def test_carry_reduces_to_target_after_a_capital_cut(monkeypatch, prices):
    _enable(monkeypatch, "carry")
    monkeypatch.setattr(carry, "COINS", ["BTCUSDT"])
    monkeypatch.setattr(carry, "NOTIONAL_X", 1.0)
    asyncio.run(carry.tick(1_000.0))
    (p,) = carry.open_positions()
    assert round(p["notional"]) == 10_000
    # The book is cut to $4k: the position is reduced, not closed and reopened.
    kv.set_sync(capital._k("added", "carry"), -6_000.0)
    asyncio.run(carry.tick(2_000.0))
    opens, closed = carry.open_positions(), carry.closed_positions()
    assert len(opens) == 1 and len(closed) == 1
    assert abs(opens[0]["notional"] - carry.target_notional()) < 50
    assert closed[0]["close_reason"] == "trim_to_capital" and round(closed[0]["notional"] + opens[0]["notional"]) == 10_000
    assert opens[0]["qty"] + closed[0]["qty"] == pytest.approx(p["qty"])
    # Steady afterwards: nothing more opens or closes.
    asyncio.run(carry.tick(3_000.0))
    assert len(carry.open_positions()) == 1 and len(carry.closed_positions()) == 1


def test_carry_q_reduces_and_keeps_the_locked_basis(monkeypatch, prices):
    _enable(monkeypatch, "carry_q")
    monkeypatch.setattr(carry_q, "COINS", ["ETHUSDT"])
    monkeypatch.setattr(carry_q, "NOTIONAL_X", 1.0)
    asyncio.run(carry_q.tick(1_000.0))
    (p,) = carry_q.open_positions()
    kv.set_sync(capital._k("added", "carry_q"), -5_000.0)
    asyncio.run(carry_q.tick(2_000.0))
    opens, closed = carry_q.open_positions(), carry_q.closed_positions()
    assert len(opens) == 1 and len(closed) == 1
    assert opens[0]["basis_ann"] == p["basis_ann"] and opens[0]["contract"] == p["contract"]
    assert abs(opens[0]["notional"] - carry_q.target_notional()) < 50
    kv.delete_sync(capital._k("added", "carry_q"))
