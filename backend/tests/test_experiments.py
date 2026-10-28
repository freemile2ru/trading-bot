import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from experiments import books, claude_swing, signal_books
from trading.paper_trader import PaperWallet


@pytest.fixture
def exp(tmp_path, monkeypatch):
    wallets = {}
    for name in books.LABELS:
        w = PaperWallet(str(tmp_path / "swing.db"), label=name)
        w.reset_portfolio(10_000)
        wallets[name] = w
    monkeypatch.setattr(books, "WALLETS", wallets)
    monkeypatch.setattr(books, "ENABLED", True)

    async def price(symbol):
        return 100.0
    monkeypatch.setattr(signal_books.monitor, "live_price", price)
    monkeypatch.setattr(claude_swing.monitor, "live_price", price)
    return wallets


def _scan(symbol="SOLUSDT", conviction=0.6):
    fast = {"signal": "buy", "rsi": 50, "score": 0.3, "price": 100.0, "ema_21": 99.8, "atr": 0.8}
    return {"symbol": symbol, "base": symbol[:-4], "price": 100.0, "conviction": conviction, "confidence": 70,
            "best_timeframe": "1h", "hold_duration": "1-2 days", "triggers": {"atr_pct": 1.0},
            "signal_package": {"ta_summary": {"timeframe_signals": {"15m": fast, "4h": fast}}}}


# ── Signal books ───────────────────────────────────────────────────────────────

def test_rules_book_takes_each_candidate_once_per_cooldown(exp, monkeypatch):
    monkeypatch.setattr(signal_books.strategy, "PULLBACK_ENTRIES", False)
    t = asyncio.run(signal_books.on_candidate(_scan()))
    assert t and t["direction"] == "long" and t["stop_loss"] < 100 < t["take_profit_2"]
    assert asyncio.run(signal_books.on_candidate(_scan())) is None            # cooldown
    assert len(exp["signals_rules"].get_portfolio()["open_trades"]) == 1


def test_signal_books_rest_a_pullback_limit_with_a_market_twin(exp, monkeypatch):
    monkeypatch.setattr(signal_books.strategy, "PULLBACK_ENTRIES", True)
    fired = {"symbol": "ETHUSDT", "action": "SELL", "entry_price": 100.0, "stop_loss": 104.0,
             "take_profit_1": 95.0, "take_profit_2": 92.0, "confidence": 72, "thesis": "x", "timeframe": "4h"}
    o = asyncio.run(signal_books.on_fired(fired))
    w = exp["signals_claude"]
    assert o["post_only"] and o["limit_price"] == pytest.approx(101.0) and o["stop_loss"] == 104.0   # 0.25R up, same stop
    assert w.get_portfolio()["open_trades"] == []
    assert any(sh["claude_verdict"] == "market_twin" and sh["maker_order_id"] == o["id"] for sh in w._snapshot()["shadows"])


def test_claude_book_takes_the_fired_signal_at_claudes_levels(exp):
    fired = {"symbol": "ETHUSDT", "action": "SELL", "entry_price": 100.0, "stop_loss": 103.0,
             "take_profit_1": 95.0, "take_profit_2": 92.0, "confidence": 72, "thesis": "x", "timeframe": "4h"}
    t = asyncio.run(signal_books.on_fired(fired))
    assert t["direction"] == "short" and t["stop_loss"] == 103.0 and t["take_profit_2"] == 92.0
    assert asyncio.run(signal_books.on_fired({**fired, "symbol": "BTCUSDT", "stop_loss": None})) is None


# ── Claude swing ───────────────────────────────────────────────────────────────

def test_validate_enforces_reward_to_risk_at_the_live_price():
    ok = {"symbol": "SOLUSDT", "direction": "long", "entry_type": "market", "entry": 100, "stop": 95, "target": 112}
    assert claude_swing.validate(ok, 100.0, {"SOLUSDT"}) is None
    assert "reward:risk" in claude_swing.validate(ok, 104.0, {"SOLUSDT"})      # price ran: 8 vs 9 → < 2
    assert "wrong side" in claude_swing.validate({**ok, "stop": 101}, 100.0, {"SOLUSDT"})
    assert "brief" in claude_swing.validate(ok, 100.0, {"ETHUSDT"})


