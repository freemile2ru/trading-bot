"""The reversal lane's sizing (vol parity, shrunken edge, tilt, heat), the hold check that replaced its clock, and the
profit lock's per-trade arm and 65% keep."""
import asyncio
import time

import pytest

from autotrader import engine, hold_check, lanes, sizing
from trading import exits
from trading.paper_trader import PaperWallet


# ── sizing ────────────────────────────────────────────────────────────────────

def test_volatility_parity_gives_every_coin_the_same_daily_swing():
    zec = sizing.plan_leg(50_000, 9.0, None, 0, 1, [], "short")
    btc = sizing.plan_leg(50_000, 3.0, None, 0, 1, [], "short")
    assert zec["notional"] == pytest.approx(50_000 * 0.002 / 0.09, rel=1e-4) and btc["notional"] == pytest.approx(50_000 * 0.002 / 0.03, rel=1e-4)
    assert zec["swing_usd"] == pytest.approx(btc["swing_usd"]) == pytest.approx(100.0)
    assert zec["stop_pct"] == pytest.approx(13.5) and btc["stop_pct"] == pytest.approx(4.5)       # 1.5 ATR, within 3–15%
    assert sizing.plan_leg(50_000, 1.0, None, 0, 1, [], "short")["stop_pct"] == 3.0               # the floor
    assert sizing.plan_leg(50_000, 12.0, None, 0, 1, [], "short")["stop_pct"] == 15.0             # the ceiling
    quiet = sizing.plan_leg(50_000, 0.5, None, 0, 1, [], "short")                                 # a very quiet coin would want $20k
    assert quiet["notional"] == 4_000.0 and quiet["leg_capped"]                                   # 8% of the ledger


def test_the_edge_multiplier_shrinks_a_small_record_toward_equal_size():
    assert sizing.edge_multiplier(None) == (1.0, "equal size — no record yet")
    m, _ = sizing.edge_multiplier({"n": 20, "avg_r": 0.25})
    assert m == pytest.approx(1.25)                                                                # 0.25 × 20/40 = 0.125 → 1 + 2 × 0.125
    assert sizing.edge_multiplier({"n": 5, "avg_r": -0.5})[0] == pytest.approx(0.8)                # −0.5 × 5/25 = −0.1
    assert sizing.edge_multiplier({"n": 200, "avg_r": -0.6})[0] == 0.5                             # clipped
    assert sizing.edge_multiplier({"n": 200, "avg_r": 0.9})[0] == 1.5
    plan = sizing.plan_leg(50_000, 10.0, {"n": 20, "avg_r": 0.25}, 0, 2, [], "short")
    assert plan["edge_mult"] == pytest.approx(1.25) and plan["tilt"] == 1.25
    assert plan["notional"] == pytest.approx(1_000 * 1.25 * 1.25)


def test_the_tilt_follows_the_slots_rank():
    assert sizing.tilt_for(0, 2)[0] == 1.25 and sizing.tilt_for(1, 2)[0] == 0.75
    assert sizing.tilt_for(0, 1)[0] == 1.0 and sizing.tilt_for(1, 3)[0] == 1.0


def test_the_heat_cap_scales_a_leg_into_the_room_left_then_refuses():
    legs = [{"direction": "short", "notional_usd": 3_000.0, "sizing": {"atr_pct": 10.0}},        # $300 of swing
            {"direction": "short", "notional_usd": 2_000.0, "atr_pct": 5.0}]                     # $100
    plan = sizing.plan_leg(50_000, 5.0, None, 0, 1, legs, "short")                               # cap $500, $400 used, wants $100
    assert plan["heat_scaled"] is None and plan["swing_usd"] == pytest.approx(100.0)
    legs.append({"direction": "short", "notional_usd": 1_000.0, "sizing": {"atr_pct": 5.0}})     # +$50 → $450 used, $50 of room
    plan = sizing.plan_leg(50_000, 5.0, None, 0, 1, legs, "short")
    assert plan["heat_scaled"] == pytest.approx(0.5) and plan["notional"] == pytest.approx(1_000.0) and plan["swing_usd"] == pytest.approx(50.0)
    legs.append({"direction": "short", "notional_usd": 1_000.0, "sizing": {"atr_pct": 5.0}})
    assert "heat cap" in sizing.plan_leg(50_000, 5.0, None, 0, 1, legs, "short")["refused"]
    many = [{"direction": "long", "notional_usd": 10.0, "sizing": {"atr_pct": 1.0}} for _ in range(6)]
    assert "6 longs already on" in sizing.plan_leg(50_000, 5.0, None, 0, 1, many, "long")["refused"]
    assert sizing.plan_leg(50_000, 5.0, None, 0, 1, many, "short")["notional"] > 0                # the other side is free


def test_a_missing_atr_falls_back_and_is_marked():
    plan = sizing.plan_leg(50_000, None, None, 0, 1, [], "short")
    assert plan["atr_pct"] == sizing.ATR_FALLBACK_PCT and plan["atr_known"] is False


