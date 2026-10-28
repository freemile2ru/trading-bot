"""Scalper: queue-model fills, costs, exits, risk limits, features, strategy gates, storage."""

import asyncio
import time

import pytest

from scalper import config as C
from scalper.book import Book
from scalper.market import MarketState
from scalper.store import Store
from trading import futures

T0 = 1_800_000_000.0


def _entry(book, side="long", price=100.0, ahead=5.0, sl_bps=10.0, tp_bps=20.0):
    o = book.place_entry("BTCUSDT", side, price, sl_bps, tp_bps, ahead, price, 1.0, T0, 1, {})
    assert not isinstance(o, str)
    return o


# ── Queue model ────────────────────────────────────────────────────────────────

def test_maker_entry_waits_for_the_queue_ahead():
    b = Book(10_000)
    o = _entry(b, ahead=5.0)
    assert not b.on_trade("BTCUSDT", 100.0, 3.0, True, T0 + 1)        # 3 of 5 ahead consumed
    assert not b.positions
    ev = b.on_trade("BTCUSDT", 100.0, 3.0, True, T0 + 2)               # queue exhausted → our turn
    assert ev[0]["type"] == "entry_filled" and "BTCUSDT" in b.positions
    assert ev[0]["order"].time_to_fill == pytest.approx(2.0)


def test_trades_on_the_wrong_side_do_not_consume_our_queue():
    b = Book(10_000)
    _entry(b, ahead=1.0)
    b.on_trade("BTCUSDT", 100.0, 50.0, False, T0 + 1)                  # buyer lifting the ask — not our bid
    assert not b.positions


def test_trade_through_fills_regardless_of_queue():
    b = Book(10_000)
    _entry(b, ahead=1e9)
    b.on_trade("BTCUSDT", 99.99, 0.001, True, T0 + 1)
    assert "BTCUSDT" in b.positions


def test_cancellations_ahead_move_us_up():
    b = Book(10_000)
    o = _entry(b, ahead=10.0)
    b.on_depth("BTCUSDT", [(100.0, 2.0)], [(100.1, 1.0)])
    assert o.queue_ahead == 2.0


def test_the_fill_trade_does_not_also_trigger_the_stop():
    b = Book(10_000)
    _entry(b, ahead=0.0)
    ev = b.on_trade("BTCUSDT", 99.0, 1.0, True, T0 + 1)                # through the bid AND below the stop
    assert [e["type"] for e in ev] == ["entry_filled", "order_placed"]


# ── Exits & costs ──────────────────────────────────────────────────────────────

def test_take_profit_is_maker_and_net_of_fees():
    b = Book(10_000)
    _entry(b, ahead=0.0, tp_bps=20.0)
    b.on_trade("BTCUSDT", 99.99, 0.001, True, T0 + 1)
    ev = b.on_trade("BTCUSDT", 100.21, 0.01, False, T0 + 30)           # trades through the 100.20 target
    t = next(e for e in ev if e["type"] == "position_closed")["trade"]
    assert t["exit_reason"] == "take_profit" and t["exit_maker"] == 1
    expected_fees = futures.fee(t["notional"], True) + futures.fee(t["qty"] * 100.2, True)
    assert t["fees"] == pytest.approx(expected_fees, rel=1e-6)
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["fees"], abs=1e-9)


def test_stop_is_taker_with_slippage_and_risk_is_sized():
    b = Book(10_000)
    _entry(b, ahead=0.0, sl_bps=10.0)
    b.on_trade("BTCUSDT", 99.99, 0.001, True, T0 + 1)
    pos = b.positions["BTCUSDT"]
    assert pos.risk_usd == pytest.approx(10_000 * C.RISK_PCT / 100, rel=0.01)
    ev = b.on_trade("BTCUSDT", 99.9, 0.01, True, T0 + 5)
    t = ev[-1]["trade"]
    assert t["exit_reason"] == "stop" and t["exit_maker"] == 0
    assert t["exit_price"] < 99.9                                        # slipped
    assert -1.1 < t["r"] < -0.9                                          # all-in sizing: a stop ≈ −1R


def test_tp_order_is_cancelled_when_position_stops_out():
    b = Book(10_000)
    _entry(b, ahead=0.0)
    b.on_trade("BTCUSDT", 99.99, 0.001, True, T0 + 1)
    b.on_trade("BTCUSDT", 99.8, 0.01, True, T0 + 2)
    assert not b.orders


