"""The daily risk budget: shares by record, the per-leg risk, the day's stop, and the sizers under it."""
import pytest

from autotrader import sizing
from experiments import copy_trading
from trading import risk_budget as rb


def _closed(n: int, r: float, risk: float = 100.0, t0: float = 1_794_700_000.0):
    return [{"id": f"t{i}", "pnl_usd": r * risk, "risk_usd": risk, "opened_ts": t0 - i * 7200} for i in range(n)]


def test_record_r_is_shrunk_and_legs_per_day_has_a_floor():
    r, n = rb.record_r(_closed(20, 0.05))
    assert n == 20 and r == pytest.approx(0.05 * 20 / 40)
    assert rb.record_r([]) == (0.0, 0)
    now = 1_794_700_000.0
    assert rb.legs_per_day("auto:reversal", _closed(2, 0.0, t0=now), now) == 4.0            # the floor
    assert rb.legs_per_day("copy_trading", _closed(28, 0.0, t0=now), now) == pytest.approx(28 / min(14.0, max(1.0, 27 * 7200 / 86400)))


def test_the_budget_splits_by_record_with_a_floor_and_stops_the_day(monkeypatch):
    now = 1_794_700_000.0
    rows = {"auto:reversal": _closed(20, 0.016, 150.0, now), "copy_trading": _closed(20, 0.18, 165.0, now), "copy_stocks": _closed(9, -0.1, 165.0, now)}
    monkeypatch.setattr(rb, "closed_trades", lambda b: rows[b])
    monkeypatch.setattr(rb, "base_equity", lambda: 50_000.0)
    monkeypatch.setattr(rb, "day_pnl", lambda now_: {"auto": -1200.0, "copy_trading": 300.0, "copy_stocks": 0.0})
    monkeypatch.setattr(rb, "ENABLED", True)
    monkeypatch.setattr(rb, "DAILY_RISK_PCT", 10.0)
    rep = rb.compute(now)
    assert rep["budget_usd"] == 5000.0 and rep["used_usd"] == 900.0 and rep["left_usd"] == 4100.0 and not rep["blocked"]
    b = rep["books"]
    assert b["auto:reversal"]["weight"] == rb.FLOOR_R and b["copy_stocks"]["weight"] == rb.FLOOR_R            # floored
    rows["copy_stocks"] = _closed(9, 0.5, 165.0, now)                                                         # a hot nine-trade record still sits at the floor
    assert rb.compute(now)["books"]["copy_stocks"]["weight"] == rb.FLOOR_R
    rows["copy_stocks"] = _closed(10, 0.5, 165.0, now)
    assert rb.compute(now)["books"]["copy_stocks"]["weight"] > rb.FLOOR_R
    rows["copy_stocks"] = _closed(9, -0.1, 165.0, now)
    assert b["copy_trading"]["share_usd"] > b["auto:reversal"]["share_usd"] > 0
    assert sum(x["share_usd"] for x in b.values()) == pytest.approx(5000.0, abs=0.05)
    assert b["auto:reversal"]["leg_risk_full_usd"] == pytest.approx(b["auto:reversal"]["share_usd"] / b["auto:reversal"]["legs_per_day"], abs=0.01)
    assert b["auto:reversal"]["leg_risk_usd"] == pytest.approx(b["auto:reversal"]["leg_risk_full_usd"] * 0.5, abs=0.01)      # 20 of 40 trades: half maturity
    assert rep["needed_r"] == pytest.approx(0.1) and rep["expected_usd_day"] == pytest.approx(sum(x["expected_usd_day"] for x in b.values()), abs=0.05)
    monkeypatch.setattr(rb, "day_pnl", lambda now_: {"auto": -4000.0, "copy_trading": -1100.0})
    rep = rb.compute(now)
    assert rep["blocked"] and rep["left_usd"] == 0.0
    rb._cache.update(at=0.0, report=None)
    monkeypatch.setattr(rb, "report", lambda now=None, max_age_s=0: rep)
    leg = rb.leg_risk("auto:reversal", now)
    assert leg["on"] and leg["blocked"] and "BLOCKED" in leg["why"] and leg["risk_usd"] == b["auto:reversal"]["leg_risk_usd"]
    assert rb.leg_risk("auction", now) == {"on": False}