def test_cell_for_prefers_the_side_in_the_regime():
    card = {"reversal": {"all": {"n": 30, "avg_r": -0.1}, "by_side": {"short": {"n": 19, "avg_r": 0.03}},
                         "by_regime_side": {"down/calm|short": {"n": 8, "avg_r": 0.2}}}}
    assert sizing.cell_for(card, "reversal", "down/calm", "short")["n"] == 8
    assert sizing.cell_for(card, "reversal", "up/wild", "short")["n"] == 19
    assert sizing.cell_for(card, "reversal", "up/wild", "long")["n"] == 30
    assert sizing.cell_for({}, "reversal", "x", "short") is None


# ── the hold check ────────────────────────────────────────────────────────────

def _leg(hours: float, pnl: float, direction="short", now=1_000_000.0):
    return {"direction": direction, "opened_ts": now - hours * 3600, "pnl_usd": pnl, "symbol": "ZECUSDT"}


def test_the_hold_check_rules_fire_in_order():
    now = 1_000_000.0
    assert hold_check.verdict(_leg(1, -50), now, 0.4, {"price": 90, "vwap24": 100}) == (True, "grace: 1.0h of 4h", "grace")
    hold, why, rule = hold_check.verdict(_leg(5, 20), now, 0.12, None)
    assert not hold and rule == "flipped" and "+0.120" in why                                       # a short's coin now scores on the long side
    assert hold_check.verdict(_leg(5, 20), now, -0.2, None)[0]                                      # still ranks our way: hold
    hold, why, rule = hold_check.verdict(_leg(5, 35), now, -0.2, {"price": 99.0, "vwap24": 100.0})
    assert not hold and rule == "reverted"                                                          # a short in profit, back through the VWAP
    assert hold_check.verdict(_leg(5, -35), now, -0.2, {"price": 99.0, "vwap24": 100.0})[0]        # underwater: the VWAP rule needs a profit
    assert hold_check.verdict(_leg(5, 35), now, None, {"price": 101.0, "vwap24": 100.0})[0]         # in profit but not yet at the VWAP: hold
    hold, why, rule = hold_check.verdict(_leg(30, 40), now, None, {"price": 101.0, "vwap24": 100.0, "move_4h_pct": -0.8})
    assert hold and rule == "running"                                                               # past 24h, in profit, still moving our way
    hold, why, rule = hold_check.verdict(_leg(30, 40), now, None, {"price": 101.0, "vwap24": 100.0, "move_4h_pct": 0.5})
    assert not hold and rule == "horizon" and "no follow-through" in why
    hold, why, rule = hold_check.verdict(_leg(30, -40), now, None, {"price": 101.0, "vwap24": 100.0, "move_4h_pct": -2.0})
    assert not hold and rule == "horizon" and "underwater" in why
    assert hold_check.verdict(_leg(80, 400), now, None, {"price": 101.0, "vwap24": 100.0, "move_4h_pct": -2.0})[2] == "cap"
    hold, why, rule = hold_check.verdict(_leg(5, 35, "long"), now, -0.3, {"price": 101.0, "vwap24": 100.0})
    assert not hold and rule == "flipped"                                                           # the mirror image for a long


# ── the lock's per-trade arm and keep ─────────────────────────────────────────

def _trade(notional=1_000.0, px=100.0, **extra):
    q = notional / px
    t = {"direction": "short", "entry_price": px, "stop_loss": px * 1.10, "take_profit_1": px * 0.5, "take_profit_2": px * 0.4, "quantity": q,
         "notional_usd": notional, "risk_usd": notional * 0.10, "fees_usd": 0.0, "funding_usd": 0.0, "pnl_usd": 0.0, "opened_ts": 0.0, **extra}
    return exits.stamp(t, "manual_lock", now=0.0)


def test_the_reversal_legs_lock_arms_at_one_percent_and_keeps_sixty_five():
    p = exits.POLICIES["manual_lock"]
    assert p.lock_keep == 0.65
    plain, leg = _trade(), _trade(lock_arm_pct=0.01)
    assert exits.lock_arm_usd(plain, p) == pytest.approx(30.0)                                      # 3% of $1,000
    assert exits.lock_arm_usd(leg, p) == pytest.approx(10.0)                                        # 1% of $1,000
    # a short from 100 prints 98 (+$20 on $1,000, 2%): the leg's lock arms, the plain one's does not
    for t in (plain, leg):
        exits.apply_bar(t, 99.0, 99.0, 98.0, 98.5, 3600.0)
    assert not plain.get("trailing") and leg.get("trailing")
    assert leg["locked_usd"] == pytest.approx(0.65 * 20.0) and leg["stop_loss"] == pytest.approx(100 - 0.65 * 2.0)
    custom = _trade(lock_arm_pct=0.01, lock_keep=0.8)
    exits.apply_bar(custom, 99.0, 99.0, 97.5, 97.8, 3600.0)                                        # peak +$25: keeps $20, the stop at 98
    assert custom["locked_usd"] == pytest.approx(20.0) and custom["stop_loss"] == pytest.approx(98.0)


# ── the lane, end to end ──────────────────────────────────────────────────────