def test_funding_charged_once_per_settlement():
    b = Book(10_000)
    o = _entry(b, ahead=0.0)
    o.meta["next_funding_ms"] = int((T0 + 10) * 1000)
    b.on_trade("BTCUSDT", 99.99, 0.001, True, T0 + 1)
    assert b.on_mark("BTCUSDT", 100.0, 0.0001, T0 + 5) == []
    ev = b.on_mark("BTCUSDT", 100.0, 0.0001, T0 + 11)
    assert ev and ev[0]["amount"] > 0                                    # long pays positive funding
    assert b.on_mark("BTCUSDT", 100.0, 0.0001, T0 + 12) == []


# ── Risk limits ────────────────────────────────────────────────────────────────

def test_daily_loss_limit_halts_new_entries():
    b = Book(10_000)
    b.day_pnl = -10_000 * C.DAILY_LOSS_LIMIT_PCT / 100 - 1
    _entry(b, ahead=0.0)
    b.on_trade("BTCUSDT", 99.99, 0.001, True, T0 + 1)
    b.on_trade("BTCUSDT", 99.8, 0.01, True, T0 + 2)                     # a loss while over the limit
    ok, why = b.can_enter("ETHUSDT", time.time())
    assert not ok and "daily loss" in why


def test_cooldown_and_one_position_per_symbol():
    b = Book(10_000)
    _entry(b, ahead=0.0)
    assert not b.can_enter("BTCUSDT", T0)[0]                            # already active
    b.on_trade("BTCUSDT", 99.99, 0.001, True, T0 + 1)
    b.on_trade("BTCUSDT", 99.8, 0.01, True, T0 + 2)                     # stopped out
    ok, why = b.can_enter("BTCUSDT", T0 + 3)
    assert not ok and "cooldown" in why


def test_max_positions_and_hourly_cap(monkeypatch):
    monkeypatch.setattr(C, "MAX_TRADES_PER_HOUR", 2)
    b = Book(10_000)
    for sym in ("BTCUSDT", "ETHUSDT"):
        b.place_entry(sym, "long", 100.0, 10.0, 20.0, 0.0, 100.0, 1.0, T0, None, {})
    ok, why = b.can_enter("SOLUSDT", T0)
    assert not ok and "entries/hour" in why


# ── Market features & strategy ────────────────────────────────────────────────

def _warm_market(buy_bias: float = 0.0) -> MarketState:
    m = MarketState("BTCUSDT")
    for i in range(700):
        now = T0 + i
        m.on_depth([["100.0", "5"]], [["100.1", "5"]], None, now)
        m.on_trade(100.05, 1.0 + (buy_bias if i >= 670 else 0.0), False, None, now)   # aggressive buys
        m.on_trade(100.05, 1.0, True, None, now + 0.5)                                    # aggressive sells
    m.roll(T0 + 701)
    return m


def test_silent_seconds_become_flat_bars():
    m = MarketState("BTCUSDT")
    m.on_depth([["100", "1"]], [["101", "1"]], None, T0)
    m.on_depth([["100", "1"]], [["101", "1"]], None, T0 + 5)
    assert [b.ts for b in m.bars] == [int(T0) + k for k in range(5)]


def test_flow_z_detects_a_one_sided_burst():
    calm, burst = _warm_market(0.0).features(), _warm_market(3.0).features()
    assert abs(calm["flow_z"]) < 1.0
    assert burst["flow_z"] > 2.0 and burst["flow_10s"] > 0


def _f(**kw):
    base = {"flow_z": 2.5, "flow_10s": 5.0, "obi": 0.3, "ema9": 101.0, "ema21": 100.0, "atr_bps": 8.0,
            "spread_bps": 1.0, "median_spread_bps": 1.0, "vwap_dev_atr": 0.5}
    return {**base, **kw}


# ── Storage ────────────────────────────────────────────────────────────────────

def test_store_roundtrip_and_labels(tmp_path):
    s = Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"))
    sid = s.add_signal({"ts": T0, "symbol": "BTCUSDT", "side": "long", "decision": "entered", "flow_z": 2.5})
    s.label_signal(sid, 30, 4.2)
    s.add_bars([("BTCUSDT", int(T0), 1, 2, 0.5, 1.5, 1.0, 0.1, 3.0, 2.0, 7, 1.2)])
    s.set_state("balance", 9_999.5)
    s.close()
    s2 = Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"))
    assert s2.query("trades", "SELECT fwd_30 FROM signals WHERE id=?", (sid,))[0]["fwd_30"] == 4.2
    assert s2.query("market", "SELECT trades FROM bars_1s")[0]["trades"] == 7
    assert s2.get_state("balance") == 9_999.5
    s2.close()


