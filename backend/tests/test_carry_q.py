import asyncio
import time

import pytest

from experiments import books, carry_q
from trading import book_switches

NOW = time.time()
DELIV = {"BTCUSDT_A": NOW + 60 * 86400, "BTCUSDT_B": NOW + 150 * 86400}


@pytest.fixture
def q(monkeypatch):
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(carry_q, "COINS", ["BTCUSDT"])
    prices = {"spot": 80_000.0, "BTCUSDT_A": 80_800.0, "BTCUSDT_B": 81_800.0}

    async def contracts(now=None):
        return {"BTCUSDT": [("BTCUSDT_A", DELIV["BTCUSDT_A"]), ("BTCUSDT_B", DELIV["BTCUSDT_B"])]}
    monkeypatch.setattr(carry_q, "contracts", contracts)

    async def px(symbol, contract):
        return prices["spot"], prices[contract]
    monkeypatch.setattr(carry_q, "_prices", px)
    return prices


def test_shorts_the_current_quarter_and_locks_its_basis(q):
    asyncio.run(carry_q.tick(NOW))
    p = carry_q.open_positions()[0]
    assert p["contract"] == "BTCUSDT_A" and p["notional"] == pytest.approx(10_000 * carry_q.NOTIONAL_X)
    assert p["basis_ann"] == pytest.approx((80_800 * (1 - 0.0003) / (80_000 * 1.0003) - 1) * 365 / 60, rel=1e-3)   # ≈ +5.7%/yr
    st = carry_q.status()["positions"][0]
    assert st["basis_locked_pct"] > 5 and st["days_to_delivery"] == pytest.approx(60, abs=0.1)


def test_price_moves_cancel_and_convergence_pays(q):
    asyncio.run(carry_q.tick(NOW))
    q["spot"], q["BTCUSDT_A"] = 88_000.0, 88_000.0                 # +10% on both legs, basis gone (as at delivery)
    asyncio.run(carry_q.tick(NOW + 30 * 86400))                    # mid-life, before the roll window
    p = carry_q.open_positions()[0]
    pnl = carry_q.mark_pnl(p, 88_000.0, 88_000.0)
    assert 0 < pnl < p["notional"] * 0.012                           # the basis (~1%) minus fees, not the 10% move


def test_rolls_a_week_before_delivery_and_unwinds_when_off(q):
    asyncio.run(carry_q.tick(NOW))
    asyncio.run(carry_q.tick(DELIV["BTCUSDT_A"] - 6 * 86400))          # inside ROLL_DAYS
    ps = carry_q.open_positions()
    assert [p["contract"] for p in ps] == ["BTCUSDT_B"] and carry_q.closed_positions()[0]["close_reason"] == "roll"
    asyncio.run(book_switches.set_enabled("carry_q", False))
    asyncio.run(carry_q.tick(DELIV["BTCUSDT_A"]))
    assert not carry_q.open_positions() and carry_q.closed_positions()[-1]["close_reason"] == "switched_off"


def test_raising_the_target_tops_up_instead_of_reopening(q, monkeypatch):
    monkeypatch.setattr(carry_q, "NOTIONAL_X", 1.0)
    asyncio.run(carry_q.tick(NOW))
    first = {p["id"] for p in carry_q.open_positions()}
    monkeypatch.setattr(carry_q, "NOTIONAL_X", 3.0)
    asyncio.run(carry_q.tick(NOW + 60))
    ps = carry_q.open_positions()
    assert first <= {p["id"] for p in ps} and len(ps) == 2
    assert sum(p["notional"] for p in ps) == pytest.approx(carry_q.target_notional(), rel=0.01)
