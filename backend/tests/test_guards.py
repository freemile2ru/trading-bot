import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from trading import guards


def _trades(rows):
    """rows: (book, direction, r) oldest first."""
    now = time.time()
    return [{"book": b, "direction": d, "r": r, "closed_ts": now - (len(rows) - i) * 60, "symbol": "SOLUSDT",
             "close_reason": "stop_loss" if r < 0 else "trailing_stop", "peak_r": 0.5, "setups": [], "regime": None}
            for i, (b, d, r) in enumerate(rows)]


@pytest.fixture
def history(monkeypatch):
    rows = []
    monkeypatch.setattr(guards, "closed_trades", lambda max_age_s=60: _trades(rows))
    return rows


def test_direction_gate_closes_after_a_losing_run_and_reopens(history):
    history += [("signals_rules", "short", 1.0)] * 10
    assert guards.entry_block("signals_rules", "short") is None
    history += [("signals_claude", "short", -1.0)] * 6                 # last 10 shorts: 4 × +1, 6 × −1 → −2R
    why = guards.entry_block("auto", "short")
    assert why and "last 10 shorts are net -2.0R" in why
    assert guards.entry_block("auto", "long") is None                  # fewer than 10 longs: open
    history += [("auto", "short", 2.0)] * 3                             # last 10: 1 × +1, 6 × −1, 3 × +2 → +1R
    assert guards.entry_block("auto", "short") is None


def test_open_risk_cap(history):
    assert guards.entry_block("signals_rules", "long", 0.5, open_risk_pct=3.5, new_risk_pct=1.0)
    assert guards.entry_block("signals_rules", "long", 0.5, open_risk_pct=2.5, new_risk_pct=1.0) is None


def test_adjustments_are_bounded_and_expire(history, monkeypatch):
    a = guards.set_adjustments("signals", {"risk_mult": 5, "conviction_add": 0.9, "max_open_risk_pct": 0.5,
                                           "pause_hours": {"long": 100, "sideways": 3}}, "test", "test")
    assert a["risk_mult"] == 1.0 and a["conviction_add"] == 0.15 and a["max_open_risk_pct"] == 2.0
    assert set(a["paused"]) == {"long"} and a["paused"]["long"] - time.time() <= 48 * 3600 + 5
    assert "paused" in guards.entry_block("signals_claude", "long")
    assert "conviction" in guards.entry_block("signals_rules", "short", conviction=0.25)
    assert guards.entry_block("auto", "long") is None                   # another family is unaffected
    monkeypatch.setattr(guards.time, "time", lambda: a["until"] + 1)
    assert guards.adjustments("signals") == {}


def test_losses_trigger_a_cool_off_and_a_bounded_claude_review(history, monkeypatch):
    history += [("signals_rules", "short", -1.0)] * 5
    assert guards._review_due("signals") == "5 losses in a row"
    assert guards._review_due("autotrader") is None

    async def create(**kw):
        body = {"diagnosis": "shorting after the move", "adjustments": {"risk_mult": 0.3, "pause_hours": {"short": 12}},
                "what_to_watch": "shorts closing green again"}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))], stop_reason="end_turn")
    from oracle import claude_oracle
    monkeypatch.setattr(claude_oracle, "get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    asyncio.run(guards.check())
    a = guards.adjustments("signals")
    assert a["risk_mult"] == 0.3 and "short" in a["paused"] and a["source"] == "claude review"
    assert "shorting after the move" in a["reason"]
    assert guards.risk_mult("signals_claude") == 0.3 and guards.risk_mult("auto") == 1.0
    assert guards._review_due("signals") is None                         # once a day


def test_signal_book_skip_is_tracked_as_a_shadow(history, tmp_path, monkeypatch):
    from experiments import books, signal_books
    from trading.paper_trader import PaperWallet
    w = PaperWallet(str(tmp_path / "s.db"), label="signals_claude")
    w.reset_portfolio(10_000)
    monkeypatch.setattr(books, "WALLETS", {**books.WALLETS, "signals_claude": w})
    monkeypatch.setattr(books, "ENABLED", True)

    async def price(symbol):
        return 100.0
    monkeypatch.setattr(signal_books.monitor, "live_price", price)
    history += [("signals_rules", "short", -1.0)] * 10
    fired = {"symbol": "ETHUSDT", "action": "SELL", "entry_price": 100.0, "stop_loss": 103.0, "take_profit_1": 96.0,
             "take_profit_2": 94.0, "confidence": 70, "thesis": "x", "timeframe": "4h"}
    assert asyncio.run(signal_books.on_fired(fired)) is None
    assert w.get_portfolio()["open_trades"] == []
    shadows = w._snapshot()["shadows"]
    assert shadows and shadows[0]["claude_verdict"] == "gated"


def test_gate_reopens_from_the_shadows_of_skipped_entries(monkeypatch):
    """No real longs can close while longs are gated — their skipped shadows must reopen the gate."""
    now = time.time()
    real = [{"book": "auto", "direction": "long", "r": -1.0, "closed_ts": now - 3000 + i} for i in range(10)]
    shadows = [{"book": "auto", "direction": "long", "r": 1.5, "closed_ts": now - 100 + i, "gated": True} for i in range(7)]
    monkeypatch.setattr(guards, "closed_trades", lambda max_age_s=60: sorted(real + shadows, key=lambda t: t["closed_ts"]))
    assert guards.direction_state()["long"]["open"]                    # last 10: 3 × −1, 7 × +1.5 → +7.5R
    monkeypatch.setattr(guards, "closed_trades", lambda max_age_s=60: real)
    assert not guards.direction_state()["long"]["open"]


def test_one_skipped_move_counts_once_however_many_books_wanted_it():
    t0 = 1_800_000_000
    row = lambda book, sym, r, dt, gated=True: {"book": book, "direction": "long", "symbol": sym, "r": r,
                                                "closed_ts": t0 + dt, "gated": gated}
    rows = [row("auto_rules", "APTUSDT", -1.1, 0), row("auto_live", "APTUSDT", -1.1, 1), row("auto_rules", "APTUSDT", -1.0, 60),
            row("signals_rules", "FILUSDT", -1.0, 0), row("signals_rules", "FILUSDT", -1.0, 7 * 3600),
            row("auto", "APTUSDT", -0.5, 120, gated=False), row("auto_rules", "APTUSDT", -0.5, 120, gated=False)]
    kept = guards._dedupe_shadows(sorted(rows, key=lambda r: r["closed_ts"]))
    assert [(r["symbol"], r["r"], r["gated"]) for r in kept] == [
        ("APTUSDT", -1.1, True), ("FILUSDT", -1.0, True), ("APTUSDT", -0.5, False), ("APTUSDT", -0.5, False),
        ("FILUSDT", -1.0, True)]                                       # real trades all kept; FIL again after 7h


def test_a_family_with_every_book_off_is_not_reviewed(history, monkeypatch):
    from trading import book_switches
    history += [("signals_claude", "short", -1.0)] * 8                 # a streak that would trigger a review
    calls = []

    async def review(family, trigger):
        calls.append(family)
    monkeypatch.setattr(guards, "review", review)
    monkeypatch.setattr(book_switches, "enabled", lambda name: name not in ("signals_claude", "signals_rules"))
    asyncio.run(guards.check())
    assert calls == [] and guards.status()["families"]["signals"]["active"] is False