def test_restart_reloads_recent_history(tmp_path, monkeypatch):
    from scalper import engine as eng
    s = Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"))
    now = int(time.time())
    s.add_bars([("BTCUSDT", now - 300 + i, 100, 100.1, 99.9, 100, 1.0, 0.1, 1.0, 1.0, 3, 100.0) for i in range(290)])
    s.close()
    monkeypatch.setattr(C, "SYMBOLS", ["BTCUSDT"])
    e = eng.ScalperEngine(Store(str(tmp_path / "t.db"), str(tmp_path / "m.db")))
    assert e.markets["BTCUSDT"].history_seconds() == 290
    e.store.close()


# ── Cost model ─────────────────────────────────────────────────────────────────

from scalper import costs


def test_book_walk_prices_our_size_across_levels():
    asks = [(100.0, 1.0), (100.1, 1.0), (100.2, 1.0)]
    vwap, share = costs.walk(asks, 2.0, worse=1)
    assert vwap == pytest.approx(100.05) and share == 1.0
    vwap, share = costs.walk(asks, 4.0, worse=1)                       # beyond visible depth
    assert share == pytest.approx(0.75) and vwap > (100.0 + 100.1 + 100.2) / 3


def test_taker_exit_is_the_worse_of_book_and_trigger():
    depth = ([(99.95, 10.0)], [(100.05, 10.0)])
    long_exit = costs.taker_exit_price("long", 1.0, depth, trigger=99.90)     # trade printed below the stale bid
    assert long_exit < 99.90
    short_exit = costs.taker_exit_price("short", 1.0, depth, trigger=100.00)
    assert short_exit > 100.05


def _filled_long(b):
    _entry(b, ahead=0.0)
    b.on_depth("BTCUSDT", [(99.99, 50.0)], [(100.01, 3.0)])
    b.on_trade("BTCUSDT", 99.99, 0.001, True, T0 + 1)
    return b.positions["BTCUSDT"]


def test_passive_exit_rests_at_the_touch_and_fills_as_maker():
    b = Book(10_000)
    pos = _filled_long(b)
    o = b.place_passive_exit("BTCUSDT", "time_stop", T0 + 200, ttl=5)
    assert o.price == 100.01 and o.queue_ahead == 3.0                  # long exit sells on the ask
    assert b.place_passive_exit("BTCUSDT", "time_stop", T0 + 201, ttl=5) is None   # one at a time
    ev = b.on_trade("BTCUSDT", 100.01, 4.0, False, T0 + 202)            # buyers lift 4 > 3 ahead
    t = next(e for e in ev if e["type"] == "position_closed")["trade"]
    assert t["exit_reason"] == "time_stop (maker)" and t["exit_maker"] == 1 and t["slippage_usd"] == 0
    assert not b.orders                                                 # TP order cancelled too


def test_passive_exit_costs_less_than_a_market_exit():
    maker, taker = Book(10_000), Book(10_000)
    _filled_long(maker)
    _filled_long(taker)
    maker.place_passive_exit("BTCUSDT", "time_stop", T0 + 200, ttl=5)
    m = next(e for e in maker.on_trade("BTCUSDT", 100.01, 4.0, False, T0 + 202) if e["type"] == "position_closed")["trade"]
    t = taker.close_market("BTCUSDT", "time_stop", T0 + 202, 99.99)["trade"]
    assert m["cost_bps"] < t["cost_bps"]
    assert m["net_pnl"] > t["net_pnl"]


def test_unfilled_passive_exit_falls_back_to_market(tmp_path, monkeypatch):
    from scalper import engine as eng
    monkeypatch.setattr(C, "SYMBOLS", ["BTCUSDT"])
    e = eng.ScalperEngine(Store(str(tmp_path / "t.db"), str(tmp_path / "m.db")))
    _filled_long(e.book)
    e.markets["BTCUSDT"].on_depth([["99.99", "50"]], [["100.01", "3"]], None, T0 + 100)
    e.book.place_passive_exit("BTCUSDT", "flow_reversal", T0 + 100, ttl=5)
    e._expire_orders(T0 + 106)
    assert "BTCUSDT" not in e.book.positions
    e.store.close()                                                     # flushes the write queue
    rows = Store(str(tmp_path / "t.db"), str(tmp_path / "m.db")).query("trades", "SELECT exit_reason, exit_maker FROM trades")
    assert rows == [{"exit_reason": "flow_reversal (taker)", "exit_maker": 0}]


