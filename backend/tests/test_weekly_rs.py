import asyncio
from datetime import datetime, timezone

import pytest

from experiments import books, weekly_rs
from trading.paper_trader import PaperWallet


@pytest.fixture
def w(tmp_path, monkeypatch):
    monkeypatch.setattr(books, "ENABLED", True)
    wallet = PaperWallet(str(tmp_path / "w.db"), label="weekly_rs")
    wallet.reset_portfolio(10_000)
    monkeypatch.setattr(books, "WALLETS", {**books.WALLETS, "weekly_rs": wallet})
    scores = {s: -i / 100 for i, s in enumerate(weekly_rs.UNIVERSE)}            # ETH at its high … RUNE furthest below

    async def sc():
        return dict(scores)
    monkeypatch.setattr(weekly_rs, "scores", sc)

    async def price(symbol):
        return 10.0
    monkeypatch.setattr(weekly_rs.monitor, "live_price", price)
    return wallet, scores


MONDAY = datetime(2026, 10, 26, 0, 5, tzinfo=timezone.utc)


def test_monday_rebalance_goes_long_the_strong_and_short_the_weak_once_a_week(w):
    wallet, scores = w
    assert asyncio.run(weekly_rs.rebalance(now=datetime(2026, 10, 25, 12, tzinfo=timezone.utc))) is None    # Sunday
    out = asyncio.run(weekly_rs.rebalance(now=MONDAY))
    held = {t["symbol"]: t["direction"] for t in wallet.get_portfolio()["open_trades"]}
    assert sorted(s for s, d in held.items() if d == "long") == sorted(weekly_rs.UNIVERSE[:5])
    assert sorted(s for s, d in held.items() if d == "short") == sorted(weekly_rs.UNIVERSE[-5:])
    assert len(out["opened"]) == 10
    assert asyncio.run(weekly_rs.rebalance(now=MONDAY.replace(hour=9))) is None                            # done this week
    t = next(t for t in wallet.get_portfolio()["open_trades"] if t["symbol"] == "ETHUSDT")
    assert t["stop_loss"] == pytest.approx(10.0 * 0.7) and t["exit_policy"] == "manual"


def test_next_week_keeps_holdovers_and_swaps_the_rest(w):
    wallet, scores = w
    asyncio.run(weekly_rs.rebalance(now=MONDAY))
    scores["ETHUSDT"] = -0.9                                                     # ETH collapses to the bottom
    from datetime import timedelta
    out = asyncio.run(weekly_rs.rebalance(now=MONDAY + timedelta(days=7)))
    assert "ETHUSDT" in out["closed"] and "short ETHUSDT" in out["opened"] and "long SOLUSDT" not in out["opened"]
