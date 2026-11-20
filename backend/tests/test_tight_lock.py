"""The tight profit lock (arm 0.5% of notional, keep 80%) shadowed beside every sized reversal leg, and its verdict."""
import pytest

from autotrader import lanes
from trading import exits


def _leg(**kw):
    t = {"id": "leg1", "symbol": "HYPEUSDT", "direction": "short", "entry_price": 10.0, "stop_loss": 11.0, "quantity": 150.0,
         "notional_usd": 1500.0, "risk_usd": 150.0, "timeframe": "4h", "leverage": 8, "opened_at": "2026-11-14T00:00:00+00:00",
         "opened_ts": 1_794_700_000.0, "exit_policy": "manual_lock", "shadows": False, "sizing": {"notional": 1500.0}, "checked_from_ms": None}
    t.update(kw)
    return t


def test_a_sized_reversal_leg_gets_the_tight_lock_shadow_and_other_manual_trades_do_not(monkeypatch):
    monkeypatch.setattr(exits, "LOCK_MODE", "live")
    sh = exits.make_shadows(_leg(atr_pct=8.0), "auto")
    assert [s["exit_policy"] for s in sh] == list(exits.LOCK_SHADOWS) and sh[-1]["id"] == "leg1:manual_lock_tight"
    assert "manual_lock_trail_1" in exits.LOCK_SHADOWS and "manual_lock_trail_1" in lanes.LOCK_SHADOW_POLICIES    # the live trail without its floor (2026-11-20)
    pure = exits.POLICIES["manual_lock_trail_1"]
    assert pure.lock_trail_atr == 1.0 and pure.lock_arm_pct == 0.01 and pure.lock_floor_r is None and pure.max_hold_h == 72
    assert all(s["expires_at"] == pytest.approx(1_794_700_000.0 + 72 * 3600) for s in sh)   # the lane's hard cap, not the 4h window
    assert all(s["atr_pct"] == 8.0 for s in sh)                                              # the trail shadows need the coin's ATR
    assert exits.make_shadows(_leg(sizing=None), "auto") == []                              # an unsized manual trade: nothing
    monkeypatch.setattr(exits, "LOCK_TIGHT_SHADOW", False)
    assert exits.make_shadows(_leg(), "auto") == []


def test_the_tight_lock_arms_at_half_a_percent_of_the_leg_and_keeps_eighty():
    p = exits.POLICIES["manual_lock_tight"]
    sh = {"risk_usd": 150.0, "notional_usd": 1500.0}
    assert exits.lock_arm_usd(sh, p) == pytest.approx(7.5) and exits.lock_keep(sh, p) == 0.8
    live = {"risk_usd": 150.0, "notional_usd": 1500.0, "lock_arm_pct": 0.01, "lock_keep": 0.65}
    assert exits.lock_arm_usd(live, exits.POLICIES["manual_lock"]) == pytest.approx(15.0) and exits.lock_keep(live, exits.POLICIES["manual_lock"]) == 0.65
    assert "manual_lock_tight" not in exits.SHADOW_POLICIES


def test_the_verdict_pairs_each_leg_with_its_shadow_and_waits_for_forty():
    legs = [_leg(id=f"l{i}", opened_ts=float(i), pnl_usd=9.0, risk_usd=150.0) for i in range(45)]
    results = [{"trade_id": f"l{i}", "policy": "manual_lock_tight", "r": 0.06 + 0.03 * (1 + i % 2), "reason": "trailing_stop"} for i in range(45)]
    v = lanes.tight_lock_verdict(legs[:30], results)
    assert v["verdict"] == "measuring" and v["n"] == 30
    v = lanes.tight_lock_verdict(legs, results)
    assert v["n"] == 45 and v["delta_r"] > 0.03 and v["t"] > 2 and v["verdict"] == "adopt"
    assert v["recent"][0]["trade_id"] == "l44" and v["shadow_usd"] > v["actual_usd"]
    flat = [{**r, "r": 0.06} for r in results]                                              # the shadow equals the actual: nothing to adopt
    assert lanes.tight_lock_verdict(legs, flat)["verdict"] == "keep the live lock"
    assert lanes.tight_lock_verdict([], [])["verdict"] == "measuring"