def _fake_claude(monkeypatch, decision: dict):
    calls = []

    async def create(**kw):
        calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="…"), SimpleNamespace(type="text", text=json.dumps(decision))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))
    monkeypatch.setattr(claude_swing, "get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    return calls


def test_review_opens_valid_trades_and_rejects_the_rest(exp, monkeypatch):
    calls = _fake_claude(monkeypatch, {
        "market_view": "risk-on", "positions": [],
        "new_trades": [
            {"symbol": "SOLUSDT", "direction": "long", "entry_type": "market", "entry": 100, "stop": 96, "target": 110,
             "hold_days": 3, "confidence": 70, "thesis": "breakout retest", "invalidation": "4h close below 97"},
            {"symbol": "ETHUSDT", "direction": "short", "entry_type": "market", "entry": 100, "stop": 102, "target": 99,
             "hold_days": 2, "confidence": 60, "thesis": "weak", "invalidation": "x"},
        ]})
    entry = asyncio.run(claude_swing.review([_scan("SOLUSDT"), _scan("ETHUSDT")], {}))
    assert len(calls) == 1 and "SOLUSDT" in calls[0]["messages"][0]["content"]
    assert [o["symbol"] for o in entry["opened"]] == ["SOLUSDT"]
    assert entry["rejected"][0]["symbol"] == "ETHUSDT" and "reward:risk" in entry["rejected"][0]["why"]
    t = exp["claude_swing"].get_portfolio()["open_trades"][0]
    assert t["exit_policy"] == "manual" and t["invalidation"] == "4h close below 97" and t["take_profit_2"] == 110
    assert claude_swing.reviews()[0]["market_view"] == "risk-on"


def test_review_manages_positions_and_enforces_the_hold_period(exp, monkeypatch):
    w = exp["claude_swing"]
    keep = w.open_trade("SOLUSDT", "long", 100.0, 95.0, 110.0, 110.0, risk_pct=1, extra={"exit_policy": "manual", "hold_days": 5})
    close = w.open_trade("ETHUSDT", "long", 100.0, 95.0, 110.0, 110.0, risk_pct=1, extra={"exit_policy": "manual", "hold_days": 5})
    old = w.open_trade("BTCUSDT", "long", 100.0, 95.0, 110.0, 110.0, risk_pct=1, extra={"exit_policy": "manual", "hold_days": 1})
    w.annotate(old["id"], opened_at="2026-01-01T00:00:00+00:00")
    _fake_claude(monkeypatch, {"positions": [{"id": close["id"], "action": "close", "reason": "invalidated"},
                                             {"id": keep["id"], "action": "tighten", "new_stop": 98, "reason": "lock in"}],
                               "new_trades": []})
    asyncio.run(claude_swing.review([_scan("SOLUSDT"), _scan("ETHUSDT"), _scan("BTCUSDT")], {}))
    left = {t["symbol"]: t for t in w.get_portfolio()["open_trades"]}
    assert set(left) == {"SOLUSDT"} and left["SOLUSDT"]["stop_loss"] == 98
    reasons = {t["symbol"]: t["close_reason"] for t in w.all_trades()["closed"]}
    assert reasons == {"ETHUSDT": "claude_closed", "BTCUSDT": "hold_period_over"}


def test_reviews_respect_the_interval_and_daily_budget(exp, monkeypatch):
    calls = _fake_claude(monkeypatch, {"positions": [], "new_trades": []})

    async def go():
        await claude_swing.maybe_review([_scan()], {})
        await asyncio.sleep(0.05)
        await claude_swing.maybe_review([_scan()], {})                        # not due yet
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert len(calls) == 1
    from storage import state as kv
    kv.set_sync(kv.key("claude_swing", "last_review"), time.time() - 10 * 3600)
    monkeypatch.setattr(claude_swing, "DAILY_CALLS", 1)                      # budget already used
    asyncio.run(claude_swing.maybe_review([_scan()], {}))
    assert len(calls) == 1