def test_bnb_discount_lowers_fees(monkeypatch):
    base = costs.fee(10_000, maker=False)
    monkeypatch.setattr(costs, "TAKER_FEE", costs.TAKER_FEE * 0.9)
    assert costs.fee(10_000, maker=False) == pytest.approx(base * 0.9)


def test_closing_a_position_reports_its_cancelled_tp():
    b = Book(10_000)
    _entry(b, ahead=0.0)
    b.on_trade("BTCUSDT", 99.99, 0.001, True, T0 + 1)
    ev = b.on_trade("BTCUSDT", 99.8, 0.01, True, T0 + 2)
    closed = next(e for e in ev if e["type"] == "position_closed")
    assert [o.purpose for o in closed["cancelled"]] == ["tp"]
    assert closed["cancelled"][0].status == "cancelled"


def test_restart_does_not_flatten_restored_positions(tmp_path, monkeypatch):
    """Regression: a restored position used to be flattened as 'feed_stale' before the feed connected."""
    from scalper import engine as eng
    monkeypatch.setattr(C, "SYMBOLS", ["BTCUSDT"])
    s = Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"))
    b = Book(10_000)
    _filled_long(b)
    s.set_state("positions", [__import__("dataclasses").asdict(p) for p in b.positions.values()])
    s.close()
    e = eng.ScalperEngine(Store(str(tmp_path / "t.db"), str(tmp_path / "m.db")))
    assert "BTCUSDT" in e.book.positions
    e.health = {"public": type("H", (), {"last_msg": 0.0})(), "market": type("H", (), {"last_msg": 0.0})()}
    e._staleness(e.started + 2)                        # 2s after start, feed not connected yet
    assert "BTCUSDT" in e.book.positions
    e._staleness(e.started + C.FLATTEN_STALE_SECONDS + 1)   # genuinely no data for 15s+
    assert "BTCUSDT" not in e.book.positions
    e.store.close()


def test_restart_keeps_the_original_tp_order_id(tmp_path, monkeypatch):
    from scalper import engine as eng
    monkeypatch.setattr(C, "SYMBOLS", ["BTCUSDT"])
    s = Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"))
    b = Book(10_000)
    pos = _filled_long(b)
    s.upsert_order(b.orders[pos.tp_order_id].record())
    s.upsert_order({"id": "stale-entry", "symbol": "ETHUSDT", "purpose": "entry", "status": "pending"})
    s.set_state("positions", [__import__("dataclasses").asdict(p) for p in b.positions.values()])
    s.close()
    e = eng.ScalperEngine(Store(str(tmp_path / "t.db"), str(tmp_path / "m.db")))
    assert e.book.positions["BTCUSDT"].tp_order_id == pos.tp_order_id
    e.store.close()
    rows = {r["id"]: r for r in Store(str(tmp_path / "t.db"), str(tmp_path / "m.db")).query("trades", "SELECT id, status, cancel_reason FROM orders")}
    assert rows[pos.tp_order_id]["status"] == "pending"                 # still live, same row
    assert rows["stale-entry"]["cancel_reason"] == "engine restart"


def test_daily_pnl_and_halt_survive_a_restart(tmp_path, monkeypatch):
    from scalper import engine as eng
    monkeypatch.setattr(C, "SYMBOLS", ["BTCUSDT"])
    s = Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"))
    loss = -(10_000 * C.DAILY_LOSS_LIMIT_PCT / 100 + 50)      # just past the configured limit
    s.add_trade({"id": "a", "symbol": "BTCUSDT", "exit_ts": time.time() - 60, "net_pnl": loss})
    s.set_state("balance", 10_000 + loss)
    s.close()
    e = eng.ScalperEngine(Store(str(tmp_path / "t.db"), str(tmp_path / "m.db")))
    assert e.book.day_pnl == pytest.approx(loss)
    ok, why = e.book.can_enter("ETHUSDT", time.time())
    assert not ok and "daily loss" in why
    e.store.close()