def test_the_atr_trail_ratchets_the_stop_one_atr_from_the_best_price_once_armed():
    t = exits.stamp({"direction": "short", "entry_price": 100.0, "stop_loss": 110.0, "take_profit_1": 50.0, "take_profit_2": 40.0, "quantity": 10.0,
                     "notional_usd": 1000.0, "risk_usd": 100.0, "timeframe": "4h", "fees_usd": 0.0, "lock_arm_pct": 0.01, "lock_trail_atr": 1.0, "atr_pct": 8.0,
                     "lock_floor_r": None},                                                                     # the trail's mechanics alone (the floor since 2026-11-20 has its own test)
                    "manual_lock", 1_794_700_000.0)
    T0 = 1_794_700_000.0
    assert exits.apply_bar(t, 100, 100.2, 99.5, 99.6, T0 + 300) is None and t["stop_loss"] == 110.0      # +0.5%: not armed (arm is 1% = $10)
    assert exits.apply_bar(t, 99.6, 99.7, 98.0, 98.5, T0 + 600) is None                                   # low 98: +2% peak → armed
    assert t["stop_loss"] == pytest.approx(98.0 * 1.08) and t["trailing"]                                 # the stop sits 1 ATR (8%) above the low
    assert exits.apply_bar(t, 98.5, 99.0, 92.0, 93.0, T0 + 900) is None and t["stop_loss"] == pytest.approx(92.0 * 1.08)   # ratchets with the low
    assert exits.apply_bar(t, 93.0, 99.5, 93.0, 99.4, T0 + 1200)[0] == "trailing_stop"                    # back up 1 ATR: out, still +0.6%
    old = exits.stamp({**{k: v for k, v in t.items() if k not in ("lock_trail_atr",)}, "stop_loss": 110.0, "trailing": False, "stop_moved": False, "lock_keep": 0.65}, "manual_lock", T0)
    exits.apply_bar(old, 99.6, 99.7, 98.0, 98.5, T0 + 600)
    assert old["stop_loss"] == pytest.approx(100.0 - 0.65 * 2.0)                                           # the 65% keep: stop at 98.7 on a $20 peak


def _trail_leg(T0: float) -> dict:
    return exits.stamp({"direction": "short", "entry_price": 100.0, "stop_loss": 110.0, "take_profit_1": 50.0, "take_profit_2": 40.0, "quantity": 10.0,
                        "notional_usd": 1000.0, "risk_usd": 100.0, "timeframe": "4h", "fees_usd": 0.0, "lock_arm_pct": 0.01, "lock_trail_atr": 1.0, "atr_pct": 8.0},
                       "manual_lock", T0)


def test_the_floor_under_the_atr_trail_keeps_half_the_peak_once_half_an_r_up():
    # 2026-11-20: a 10% stop ($100 of risk), an 8% daily ATR. The trail alone leaves a +0.6R leg with its stop on the losing side.
    p = exits.POLICIES["manual_lock"]
    assert p.lock_floor_r == 0.5 and p.lock_floor_keep == 0.5
    T0 = 1_794_700_000.0
    t = _trail_leg(T0)
    assert exits.apply_bar(t, 100, 100.2, 96.0, 96.5, T0 + 300) is None                              # low 96: +$40 = 0.4R — armed, the trail alone
    assert t["stop_loss"] == pytest.approx(96.0 * 1.08) and t["locked_usd"] == 0.0 and t["lock_floor_bound"] is False   # 103.68: a loss if hit
    assert exits.apply_bar(t, 96.5, 97.0, 94.0, 94.5, T0 + 600) is None                              # low 94: +$60 = 0.6R — the floor binds
    assert t["stop_loss"] == pytest.approx(97.0) and t["locked_usd"] == pytest.approx(30.0) and t["lock_floor_bound"] is True
    out = exits.apply_bar(t, 94.5, 98.0, 94.5, 97.9, T0 + 900)                                        # back up through 97: out with half the peak
    assert out[0] == "trailing_stop" and out[1] == pytest.approx(97.0)
    big = _trail_leg(T0)
    assert exits.apply_bar(big, 100, 100.2, 80.0, 81.0, T0 + 300) is None                            # a 20% move: the trail (86.4, $136) is tighter than the floor (90, $100)
    assert big["stop_loss"] == pytest.approx(80.0 * 1.08) and big["locked_usd"] == pytest.approx(136.0) and big["lock_floor_bound"] is False


def test_the_floor_is_off_without_the_trail_and_when_disabled():
    T0 = 1_794_700_000.0
    copy = exits.stamp({"direction": "short", "entry_price": 100.0, "stop_loss": 110.0, "take_profit_1": 50.0, "take_profit_2": 40.0, "quantity": 10.0,
                        "notional_usd": 1000.0, "risk_usd": 100.0, "timeframe": "4h", "fees_usd": 0.0, "lock_arm_pct": 0.01, "lock_keep": 0.65}, "manual_lock", T0)
    assert exits.apply_bar(copy, 100, 100.2, 94.0, 94.5, T0 + 300) is None
    assert copy["stop_loss"] == pytest.approx(100.0 - 0.65 * 6.0) and copy["lock_floor_bound"] is False   # the 65% keep already holds more than half
    off = _trail_leg(T0)
    off["lock_floor_r"] = None                                                                         # LOCK_FLOOR_R=0 reads as None: the plain trail
    assert exits.apply_bar(off, 100, 100.2, 94.0, 94.5, T0 + 300) is None
    assert off["stop_loss"] == pytest.approx(94.0 * 1.08) and off["locked_usd"] == 0.0 and off["lock_floor_bound"] is False
