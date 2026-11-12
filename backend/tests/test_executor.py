"""The autotrader as executor: lane budgets, mirroring a signal book, the reversal score, signal ledgers hidden."""
import asyncio

import pytest

from autotrader import engine, lanes, reversal
from experiments import books
from storage import state as kv
from trading import capital, overview
from trading.paper_trader import PaperWallet


@pytest.fixture(autouse=True)
def _no_coin_tape(monkeypatch):
    """The per-coin entry gate reads Binance; tests see no tape (no gate) unless they set one."""
    from trading import tape

    async def none(symbol, now=None):
        return None
    monkeypatch.setattr(tape, "coin_tape", none)


def test_lane_of_recognises_every_lane_marker():
    assert lanes.lane_of({"cascade_id": "x"}) == "cascade" and lanes.lane_of({"factor_id": "y"}) == "factor"
    assert lanes.lane_of({"reversal_id": "z"}) == "reversal" and lanes.lane_of({"weekly_id": "w"}) == "weekly"
    assert lanes.lane_of({"lane": "events"}) == "events" and lanes.lane_of({}) == "ta"
    assert set(lanes.LANE_BOOK) == set(lanes.LANES)


def test_lane_budget_is_the_books_plan_entry_scaled_to_the_ledger(monkeypatch):
    monkeypatch.setattr(engine, "CASCADE_IN_AUTO", True)                 # the lane is off by default since 2026-11-11
    monkeypatch.setattr(capital, "PLAN", {"auto": 50_000.0, "events": 8_000.0, "unlocks": 4_000.0, "weekly_rs": 1_000.0, "cascade": 500.0, "multi_factor": 1_000.0, "main": 500.0})
    kv.delete_sync(capital._k("plan"))
    # a $50k ledger: every lane gets its budget in full
    assert engine._lane_budget("events", 50_000.0) == 8_000.0 and engine._lane_budget("cascade", 50_000.0) == 500.0
    assert engine._lane_budget("reversal", 50_000.0) == engine.ENTRY_BUDGET_USD
    assert engine._lane_budget("ta", 50_000.0) == 0.0                                  # the retired scanner lane reserves nothing
    # a $5k ledger: the budgets (14,500 + the reversal lane's 2,000) are scaled down pro rata
    total = 14_500 + engine.ENTRY_BUDGET_USD
    assert engine._lane_budget("events", 5_000.0) == pytest.approx(5_000 * 8_000 / total)
    assert engine._lane_budget("reversal", 5_000.0) == pytest.approx(5_000 * engine.ENTRY_BUDGET_USD / total)
    monkeypatch.setattr(engine, "TA_LANE", True)                                       # switched back on, it has its budget again
    assert engine._lane_budget("ta", 50_000.0) == engine.ENTRY_BUDGET_USD


def _scan(sym, conv, ret24, oi, top_long):
    return {"symbol": sym, "conviction": conv, "composite_score": conv * 50, "ta_score": conv * 40,
            "fa": {"score": conv * 10, "features": {"ret_24h": ret24, "oi_change_24h_pct": oi, "top_trader_long_pct": top_long}, "components": {"liquidity": {"score": 0.5}}}}


def test_reversal_score_fades_what_the_scanner_likes():
    scans = [_scan(f"C{i}USDT", conv=i / 10, ret24=i, oi=i, top_long=50 - i) for i in range(10)]
    sc = reversal.score(scans, {"conviction": -1.0, "ret_24h": -1.0, "oi_change_24h_pct": -1.0, "top_trader_long_pct": 1.0})
    assert len(sc) == 10
    ranked = sorted(sc, key=sc.get)
    assert ranked[0] == "C9USDT" and ranked[-1] == "C0USDT"             # the most-liked coin scores lowest: it is the short
    assert reversal.score(scans[:4]) == {}                               # too few coins to rank
    r = reversal.readings(scans[3])
    assert r["conviction"] == 0.3 and r["fa_liquidity"] == 0.5 and r["ret_24h"] == 3.0