def test_plan_leg_sizes_to_the_budget_risk_and_the_share_heat_cap():
    plan = sizing.plan_leg(50_000.0, 6.0, None, 0, 2, [], "short", risk_usd=555.0, heat_cap_usd=2000.0 / sizing.STOP_ATR)
    swing = 555.0 / sizing.STOP_ATR
    assert plan["base_notional"] == pytest.approx(swing / 0.06, abs=0.5) and plan["budget_risk_usd"] == 555.0
    assert plan["heat_cap_usd"] == pytest.approx(2000.0 / sizing.STOP_ATR, abs=0.01) and "risk budget" in plan["why"]
    old = sizing.plan_leg(50_000.0, 6.0, None, 0, 2, [], "short")
    assert old["base_notional"] == pytest.approx(50_000.0 * sizing.RISK_PCT / 100 / 0.06, abs=0.5) and old["budget_risk_usd"] is None


def test_copy_leg_notional_under_the_budget(monkeypatch):
    monkeypatch.setattr(copy_trading, "MAX_LEG_PCT", 100.0)
    assert copy_trading.leg_notional(33_000.0, 2.0, "crypto", risk_usd=1_700.0) == pytest.approx(33_000.0)        # $85k asked, capped at the book
    assert copy_trading.leg_notional(33_000.0, 4.0, "crypto", risk_usd=600.0) == pytest.approx(15_000.0)         # $600 at a 4% stop
    assert copy_trading.leg_notional(33_000.0, 2.0, "crypto") == pytest.approx(min(33_000.0 * copy_trading.RISK_PCT / 2.0, 33_000.0))


def test_a_thin_record_risks_a_thin_leg(monkeypatch):
    """2026-11-17: a leg's risk is scaled by min(1, closed trades / MATURITY_N) — the history review found the budget putting
    its largest legs on the thinnest records. The unscaled figure rides along as the counterfactual."""
    now = 1_794_700_000.0
    rows = {"auto:reversal": _closed(80, 0.05, 150.0, now), "copy_trading": _closed(20, 0.05, 165.0, now), "copy_stocks": _closed(12, 0.12, 165.0, now)}
    monkeypatch.setattr(rb, "closed_trades", lambda b: rows[b])
    monkeypatch.setattr(rb, "base_equity", lambda: 50_000.0)
    monkeypatch.setattr(rb, "day_pnl", lambda now_: {})
    monkeypatch.setattr(rb, "ENABLED", True)
    monkeypatch.setattr(rb, "MATURITY_N", 40)
    b = rb.compute(now)["books"]
    assert b["auto:reversal"]["maturity"] == 1.0 and b["auto:reversal"]["leg_risk_usd"] == b["auto:reversal"]["leg_risk_full_usd"]
    assert b["copy_trading"]["maturity"] == 0.5 and b["copy_trading"]["leg_risk_usd"] == pytest.approx(b["copy_trading"]["leg_risk_full_usd"] * 0.5, abs=0.01)
    assert b["copy_stocks"]["maturity"] == 0.3 and b["copy_stocks"]["record_total_n"] == 12
    assert b["copy_stocks"]["expected_usd_day"] == pytest.approx(b["copy_stocks"]["share_usd"] * b["copy_stocks"]["record_r"] * 0.3, abs=0.01)
    monkeypatch.setattr(rb, "_cache", {"at": 0.0, "report": None})
    leg = rb.leg_risk("copy_stocks", now)
    assert leg["maturity"] == 0.3 and leg["risk_usd"] == b["copy_stocks"]["leg_risk_usd"] and leg["risk_full_usd"] == b["copy_stocks"]["leg_risk_full_usd"]
    assert "× 0.30 (12 of 40 trades)" in leg["why"]
    assert "×" not in rb.leg_risk("auto:reversal", now)["why"]
