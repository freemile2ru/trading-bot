"""The ported auction-level system: session levels, acceptance → confirmation → entry → exit, grades."""
import numpy as np
import pandas as pd
import pytest

from trading import auction_levels as AL


def _bars(path: list[float], start="2026-06-01 13:30", volume=100.0) -> pd.DataFrame:
    """5-minute bars from a close path (open = previous close, high/low ±0.05%), starting at a UTC time."""
    idx = pd.date_range(start, periods=len(path), freq="5min", tz="UTC")
    closes = np.array(path, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * 1.0005, "low": np.minimum(opens, closes) * 0.9995,
                         "close": closes, "volume": volume}, index=idx)


def test_session_levels_prior_day_overnight_orb_and_ib():
    # Two New York days in June (EDT: 09:30 ET = 13:30 UTC). Day 1 ranges 100–110 in session, overnight 95–105, day 2 opens at 102.
    day1 = [100 + (i % 11) for i in range(90)]                    # 09:30 → 17:00 ET: session highs 110, lows 100
    after = [100] * 12                                            # 17:00 → 18:00 ET: between the session and the overnight
    night = [95 + (i % 11) for i in range(96)]                    # 18:00 → 02:00 ET: overnight 95–105
    bridge = [102] * 90                                           # 02:00 → 09:30 ET: still overnight, flat
    day2 = [102, 103, 104, 103, 102, 101] + [102.5] * 12          # ORB 09:30–10:00 = 101–104, IB to 10:30 adds nothing new
    df = _bars(day1 + after + night + bridge + day2)
    d = AL.session_frame(df)
    last = d.iloc[-1]
    assert last["et"].hour == 10 and last["in_session"]
    assert last["PDH"] == pytest.approx(110 * 1.0005) and last["PDL"] == pytest.approx(100 * 0.9995)
    assert last["ONH"] == pytest.approx(105 * 1.0005) and last["ONL"] == pytest.approx(95 * 0.9995)
    assert last["ORBH"] == pytest.approx(104 * 1.0005) and last["ORBL"] == pytest.approx(101 * 0.9995)
    assert np.isnan(d.iloc[-13]["ORBH"])                           # ORB levels only show once the first 30 minutes have ended
    assert last["IBH"] == pytest.approx(104 * 1.0005) and last["IBH1"] == pytest.approx(last["IBH"] + (last["IBH"] - last["IBL"]))


def test_psych_levels_step_with_price():
    assert AL._psych(100.0, True) == pytest.approx(100.4) and AL._psych(100.0, False) == pytest.approx(100.0)
    assert AL._psych(615.2, True) > 615.2 and AL._psych(615.2, False) < 615.2


def test_acceptance_confirms_enters_and_reaches_the_target():
    # Prior day grinds 100 → 101 (PDH ≈ 101.05); the overnight keeps rising slowly so the 15m/30m bias is UP at the open
    # (a stand-down needs the master bias against or absent); on day 2 price accepts above PDH, holds, and runs.
    pre = [99.0 + i / 575 for i in range(576)]                     # two quiet days first: the 30m ATR needs 84 closed candles
    day1 = [100 + i / 90 for i in range(90)]                      # 09:30 → 17:00 ET
    night = [98.5 + 2.5 * i / 197 for i in range(198)]            # 17:00 → 09:30 ET next day: 98.5 → 101.0, steep enough that the 30m candles have real bodies
    day2 = [101.0, 101.05, 101.2, 101.3, 101.5, 101.9, 102.3, 102.8, 103.4, 104.0, 104.5, 104.8, 105.0] + [105.0] * 60
    df = _bars(pre + day1 + night + day2)
    trades = AL.simulate(df, "TESTUSDT", min_grade=0)
    assert trades, "an acceptance above PDH should become a trade"
    t = trades[0]
    assert t.direction == "long" and t.level_name in ("PDH", "ONH")          # both sit at ~101.05; the study credits the higher one
    assert t.entry_ts == t.confirm_ts + pd.Timedelta(minutes=5) and t.confirm_ts == t.accept_ts + pd.Timedelta(minutes=5)
    assert t.stop < t.entry < t.target and t.target_name in ("PSYCH", "WKH", "IBH", "IBH05", "IBH1", "IBH2", "ORBH")
    assert t.exit_reason in ("target", "exit_through_level", "session_end") and 0 <= t.grade <= 8
    assert t.r is not None and t.exit is not None


def test_grade_counts_alignment_and_stand_down_reasons():
    ctx = {"master": 1, "caution": False, "divergence": False, "vol_pressure": 10.0, "bias": 1, "indecision30": False, "atr_low": False}
    assert AL.grade(ctx, 1) == (8, False, [])
    bad = {**ctx, "master": -1, "divergence": True, "bias": 0}
    score, stand_down, reasons = AL.grade(bad, 1)
    assert stand_down and reasons == ["HTF", "DIV", "5M"] and score == 4     # only no-caution, volume, no-indecision, ATR remain
    assert AL.grade({**ctx, "vol_pressure": -50.0}, 1)[0] == 7            # a long into heavy selling loses the volume point


def test_summary_applies_costs_and_splits_halves():
    t = AL.Trade("X", "long", "PDH", 100.0, pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-01", tz="UTC"),
                 entry=100.0, stop=99.0, target=103.0, target_name="PSYCH", grade=7, exit=103.0, exit_reason="target", exit_ts=pd.Timestamp("2026-01-01", tz="UTC"))
    u = AL.Trade("X", "long", "PDH", 100.0, pd.Timestamp("2026-01-02", tz="UTC"), pd.Timestamp("2026-01-02", tz="UTC"), pd.Timestamp("2026-01-02", tz="UTC"),
                 entry=100.0, stop=99.0, target=103.0, target_name="PSYCH", grade=5, exit=99.0, exit_reason="stop", exit_ts=pd.Timestamp("2026-01-02", tz="UTC"))
    s = AL.summarize([t, u], cost_bps=10.0)
    assert s["trades"] == 2 and s["all"]["avg_net_pct"] == pytest.approx((3.0 - 0.2 + (-1.0 - 0.2)) / 2, abs=1e-6)
    assert s["first_half"]["n"] == 1 and s["by_grade"][7]["win_pct"] == 100.0 and s["by_exit"]["stop"]["avg_r"] == pytest.approx(-1.2)
