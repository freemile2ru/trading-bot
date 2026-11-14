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
    sh = exits.make_shadows(_leg(), "auto")
    assert [s["exit_policy"] for s in sh] == ["manual_lock_tight"] and sh[0]["id"] == "leg1:manual_lock_tight"
    assert sh[0]["expires_at"] == pytest.approx(1_794_700_000.0 + 72 * 3600)             # the lane's hard cap, not the 4h window
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