def _scan(sym, conv, ret24, oi, top_long):
    return {"symbol": sym, "conviction": conv, "composite_score": conv * 50, "ta_score": conv * 40,
            "fa": {"score": conv * 10, "features": {"ret_24h": ret24, "oi_change_24h_pct": oi, "top_trader_long_pct": top_long}, "components": {"liquidity": {"score": 0.5}}}}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    active = PaperWallet(str(tmp_path / "active.db"), label="auto", initial_balance=50_000)
    monkeypatch.setattr(engine, "_active", lambda: active)
    monkeypatch.setattr(engine, "_wallets", lambda: [active])
    monkeypatch.setattr(engine, "_holdings_elsewhere", lambda include_books=True: [])
    monkeypatch.setattr(engine, "_lane_history", lambda: [])
    monkeypatch.setattr(engine, "REVERSAL_IN_AUTO", True)
    from storage import state as kv
    kv.delete_sync(kv.key("auto", "reversal_slot"))
    atrs = {"C9USDT": 9.0, "C8USDT": 3.0}

    async def not_paused():
        return False

    async def no_regime():
        return None

    async def price(sym):
        return 100.0

    async def atr(sym, now=None):
        return atrs.get(sym)

    async def no_prop(picks, scores, now=None):
        return []

    def no_cooldown(symbol, direction):                      # sync: the engine runs it in a thread
        return None
    monkeypatch.setattr(engine, "is_paused", not_paused)
    monkeypatch.setattr(engine.regime, "current", no_regime)
    monkeypatch.setattr(engine.monitor, "live_price", price)
    monkeypatch.setattr(sizing, "daily_atr_pct", atr)
    from autotrader import reversal
    monkeypatch.setattr(reversal, "chosen", lambda: {"conviction": -1.0, "ret_24h": -1.0, "oi_change_24h_pct": -1.0, "top_trader_long_pct": 1.0})
    monkeypatch.setattr(engine.coin_lock, "recent_stop_out", no_cooldown)
    from experiments import prop
    monkeypatch.setattr(prop, "mirror_reversal", no_prop)
    from trading import tape

    async def no_tape(symbol, now=None):
        return None
    monkeypatch.setattr(tape, "coin_tape", no_tape)
    return active


def test_the_lane_sizes_each_leg_by_its_coin_and_stamps_every_input(lane):
    scans = [_scan(f"C{i}USDT", conv=i / 10, ret24=i, oi=i, top_long=50 - i) for i in range(10)]
    asyncio.run(engine._reversal_lane(scans))
    legs = sorted(lane.get_portfolio()["open_trades"], key=lambda t: t["symbol"])
    assert [t["symbol"] for t in legs] == ["C8USDT", "C9USDT"]                                      # the two most-liked coins, sold
    c8, c9 = legs
    assert c9["sizing"]["rank"] == 0 and c9["sizing"]["tilt"] == 1.25 and c8["sizing"]["tilt"] == 0.75
    assert c9["notional_usd"] == pytest.approx(50_000 * 0.002 / 0.09 * 1.25, rel=0.02)              # 9% ATR coin, strongest score
    assert c8["notional_usd"] == pytest.approx(50_000 * 0.002 / 0.03 * 0.75, rel=0.02)              # 3% ATR coin, weakest
    assert c9["stop_loss"] == pytest.approx(100 * 1.135, rel=0.01) and c8["stop_loss"] == pytest.approx(100 * 1.045, rel=0.01)
    assert c9["lock_arm_pct"] == 0.01 and c9["atr_pct"] == 9.0 and c9["lane_mult"] == 1.0
    assert c9["expires_at"] - c9["opened_ts"] == pytest.approx(hold_check.HARD_CAP_H * 3600, abs=5)
    assert "vol parity" in c9["lane_why"] and "strongest" in c9["lane_why"]


def test_the_hold_check_closes_a_leg_the_clock_used_to_keep(lane, monkeypatch):
    scans = [_scan(f"C{i}USDT", conv=i / 10, ret24=i, oi=i, top_long=50 - i) for i in range(10)]
    asyncio.run(engine._reversal_lane(scans))
    legs = lane.get_portfolio()["open_trades"]
    assert len(legs) == 2
    # 30 hours on, underwater, the tape flat: the horizon rule closes both; a fresh leg would still be in its grace period
    for t in legs:
        lane.annotate(t["id"], opened_ts=time.time() - 30 * 3600)
    lane.update_prices({"C8USDT": 101.0, "C9USDT": 101.0})

    async def tape(symbol, now=None):
        return {"price": 101.0, "vwap24": 100.0, "move_4h_pct": 0.1}
    monkeypatch.setattr(hold_check, "tape", tape)
    asyncio.run(engine._hold_checks({}))
    p = lane.get_portfolio()
    assert p["open_trades"] == [] and len(p["closed_trades"]) == 2
    assert all(t["close_reason"] == "hold_check" and t["hold_rule"] == "horizon" and "underwater" in t["exit_note"] for t in p["closed_trades"])
    audit = lanes.reversal_exit_audit(p["closed_trades"])
    assert audit["n"] == 2 and audit["by_exit"]["horizon"]["n"] == 2 and audit["sized_n"] == 2
