"""FA in the autotrader: context for Claude and entry records — no longer a decision input (retired)."""

import time

import pytest

from analysis.scoring import build_signal_package
from autotrader import strategy
from oracle.claude_oracle import _build_user_message
from trading import history
from trading import position_manager as pm
from trading.paper_trader import PaperWallet


def _scan(conviction, fa_score):
    return {"conviction": conviction, "tf_alignment": {"multiplier": 1.2},
            "conviction_detail": {"aligned_setups": ["x"], "opposing_setups": []},
            "fa": {"score": fa_score, "label": "test"}}


@pytest.mark.parametrize("fa", [-0.9, -0.5, 0.0, 0.5, 0.9])
def test_fa_no_longer_vetoes_entries(fa):
    """The FA veto was retired (FA inputs had no predictive power) — FA is context only now."""
    for conv in (0.5, -0.5):
        direction, reasons = strategy.bias(_scan(conv, fa))
        assert direction == ("long" if conv > 0 else "short")
        assert not any("vetoed" in r for r in reasons)


def _pkg(with_fa):
    pkg = build_signal_package(
        symbol="SOLUSDT", ta_result={"timeframes": {}, "consensus_score": 0.0}, news={}, macro={},
        fear_greed={"value": 50, "classification": "Neutral"}, social={}, coingecko={},
    )
    if with_fa:
        pkg["fundamentals"] = {"score": -0.42, "label": "bearish", "rs_vs_btc_24h": -3.1,
                               "components": {"relative_strength": {"score": -0.8, "note": "lagging BTC"},
                                              "supply": {"score": -0.15, "note": "uncapped supply"}}}
    return pkg


def test_claude_sees_fundamentals():
    msg = _build_user_message(_pkg(True), None)
    assert "### FUNDAMENTALS" in msg and "relative strength: -0.80" in msg and "lagging BTC" in msg


def test_claude_message_without_fa_still_builds():
    assert "### FUNDAMENTALS" not in _build_user_message(_pkg(False), None)


@pytest.mark.parametrize("direction,score,bucket", [
    ("long", 0.3, "with"), ("long", -0.3, "against"), ("long", 0.1, "neutral"),
    ("short", -0.3, "with"), ("short", 0.3, "against"), ("long", None, "unknown"),
])
def test_fa_bucket_is_relative_to_direction(direction, score, bucket):
    t = {"direction": direction, "fa_at_entry": None if score is None else {"score": score}}
    assert history.fa_bucket(t) == bucket


def test_fa_no_longer_blocks_averaging_down(tmp_path):
    w = PaperWallet(str(tmp_path / "w.json"))
    w.reset_portfolio(10_000)
    t = w.open_trade("XUSDT", "long", 100.0, 98.0, 103.0, 105.0, size_pct=20)
    fast = {"signal": "buy", "rsi": 55, "macd_histogram": 1.0, "bb_position": "inside"}
    scan = {"symbol": "XUSDT", "price": 99.0, "conviction": 0.5, "triggers": {"atr_pct": 1.0},
            "signal_package": {"ta_summary": {"timeframe_signals": {"15m": fast}}}}
    ok = pm.decide(t, {**scan, "fa": {"score": 0.1}}, time.time(), 1000, 10_000)
    blocked = pm.decide(t, {**scan, "fa": {"score": -0.5}}, time.time(), 1000, 10_000)
    assert ok["action"] == "add" and ok["kind"] == "average_down"
    assert blocked["action"] == "add"                      # FA is no longer a decision input


def test_late_entries_are_blocked_in_both_directions():
    fast = {"price": 105.0, "ema_21": 100.0, "atr": 2.0, "rsi": 60}     # +2.5 ATR
    assert strategy.extension_atr(fast, "long") == 2.5
    assert strategy.extension_atr(fast, "short") == -2.5
    t = strategy.timing("long", fast, None, {"triggers": {"atr_pct": 1.0}, "conviction": 0.5})
    assert t["blocked"] and "late" in t["reasons"][0]
    t = strategy.timing("short", {**fast, "price": 95.0}, None, {"triggers": {"atr_pct": 1.0}, "conviction": -0.5})
    assert t["blocked"] and "late" in t["reasons"][0]


def test_scanner_does_not_escalate_a_late_setup():
    from scanner import escalation
    fast = {"price": 105.0, "ema_21": 100.0, "atr": 2.0}
    scan = {"conviction": 0.6, "confidence": 80, "conviction_detail": {"aligned_setups": ["x"]},
            "tf_alignment": {"multiplier": 1.2},
            "signal_package": {"ta_summary": {"timeframe_signals": {"15m": fast}}}}
    ok, reason = escalation.qualifies(scan)
    assert not ok and reason.startswith("late")
    ok, _ = escalation.qualifies({**scan, "signal_package": {"ta_summary": {"timeframe_signals": {"15m": {**fast, "price": 101.0}}}}})
    assert ok
