"""Index shadow: the reversal and cascade signals scored on the picked coin, the alt index and BTC+ETH; the cascade lane off."""
import asyncio
import time

import pytest

from autotrader import engine
from experiments import index_shadow as ix
from storage import state as kv


def test_score_is_in_the_trade_direction_and_net_of_a_round_trip():
    short = {"direction": "short", "notional_usd": 1000.0, "pnl_usd": 12.0}
    s = ix.score(short, coin=-2.0, index=-3.0, majors=-1.0)
    assert s["coin_pct"] == pytest.approx(2.0 - ix.FEE_RT_PCT) and s["index_pct"] == pytest.approx(3.0 - ix.FEE_RT_PCT)
    assert s["majors_pct"] == pytest.approx(1.0 - ix.FEE_RT_PCT) and s["actual_pct"] == pytest.approx(1.2)
    assert ix.score({"direction": "long", "notional_usd": 0}, 1.0, 1.0, None)["majors_pct"] is None


def test_record_prices_each_leg_from_the_bars(monkeypatch):
    prices = {"HYPEUSDT": (100.0, 95.0), "BTCUSDT": (50.0, 49.0)}

    async def price_at(sym, ts):
        a, b = prices.get(sym, (10.0, 9.8))                 # every index member −2%
        return a if ts < 1_500 else b
    monkeypatch.setattr(ix, "price_at", price_at)
    t = {"id": "ix-t1", "symbol": "HYPEUSDT", "direction": "short", "opened_ts": 1_000.0, "closed_ts": 2_000.0, "notional_usd": 1000.0, "pnl_usd": 40.0}
    kv.delete_sync(ix._k("index", "reversal"))
    rec = asyncio.run(ix.record("reversal", t))
    assert rec["coin_pct"] == pytest.approx(5.0 - 0.1) and rec["index_pct"] == pytest.approx(2.0 - 0.1)
    assert rec["majors_pct"] == pytest.approx((2.0 + 2.0) / 2 - 0.1) and ix.records("reversal")[-1]["trade_id"] == "ix-t1"


def test_entries_close_together_are_one_episode():
    rows = [{"opened_ts": 0, "symbol": "A", "coin_pct": 1.0, "index_pct": 2.0, "majors_pct": None, "actual_pct": 0.5},
            {"opened_ts": 3600, "symbol": "B", "coin_pct": 3.0, "index_pct": 2.0, "majors_pct": None, "actual_pct": 1.5},
            {"opened_ts": 3 * 3600 + 3600, "symbol": "C", "coin_pct": -1.0, "index_pct": 0.0, "majors_pct": 1.0, "actual_pct": -1.0}]
    eps = ix.episodes(rows)
    assert [e["n"] for e in eps] == [2, 1] and eps[0]["coin_pct"] == 2.0 and eps[0]["majors_pct"] is None


def test_the_verdict_waits_for_forty_episodes_then_needs_a_gap_both_halves_agree_on():
    ep = lambda c, i: {"coin_pct": c, "index_pct": i}
    assert ix.verdict([ep(0, 1)] * 39, 0.0)["verdict"] == "measuring"
    better = [ep(0.0, 0.5 + 0.1 * (k % 3)) for k in range(40)]
    v = ix.verdict(better, base=0.1)
    assert v["verdict"] == "index better" and v["timing_vs_baseline"]["real"]
    assert ix.verdict([ep(0.5 + 0.1 * (k % 3), 0.0) for k in range(40)], None)["verdict"] == "coin better"
    first_half_only = [ep(0.0, 1.0 + 0.1 * (k % 3)) for k in range(20)] + [ep(0.0, -0.2 - 0.1 * (k % 3)) for k in range(20)]
    assert ix.verdict(first_half_only, None)["verdict"] == "no difference"


def test_the_cascade_lane_is_off_and_reserves_no_budget(monkeypatch):
    assert engine.CASCADE_IN_AUTO is False
    assert engine._lane_budget("cascade", 1e9) == 0.0


def test_a_switched_off_lane_opens_nothing_but_closes_its_mirror_with_the_book(tmp_path, monkeypatch):
    from experiments import books
    from trading.paper_trader import PaperWallet
    src = PaperWallet(str(tmp_path / "src.db"), label="cascade", initial_balance=500)
    active = PaperWallet(str(tmp_path / "active.db"), label="auto", initial_balance=5_000)
    monkeypatch.setitem(books.WALLETS, "cascade", src)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(engine, "_active", lambda: active)
    monkeypatch.setattr(engine, "_wallets", lambda: [active])

    async def not_paused():
        return False

    async def price(sym):
        return 101.0
    monkeypatch.setattr(engine, "is_paused", not_paused)
    monkeypatch.setattr(engine.monitor, "live_price", price)
    key = kv.key("auto", "cascade_mirror")
    asyncio.run(kv.delete(key))
    held = src.open_trade(symbol="SOLUSDT", direction="long", entry_price=100.0, stop_loss=92.0, take_profit_1=150.0, take_profit_2=190.0,
                          notional_usd=50.0, timeframe="4h", leverage=3, extra={"exit_policy": "manual", "shadows": False, "strategy": "cascade"})
    ours = active.open_trade(symbol="SOLUSDT", direction="long", entry_price=100.0, stop_loss=92.0, take_profit_1=150.0, take_profit_2=190.0,
                             notional_usd=300.0, timeframe="4h", leverage=3, extra={"exit_policy": "manual", "shadows": False, "lane": "cascade"})
    asyncio.run(kv.hset(key, held["id"], {"trade": ours["id"], "symbol": "SOLUSDT", "opened": time.time()}))
    fresh = src.open_trade(symbol="ETHUSDT", direction="long", entry_price=100.0, stop_loss=92.0, take_profit_1=150.0, take_profit_2=190.0,
                           notional_usd=50.0, timeframe="4h", leverage=3, extra={"exit_policy": "manual", "shadows": False, "strategy": "cascade"})
    src.annotate(fresh["id"], opened_ts=time.time(), expires_at=time.time() + 4 * 3600)
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", False))
    assert [t["symbol"] for t in active.get_portfolio()["open_trades"]] == ["SOLUSDT"]          # nothing new opened
    src.close_trade_manually(held["id"], 101.0, "expired")
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", False))
    assert active.get_portfolio()["open_trades"] == []                                           # ours closed with the book
    assert held["id"] not in asyncio.run(kv.hgetall(key))