def test_scalper_leverage_is_capped_at_its_own_limit(monkeypatch):
    b = Book(10_000)
    o = b.place_entry("BTCUSDT", "long", 100.0, 25.0, 40.0, 0.0, 100.0, 1.0, T0, None, {})
    assert o.meta["leverage"] == C.MAX_LEVERAGE == 20
    monkeypatch.setattr(C, "MAX_LEVERAGE", 10)
    o2 = b.place_entry("ETHUSDT", "long", 100.0, 25.0, 40.0, 0.0, 100.0, 1.0, T0, None, {})
    assert o2.meta["leverage"] == 10
    assert o2.meta["margin"] == pytest.approx(o2.qty * 100.0 / 10)


def test_wider_stop_means_smaller_position_and_lower_fees():
    tight, wide = Book(10_000), Book(10_000)
    a = tight.place_entry("BTCUSDT", "long", 100.0, 12.0, 20.0, 0.0, 100.0, 1.0, T0, None, {})
    b = wide.place_entry("BTCUSDT", "long", 100.0, 25.0, 40.0, 0.0, 100.0, 1.0, T0, None, {})
    assert b.qty < a.qty * 0.65                                  # ~40% smaller for the same $ risk
    assert costs.fee(b.qty * 100, maker=False) < costs.fee(a.qty * 100, maker=False)


def test_breakeven_win_rate_falls_with_wider_floors():
    assert costs.breakeven_win_rate(40, 25) < costs.breakeven_win_rate(20, 12)


def test_passive_exit_repegs_to_the_new_touch_keeping_its_deadline():
    b = Book(10_000)
    pos = _filled_long(b)                                        # touch: bid 99.99 / ask 100.01
    first = b.place_passive_exit("BTCUSDT", "time_stop", T0 + 200, ttl=15)
    assert b.repeg_exit("BTCUSDT", T0 + 201) is None             # still at the touch
    b.on_depth("BTCUSDT", [(99.97, 5.0)], [(99.99, 2.0)])        # price drops: ask now 99.99
    old, new = b.repeg_exit("BTCUSDT", T0 + 205)
    assert old.id == first.id and old.status == "cancelled"
    assert new.price == 99.99 and pos.exit_order_id == new.id
    assert new.placed_ts + new.ttl == pytest.approx(first.placed_ts + first.ttl)   # same deadline


def test_impulse_replay_trails_and_stops_pessimistically():
    import numpy as np
    import pandas as pd
    from research import scalper_lab as lab
    from scalper import costs

    n = lab.IMPULSE_HORIZON + 2
    c = np.full(n, 100.0)
    c[1:40] = np.linspace(100, 101, 39)          # runs +1% …
    c[40:] = 100.5                                 # … then gives back half
    path = pd.DataFrame({"h": c, "l": c, "c": c}, index=np.arange(n))
    lat, taker = costs.LATENCY_BPS, costs.TAKER_FEE * 1e4
    got = lab._replay_impulse(path, -1, 1.0, sl=30, tp=None, trail=30)
    # entry ≈ 100 (+latency); best ≈ +100 bps; trail stop at best − 30 ≈ +70; price falls to +50 → out at stop
    assert got == pytest.approx(70 - lat - 2 * taker - lat, abs=1.5)
    # a same-second touch of stop and target counts as the stop
    both = pd.DataFrame({"h": [100.0] + [101.0] * (n - 1), "l": [100.0] + [99.0] * (n - 1),
                         "c": [100.0] * n}, index=np.arange(n))
    assert lab._replay_impulse(both, -1, 1.0, sl=30, tp=60, trail=None) < 0


def test_liquidation_stream_is_recorded_for_every_symbol():
    from types import SimpleNamespace

    from scalper.engine import ScalperEngine

    rows = []
    fake = SimpleNamespace(store=SimpleNamespace(add_liquidation=rows.append), markets={})
    msg = {"e": "forceOrder", "o": {"s": "QNTUSDT", "S": "SELL", "p": "280.0", "ap": "281.0",
                                    "q": "2", "z": "2", "T": 1790549397200}}
    ScalperEngine.on_message(fake, "arr", "!FORCEORDER", msg)
    ScalperEngine.on_message(fake, "arr", "!FORCEORDER", {"o": {"s": "X"}})     # malformed: ignored
    assert rows == [(1790549397.2, "QNTUSDT", "SELL", 280.0, 281.0, 2.0, 562.0)]