def test_mirror_wallet_book_opens_sizes_and_closes_with_the_books_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CASCADE_IN_AUTO", True)                 # the lane is off by default since 2026-11-11
    src = PaperWallet(str(tmp_path / "src.db"), label="cascade", initial_balance=500)
    active = PaperWallet(str(tmp_path / "active.db"), label="auto", initial_balance=5_000)
    monkeypatch.setitem(books.WALLETS, "cascade", src)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(engine, "_active", lambda: active)
    monkeypatch.setattr(engine, "_wallets", lambda: [active])
    monkeypatch.setattr(capital, "PLAN", {"auto": 1_000.0, "events": 8_000.0, "unlocks": 4_000.0, "weekly_rs": 1_000.0, "cascade": 500.0, "multi_factor": 1_000.0})
    kv.delete_sync(capital._k("plan"))

    async def not_paused():
        return False

    async def no_regime():
        return None

    async def price(sym):
        return 100.0
    monkeypatch.setattr(engine, "is_paused", not_paused)
    monkeypatch.setattr(engine.regime, "current", no_regime)
    monkeypatch.setattr(engine.monitor, "live_price", price)
    asyncio.run(kv.delete(kv.key("auto", "cascade_mirror")))
    asyncio.run(kv.set(kv.key("auto", "cascade_since"), {"at": 1.0}, 3600))          # the lane has been following for a while
    t = src.open_trade(symbol="SOLUSDT", direction="long", entry_price=100.0, stop_loss=92.0, take_profit_1=150.0, take_profit_2=190.0,
                       notional_usd=50.0, timeframe="4h", leverage=3, extra={"exit_policy": "manual", "shadows": False, "strategy": "cascade"})
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    ours = active.get_portfolio()["open_trades"]
    assert len(ours) == 1 and ours[0]["symbol"] == "SOLUSDT" and ours[0]["lane"] == "cascade" and ours[0]["cascade_id"] == t["id"]
    # the book used 10% of its equity and the cascade lane's budget on a $5k ledger is $500 × 5,000/18,500 — ~$14 — but every
    # leg risks at least LANE_RISK_PCT of the ledger at its 8% stop: 3% × $5,000 / 8% = $1,875 (under the 50% cap)
    assert ours[0]["notional_usd"] == pytest.approx(max(0.10 * engine._lane_budget("cascade", 5_000.0), 5_000 * engine.LANE_RISK_PCT / 8.0), rel=0.05)
    assert ours[0]["stop_loss"] == pytest.approx(92.0, rel=0.01)
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    assert len(active.get_portfolio()["open_trades"]) == 1                 # not twice
    src.close_trade_manually(t["id"], 103.0, "expired")
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    assert active.get_portfolio()["open_trades"] == []
    assert active.all_trades()["closed"][0]["close_reason"] == "cascade_expired"
    assert asyncio.run(kv.hgetall(kv.key("auto", "cascade_mirror"))) == {}


def test_signal_ledgers_are_hidden_and_hold_no_plan_money(monkeypatch):
    assert {"cascade", "weekly_rs", "multi_factor", "events", "unlocks"} <= set(overview.SIGNAL_SYSTEMS)
    rows = [{"system": "cascade", "status": "closed", "closed_ts": 1.0, "symbol": "X", "direction": "long"},
            {"system": "auto_live", "status": "closed", "closed_ts": 1.0, "symbol": "X", "direction": "long"}]
    assert not overview.matches(rows[0], {}) and overview.matches(rows[1], {})
    assert overview.matches(rows[0], {"include_retired": True})
    moves = []
    monkeypatch.setattr(capital, "deposit", lambda b, a, note=None: moves.append((b, a)) or {"book": b, "amount": a})
    monkeypatch.setattr(capital, "withdraw", lambda b, a, note=None: moves.append((b, -a)) or {"book": b, "amount": -a})
    monkeypatch.setattr(capital, "shortfall", lambda b: 999.0)
    monkeypatch.setattr(capital, "PLAN", {"cascade": 500.0, "auto": 1_000.0})
    kv.delete_sync(capital._k("plan"))
    capital.apply_plan()
    assert [b for b, _ in moves] == ["auto"]


