"""Scanner escalation gate: no duplicate Claude calls for a coin that already has a live signal."""

import asyncio
import time

from scanner import escalation


def _scan(conviction):
    return {
        "symbol": "WLDUSDT", "base": "WLD", "price": 0.49, "conviction": conviction, "confidence": 80,
        "conviction_detail": {"aligned_setups": ["trend_continuation"]}, "tf_alignment": {"multiplier": 1.2},
        "setups": [], "fa": {"label": "x"}, "regime": "trending", "best_timeframe": "1h",
        "hold_duration": "4h", "signal_package": {},
    }


def _run(monkeypatch, live_action, conviction):
    calls = []

    async def fired():
        return [{"symbol": "WLDUSDT", "action": live_action, "fired_at": time.time() - 1800}]

    async def claude(pkg, gm):
        calls.append(pkg)
        return {"trade_recommendation": {"action": "HOLD"}}

    async def zero():
        return 0

    async def yes(*a, **k):
        return True

    async def one(*a, **k):
        return 1

    monkeypatch.setattr(escalation, "recent_fired", fired)
    monkeypatch.setattr(escalation, "synthesize_trade", claude)
    monkeypatch.setattr(escalation, "budget_used", zero)
    monkeypatch.setattr(escalation.state, "acquire_once", yes)
    monkeypatch.setattr(escalation.state, "incr_with_expire", one)
    asyncio.run(escalation.try_escalate(_scan(conviction), {}))
    return calls


def test_same_direction_live_signal_skips_claude(monkeypatch):
    assert _run(monkeypatch, "BUY", 0.6) == []


def test_opposite_direction_still_reaches_claude(monkeypatch):
    assert len(_run(monkeypatch, "BUY", -0.6)) == 1
