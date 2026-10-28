import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from storage import signals as signal_log
from trading import debrief


def _row(net=-10.0, gross=-8.0, reason="stop_loss", **kw):
    return {"system": "auto", "id": "t1", "symbol": "SOLUSDT", "direction": "long", "net_pnl": net, "gross_pnl": gross,
            "fees": 2.0, "r": -1.0, "exit_reason": reason, **kw}


@pytest.mark.parametrize("row, path, primary, extra", [
    (_row(net=30, gross=32, reason="trailing_stop"), {"best_r": 2.0, "post_best_r": 0.2}, "clean win", []),
    (_row(net=30, gross=32, reason="take_profit_2"), {"best_r": 2.0, "post_best_r": 1.8}, "win, exited early", []),
    (_row(net=-1, gross=3, reason="manual"), {"best_r": 0.4, "post_best_r": 0.1}, "fees ate it", []),
    (_row(), {"best_r": 1.4, "post_best_r": 0.0}, "gave back the gain", []),
    (_row(), {"best_r": 0.5, "post_best_r": 1.5, "post_back_through_entry": True}, "stopped, then reversed", ["exited early"]),
    (_row(), {"best_r": 0.1, "post_best_r": 0.0}, "never worked", []),
    (_row(), {"best_r": 0.6, "post_best_r": 0.2}, "clean loss", []),
])
def test_tags(row, path, primary, extra):
    d = debrief.tag(row, path)
    assert d["primary"] == primary
    for e in extra:
        assert e in d["tags"]


def _trades(now):
    base = {"symbol": "SOLUSDT", "direction": "long", "status": "closed", "entry_price": 100.0, "exit_price": 98.0,
            "quantity": 10.0, "risk_usd": 20.0, "net_pnl": -21.0, "gross_pnl": -20.0, "fees": 1.0, "r": -1.05,
            "exit_reason": "stop_loss", "mfe_r": None}
    return [
        {**base, "system": "auto", "id": "old", "opened_ts": now - 3 * 86400, "closed_ts": now - 2 * 86400, "hold_min": 1440},
        {**base, "system": "auto", "id": "recent", "opened_ts": now - 7200, "closed_ts": now - 3600, "hold_min": 60},    # window not over
        {**base, "system": "main", "id": "scalp", "opened_ts": now - 7800, "closed_ts": now - 7200, "hold_min": 10},
        {**base, "system": "impulse", "id": "retired2", "opened_ts": now - 7800, "closed_ts": now - 7200, "hold_min": 10},
        {**base, "system": "scalper", "id": "retired", "opened_ts": now - 3 * 86400, "closed_ts": now - 2 * 86400, "hold_min": 5},
    ]


@pytest.fixture
def market(monkeypatch):
    now = time.time()
    from trading import overview
    monkeypatch.setattr(overview, "all_rows", lambda: (_trades(now), {}))

    async def klines(symbol, interval, start_ms, end_ms):
        step = 60_000 if interval == "1m" else 3_600_000
        # dips to 98 (the stop), then rallies to 104 after the exit: a stop-then-reverse
        out, t, i = [], start_ms - start_ms % step, 0
        while t <= end_ms:
            px = 101 if i < 3 else 97.5 if i < 5 else 104
            out.append([t, px, px + 0.2, px - 0.2, px, 1, 1])
            t, i = t + step, i + 1
        return out
    import data.derivatives_client as dc
    monkeypatch.setattr(dc, "fetch_klines_range", klines)
    return now


def test_run_tags_waits_for_the_window_and_skips_retired(market):
    n = asyncio.run(debrief.run_tags())
    keys = signal_log.debriefed_keys()
    assert ("auto", "old") in keys and ("main", "scalp") in keys
    assert ("auto", "recent") not in keys and ("scalper", "retired") not in keys and ("impulse", "retired2") not in keys
    assert n == 2
    d = signal_log.debrief("auto", "old")
    assert d["metrics"]["post_back_through_entry"] and "stopped, then reversed" in d["tags"]


def _fake_claude(monkeypatch):
    calls = []

    async def create(**kw):
        calls.append(kw)
        ids = [line.split("id ")[1].split(" ")[0] for line in kw["messages"][0]["content"].split("\n") if line.startswith("### id ")]
        body = json.dumps([{"id": i, "thesis": "wrong", "what_happened": "faded", "cause": "entry timing",
                            "lesson": "wait for the retest"} for i in ids])
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="…"), SimpleNamespace(type="text", text=body)])
    import oracle.claude_oracle as co
    monkeypatch.setattr(co, "get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    return calls


def test_claude_debriefs_swing_trades_in_batches_within_budget(monkeypatch):
    now = time.time()
    for i in range(7):
        signal_log.save_debrief("auto", f"a{i}", "SOLUSDT", now - i, "clean loss", ["clean loss"], {"r": -1})
    signal_log.save_debrief("impulse", "x1", "SOLUSDT", now, "never worked", ["never worked"], {"r": -1})
    rec = {"id": "?", "direction": "long", "symbol": "SOLUSDT", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_2": 104.0,
           "current_price": 98.0, "pnl_usd": -20.0, "close_reason": "stop_loss", "claude_thesis": "breakout"}
    monkeypatch.setattr(debrief, "_record", lambda system, tid: {**rec, "id": tid})
    calls = _fake_claude(monkeypatch)
    monkeypatch.setattr(debrief, "DAILY_CALLS", 1)
    assert asyncio.run(debrief.run_claude()) == 5                    # one call, one batch of 5
    assert len(calls) == 1
    assert signal_log.debrief("auto", "a0")["claude"]["lesson"] == "wait for the retest"
    assert signal_log.debrief("impulse", "x1")["claude"] is None      # scalps get tags only
    monkeypatch.setattr(debrief, "DAILY_CALLS", 5)
    asyncio.run(debrief.run_claude())
    assert all(signal_log.debrief("auto", f"a{i}")["claude"] for i in range(7))


def test_claude_swing_reads_its_own_lessons(monkeypatch):
    from experiments import claude_swing
    signal_log.save_debrief("claude_swing", "c1", "LINKUSDT", time.time(), "stopped, then reversed", ["stopped, then reversed"], {"r": -1})
    signal_log.save_claude_debrief("claude_swing", "c1", {"what_happened": "stop hit on a wick", "lesson": "wider stop below the swing low"})
    brief = claude_swing._brief([], {}, [], [], 10_000)
    assert "Lessons from your recent closed trades" in brief and "wider stop below the swing low" in brief


def test_response_text_skips_thinking_blocks():
    """Opus 5.5 answers with a thinking block first — reading content[0].text broke every Claude call."""
    from oracle.claude_oracle import parse_json, response_text
    resp = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="let me see", text=None),
                                    SimpleNamespace(type="text", text='```json\n{"a": 1}\n```')])
    assert parse_json(response_text(resp)) == {"a": 1}


def test_oracle_parses_a_reply_that_starts_with_thinking(monkeypatch):
    import oracle.claude_oracle as co
    reply = {"trade_recommendation": {"action": "BUY", "confidence": 70}, "reasoning": {}}

    async def create(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="…"), SimpleNamespace(type="text", text=json.dumps(reply))],
                               model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    monkeypatch.setattr(co, "get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(co, "_build_user_message", lambda pkg, gm: "x")
    out = asyncio.run(co.synthesize_trade({}, {}))
    assert "error" not in out and out["trade_recommendation"]["action"] == "BUY"