def test_mirror_ignores_what_the_book_already_held_when_the_lane_began(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CASCADE_IN_AUTO", True)                 # the lane is off by default since 2026-11-11
    src = PaperWallet(str(tmp_path / "src2.db"), label="cascade", initial_balance=500)
    active = PaperWallet(str(tmp_path / "active2.db"), label="auto", initial_balance=5_000)
    monkeypatch.setitem(books.WALLETS, "cascade", src)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(engine, "_active", lambda: active)
    monkeypatch.setattr(engine, "_wallets", lambda: [active])

    async def not_paused():
        return False

    async def no_regime():
        return None

    async def price(sym):
        return 100.0
    monkeypatch.setattr(engine, "is_paused", not_paused)
    monkeypatch.setattr(engine.regime, "current", no_regime)
    monkeypatch.setattr(engine.monitor, "live_price", price)
    asyncio.run(kv.delete(kv.key("auto", "cascade_mirror")))
    asyncio.run(kv.delete(kv.key("auto", "cascade_since")))
    old = src.open_trade(symbol="SOLUSDT", direction="long", entry_price=100.0, stop_loss=92.0, take_profit_1=150.0, take_profit_2=190.0,
                         notional_usd=50.0, timeframe="4h", leverage=3, extra={"exit_policy": "manual", "shadows": False})
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    assert active.get_portfolio()["open_trades"] == []                              # held before the lane began: left alone
    new = src.open_trade(symbol="ETHUSDT", direction="short", entry_price=100.0, stop_loss=108.0, take_profit_1=50.0, take_profit_2=10.0,
                         notional_usd=50.0, timeframe="4h", leverage=3, extra={"exit_policy": "manual", "shadows": False})
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    ours = active.get_portfolio()["open_trades"]
    assert [t["symbol"] for t in ours] == ["ETHUSDT"] and ours[0]["cascade_id"] == new["id"]  # opened after: followed
    src.close_trade_manually(old["id"], 101.0, "expired")
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    assert old["id"] not in asyncio.run(kv.hgetall(kv.key("auto", "cascade_mirror")))      # the book closed it: forgotten
    asyncio.run(kv.delete(kv.key("auto", "cascade_mirror")))
    asyncio.run(kv.delete(kv.key("auto", "cascade_since")))


def test_reversal_lane_sells_the_top_of_the_score_and_buys_nothing_by_default():
    scores = {f"C{i}USDT": i / 10 for i in range(8)}
    assert reversal.picks(scores, 2) == [("C0USDT", "short"), ("C1USDT", "short")]
    assert reversal.picks(scores, 2, ("short", "long")) == [("C0USDT", "short"), ("C1USDT", "short"), ("C6USDT", "long"), ("C7USDT", "long")]
    assert engine.REVERSAL_SIDES == ("short",)


def test_a_leg_never_opposes_a_position_another_system_holds():
    held = [{"symbol": "WLDUSDT", "direction": "short", "system": "weekly_rs"},
            {"symbol": "BTCUSDT", "direction": "short", "event_leg": "hedge"}]
    assert engine.opposite_holding("WLDUSDT", "long", held)["system"] == "weekly_rs"
    assert engine.opposite_holding("WLDUSDT", "short", held) is None                  # the same side is not a conflict
    assert engine.opposite_holding("BTCUSDT", "long", held) is None                   # a hedge leg is not a view
    assert engine.opposite_holding("ETHUSDT", "long", held) is None


def test_the_scanner_lane_is_retired_by_default():
    assert engine.TA_LANE is False and "retired" in lanes.LANES["ta"]


def test_a_mirror_leg_is_deferred_while_the_coin_is_being_dumped(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CASCADE_IN_AUTO", True)                 # the lane is off by default since 2026-11-11
    """TRUMP, 2026-11-04 17:29 UTC: the factor basket bought a coin 5% down on the half hour on 5× volume and it fell
    another 5% in the next five minutes. A leg is not opened into a move running against it; the next tick retries."""
    from trading import tape
    src = PaperWallet(str(tmp_path / "src.db"), label="cascade", initial_balance=500)
    active = PaperWallet(str(tmp_path / "active.db"), label="auto", initial_balance=5_000)
    monkeypatch.setitem(books.WALLETS, "cascade", src)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(engine, "_active", lambda: active)
    monkeypatch.setattr(engine, "_wallets", lambda: [active])
    monkeypatch.setattr(capital, "PLAN", {"auto": 1_000.0, "cascade": 500.0})
    kv.delete_sync(capital._k("plan"))

    async def not_paused():
        return False

    async def no_regime():
        return None

    async def price(sym):
        return 100.0
    state = {"tape": {"move_pct": -5.1, "vol_x": 4.8}}

    async def coin_tape(symbol, now=None):
        return state["tape"]
    monkeypatch.setattr(engine, "is_paused", not_paused)
    monkeypatch.setattr(engine.regime, "current", no_regime)
    monkeypatch.setattr(engine.monitor, "live_price", price)
    monkeypatch.setattr(tape, "coin_tape", coin_tape)
    asyncio.run(kv.delete(kv.key("auto", "cascade_mirror")))
    asyncio.run(kv.set(kv.key("auto", "cascade_since"), {"at": 1.0}, 3600))
    src.open_trade(symbol="TRUMPUSDT", direction="long", entry_price=100.0, stop_loss=70.0, take_profit_1=150.0, take_profit_2=190.0,
                   notional_usd=50.0, timeframe="4h", leverage=3, extra={"exit_policy": "manual", "shadows": False, "strategy": "cascade"})
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    assert active.get_portfolio()["open_trades"] == []                                   # deferred, not opened
    assert asyncio.run(kv.hgetall(kv.key("auto", "cascade_mirror"))) == {}               # and not recorded as mirrored
    state["tape"] = {"move_pct": -0.4, "vol_x": 1.1}                                     # the dump is over: next tick takes it
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    assert [t["symbol"] for t in active.get_portfolio()["open_trades"]] == ["TRUMPUSDT"]


def _reversal_setup(tmp_path, monkeypatch, equity=50_000):
    from trading import coin_lock
    active = PaperWallet(str(tmp_path / "active.db"), label="auto", initial_balance=equity)
    monkeypatch.setattr(engine, "_active", lambda: active)
    monkeypatch.setattr(engine, "_wallets", lambda: [active])
    monkeypatch.setattr(engine, "_holdings_elsewhere", lambda include_books=True: [])

    async def no_regime():
        return None

    async def price(sym):
        return 1248.0
    monkeypatch.setattr(engine.regime, "current", no_regime)
    monkeypatch.setattr(engine.monitor, "live_price", price)
    kv.delete_sync(coin_lock._stop_key("ZECUSDT", "short"))
    return active


def _reversal_leg(active):
    equity = active.get_portfolio()["total_equity"]
    notional_pct = engine.ENTRY_BUDGET_USD / engine.REVERSAL_K / equity * 100
    return asyncio.run(engine._open_mirror_leg(
        active, "ZECUSDT", "short", engine.REVERSAL_STOP_PCT, notional_pct, "reversal score -0.2", "24h", None,
        {"lane": "reversal", "reversal_id": "1:ZECUSDT", "reversal_score": -0.2}, risk_pct=engine.REVERSAL_RISK_PCT))


def test_a_reversal_leg_stays_at_measuring_size_on_a_large_ledger(tmp_path, monkeypatch):
    """ZEC, 2026-11-06: with $50k on the prod ledger the 1% risk floor made a $5k leg at a 10% stop — $507 at risk on a
    lane whose five wins had made $516 together. A leg now risks 0.2% of the ledger: $1k notional, $100 at risk."""
    active = _reversal_setup(tmp_path, monkeypatch)
    t = _reversal_leg(active)
    assert t and not t.get("error")
    assert t["notional_usd"] == pytest.approx(1_000.0, rel=0.01)
    assert t["risk_usd"] == pytest.approx(100.0, rel=0.02)
    assert t["stop_loss"] == pytest.approx(1248.0 * 1.10, rel=1e-6)


def test_a_leg_waits_out_the_cooldown_after_a_stop_on_the_same_side(tmp_path, monkeypatch):
    from trading import coin_lock
    active = _reversal_setup(tmp_path, monkeypatch)
    coin_lock.record_stop_out("copy_trading", {"symbol": "ZECUSDT", "direction": "short", "close_reason": "stop_loss", "current_price": 1382.0, "pnl_usd": -546.0})
    assert _reversal_leg(active) is None
    assert active.get_portfolio()["open_trades"] == []
    kv.delete_sync(coin_lock._stop_key("ZECUSDT", "short"))
    assert _reversal_leg(active)["symbol"] == "ZECUSDT"
