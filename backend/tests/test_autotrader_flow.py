"""End-to-end autotrader decisions through the real (isolated) state store.

Regression guard: after the Redis→SQLite migration the module name `state` shadowed local
dicts called `state`, and every decision crashed. Nothing exercised `_decide`, so it shipped.
"""

import asyncio
import time

import pytest

from autotrader import engine, strategy
from storage import state as kv
from trading.paper_trader import PaperWallet


@pytest.fixture(autouse=True)
def _scanner_lane_on(monkeypatch):
    """These tests exercise the scanner path itself; the lane is retired in production (engine.TA_LANE)."""
    monkeypatch.setattr(engine, "TA_LANE", True)



@pytest.fixture
def auto(tmp_path, monkeypatch):
    w = PaperWallet(str(tmp_path / "swing.db"), label="auto")
    w.reset_portfolio(10_000)
    monkeypatch.setattr(engine, "auto_wallet", w)
    monkeypatch.setattr(engine, "main_wallet", PaperWallet(str(tmp_path / "swing.db"), label="main"))
    twin = PaperWallet(str(tmp_path / "swing.db"), label="auto_rules")
    twin.reset_portfolio(10_000)
    monkeypatch.setattr(engine, "RULES_TWIN", engine.Variant("auto_rules", use_claude=False, wallet=twin))

    async def no_fired():
        return []

    async def price(symbol):
        return 100.0

    async def no_monitor(wallet):
        return []

    monkeypatch.setattr(engine.escalation, "recent_fired", no_fired)
    monkeypatch.setattr(engine.monitor, "live_price", price)
    async def calm_btc():
        return 30.0               # inside the regime gate, and no network in tests

    monkeypatch.setattr(engine.monitor, "run", no_monitor)
    monkeypatch.setattr(engine, "btc_vol_pct", calm_btc)
    return w


def _scan(symbol="SOLUSDT", conviction=0.5):
    fast = {"signal": "buy", "rsi": 50, "score": 0.3, "price": 100.0, "ema_21": 99.8, "atr": 0.5}
    return {"symbol": symbol, "base": symbol[:-4], "price": 100.0, "conviction": conviction,
            "best_timeframe": "1h", "hold_duration": "4h", "tf_alignment": {"multiplier": 1.2},
            "conviction_detail": {"aligned_setups": ["x"], "opposing_setups": []},
            "fa": {"score": 0.3, "label": "bullish"}, "triggers": {"atr_pct": 1.0},
            "signal_package": {"ta_summary": {"timeframe_signals": {"15m": fast}}}}


def test_decisions_run_and_persist_state(auto):
    asyncio.run(engine.on_scans([_scan()], {"SOLUSDT"}, {}))
    st = asyncio.run(kv.hgetall(engine.STATE_KEY))["SOLUSDT"]
    assert st["status"] in ("armed", "blocked", "watching", "confirming")
    assert st["reasons"]


def test_full_confirmation_path_opens_a_trade(auto, monkeypatch):
    monkeypatch.setattr(strategy, "MAKER_ENTRIES", False)          # the market-order path
    monkeypatch.setattr(strategy, "PULLBACK_ENTRIES", False)
    monkeypatch.setattr(strategy, "timing", lambda *a: {"score": 80, "enter": True, "blocked": False, "reasons": ["forced"]})

    async def claude(pkg, gm):
        return {"trade_recommendation": {"action": "BUY", "confidence": 75, "entry_price": 100.0},
                "reasoning": {"primary_thesis": "test"}}

    monkeypatch.setattr(engine, "synthesize_trade", claude)

    async def go():
        await engine.on_scans([_scan()], {"SOLUSDT"}, {})
        for _ in range(100):                     # the confirmation runs as a background task
            if auto.get_portfolio()["open_trades"] and \
                    (await kv.get(engine._claude_key("SOLUSDT")) or {}).get("status") == "approved":
                break
            await asyncio.sleep(0.02)

    asyncio.run(go())
    trades = auto.get_portfolio()["open_trades"]
    assert [t["symbol"] for t in trades] == ["SOLUSDT"]
    assert asyncio.run(kv.get(engine._claude_key("SOLUSDT")))["status"] == "approved"


def test_declined_entry_vetoes_and_is_tracked(auto, monkeypatch):
    monkeypatch.setattr(strategy, "timing", lambda *a: {"score": 80, "enter": True, "blocked": False, "reasons": ["forced"]})

    async def claude(pkg, gm):
        return {"trade_recommendation": {"action": "HOLD", "confidence": 40}, "reasoning": {}}

    monkeypatch.setattr(engine, "synthesize_trade", claude)

    async def go():
        await engine.on_scans([_scan()], {"SOLUSDT"}, {})
        for _ in range(50):
            if (await kv.get(engine._claude_key("SOLUSDT")) or {}).get("status") == "rejected":
                break
            await asyncio.sleep(0.02)
        await engine.on_scans([_scan()], {"SOLUSDT"}, {})

    asyncio.run(go())
    st = asyncio.run(kv.hgetall(engine.STATE_KEY))["SOLUSDT"]
    assert st["status"] == "vetoed"
    assert auto.get_portfolio()["open_trades"] == []
    assert auto.shadow_starts()                  # the declined entry is shadow-tracked


def test_regime_gate_blocks_shorts_and_high_btc_vol(monkeypatch):
    from autotrader import strategy
    monkeypatch.setattr(strategy, "ALLOW_SHORTS", False)
    monkeypatch.setattr(strategy, "MAX_BTC_VOL", 37.0)
    assert "shorts off" in strategy.regime_block("short", 20.0)
    assert "standing aside" in strategy.regime_block("long", 45.0)
    assert strategy.regime_block("long", 30.0) is None
    assert strategy.regime_block("long", None) is None           # data missing → don't block
    monkeypatch.setattr(strategy, "ALLOW_SHORTS", True)
    monkeypatch.setattr(strategy, "MAX_BTC_VOL", 0.0)
    assert strategy.regime_block("short", 99.0) is None           # both switches off


def test_coin_held_by_another_system_is_skipped_then_freshly_reviewed(auto, monkeypatch):
    """Don't stack onto a coin another system is in; once it exits, review from scratch —
    a Claude verdict from before the block is dropped, the old signal is never replayed."""
    holding = {"SOLUSDT": [{"system": "main", "mode": "live", "direction": "long", "entry": 100.0}]}
    monkeypatch.setattr(engine.coin_lock, "held_by_others", lambda system: holding)

    async def go():
        await kv.set(engine._claude_key("SOLUSDT"), {"status": "rejected", "at": 0, "note": "old veto"}, 3600)
        await engine.on_scans([_scan()], {"SOLUSDT"}, {})
        busy = (await kv.hgetall(engine.STATE_KEY))["SOLUSDT"]
        holding.clear()                                   # the main wallet exits
        await engine.on_scans([_scan()], {"SOLUSDT"}, {})
        return busy, (await kv.hgetall(engine.STATE_KEY))["SOLUSDT"], await kv.get(engine._claude_key("SOLUSDT"))

    busy, after, claude = asyncio.run(go())
    assert busy["status"] == "busy_elsewhere" and "Main (live)" in busy["reasons"][0]
    assert auto.get_portfolio()["open_trades"] == []
    assert after["status"] != "busy_elsewhere" and after["status"] != "vetoed"
    assert "exited — re-reviewed" in after["reasons"][0]
    assert not claude or claude.get("status") != "rejected"


def test_unreadable_holdings_block_entries(auto, monkeypatch):
    def boom(system):
        raise RuntimeError("db locked")
    monkeypatch.setattr(engine.coin_lock, "held_by_others", boom)
    asyncio.run(engine.on_scans([_scan()], {"SOLUSDT"}, {}))
    assert asyncio.run(kv.hgetall(engine.STATE_KEY))["SOLUSDT"]["status"] == "busy_elsewhere"


def test_rules_twin_enters_the_same_signal_without_claude(auto, monkeypatch):
    """The paired experiment: identical rules, but the twin never asks Claude."""
    monkeypatch.setattr(engine.experiment_books, "ENABLED", True)
    monkeypatch.setattr(strategy, "MAKER_ENTRIES", False)
    monkeypatch.setattr(strategy, "PULLBACK_ENTRIES", False)
    monkeypatch.setattr(strategy, "timing", lambda *a: {"score": 80, "enter": True, "blocked": False, "reasons": ["forced"]})
    calls = []

    async def claude(pkg, gm):
        calls.append(engine._v().name)
        return {"trade_recommendation": {"action": "HOLD", "confidence": 10}, "reasoning": {}}
    monkeypatch.setattr(engine, "synthesize_trade", claude)

    async def go():
        await engine.on_scans([_scan()], {"SOLUSDT"}, {})
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.02)
    asyncio.run(go())
    twin = engine.RULES_TWIN.wallet
    trades = twin.get_portfolio()["open_trades"]
    assert [t["symbol"] for t in trades] == ["SOLUSDT"] and trades[0]["claude_verdict"] == "skipped"
    assert auto.get_portfolio()["open_trades"] == []          # Claude said HOLD for the real one
    assert calls == ["auto"]                                  # only the real autotrader asked Claude
    states = asyncio.run(kv.hgetall(kv.key("auto_rules", "state")))
    assert states["SOLUSDT"]["status"] == "in_trade"



def test_maker_entry_rests_post_only_with_a_market_twin(auto, monkeypatch):
    monkeypatch.setattr(strategy, "MAKER_ENTRIES", True)
    monkeypatch.setattr(strategy, "PULLBACK_ENTRIES", False)
    monkeypatch.setattr(strategy, "timing", lambda *a: {"score": 80, "enter": True, "blocked": False, "reasons": ["forced"]})

    async def claude(pkg, gm):
        return {"trade_recommendation": {"action": "BUY", "confidence": 75, "entry_price": 100.0},
                "reasoning": {"primary_thesis": "test"}}
    monkeypatch.setattr(engine, "synthesize_trade", claude)

    async def go():
        await engine.on_scans([_scan()], {"SOLUSDT"}, {})
        for _ in range(100):
            if auto.get_orders()["pending"]:
                break
            await asyncio.sleep(0.02)
    asyncio.run(go())
    order = auto.get_orders()["pending"][0]
    assert order["post_only"] and order["limit_price"] == pytest.approx(100.0 * (1 - strategy.MAKER_OFFSET_BPS / 1e4))
    assert order["expires_at"] - order["placed_at"] == pytest.approx(strategy.MAKER_TTL_S, abs=2)
    shadows = auto._snapshot()["shadows"]
    assert any(sh.get("claude_verdict") == "market_twin" and sh.get("maker_order_id") == order["id"] for sh in shadows)
    assert auto.get_portfolio()["open_trades"] == []                 # nothing bought until it fills


def test_pullback_entry_rests_back_toward_the_stop_with_the_stop_unchanged(auto, monkeypatch):
    monkeypatch.setattr(strategy, "PULLBACK_ENTRIES", True)
    monkeypatch.setattr(strategy, "timing", lambda *a: {"score": 80, "enter": True, "blocked": False, "reasons": ["forced"]})

    async def claude(pkg, gm):
        return {"trade_recommendation": {"action": "BUY", "confidence": 75, "entry_price": 100.0},
                "reasoning": {"primary_thesis": "test"}}
    monkeypatch.setattr(engine, "synthesize_trade", claude)

    async def go():
        await engine.on_scans([_scan()], {"SOLUSDT"}, {})
        for _ in range(100):
            if auto.get_orders()["pending"]:
                break
            await asyncio.sleep(0.02)
    asyncio.run(go())
    order = auto.get_orders()["pending"][0]
    _, fast = strategy.fast_tf(_scan())
    lv = strategy.build_levels("long", 100.0, _scan(), fast)
    assert order["post_only"] and order["stop_loss"] == pytest.approx(lv["stop_loss"])     # the signal's stop
    assert order["limit_price"] == pytest.approx(100.0 - (100.0 - lv["stop_loss"]) * strategy.PULLBACK_R)
    assert order["expires_at"] - order["placed_at"] == pytest.approx(strategy.PULLBACK_TTL_S, abs=2)
    twin = next(sh for sh in auto._snapshot()["shadows"] if sh.get("maker_order_id") == order["id"])
    assert twin["claude_verdict"] == "market_twin" and twin["entry_price"] >= 100.0      # the market entry it replaced


def test_post_only_orders_fill_only_when_price_trades_through(tmp_path):
    from trading.paper_trader import MINUTE_MS
    w = PaperWallet(str(tmp_path / "m.db"), label="m")
    w.reset_portfolio(10_000)
    o = w.place_order("SOLUSDT", "long", 100.0, 97.0, 104.5, 107.5, 50, time.time() + 900, extra={"post_only": True})
    start = o["checked_from_ms"]
    bar = lambda i, lo: {"open_time": start + i * MINUTE_MS, "open": 100.2, "high": 100.3, "low": lo, "close": 100.1}
    w.process_paths({"SOLUSDT": {"bars": [bar(0, 100.0), bar(1, 99.99)], "price": 100.1}})
    assert w.get_orders()["pending"]                                  # touched, even 1 bp through: no fill
    w.process_paths({"SOLUSDT": {"bars": [bar(2, 99.97)], "price": 100.1}})
    t = w.get_portfolio()["open_trades"][0]
    assert t["entry_price"] == pytest.approx(100.0)                   # a resting order fills at its own price


def test_market_twins_stay_out_of_the_exit_comparison(tmp_path):
    from trading import exits
    w = PaperWallet(str(tmp_path / "x.db"), label="x")
    w.reset_portfolio(10_000)
    w.add_virtual("ETHUSDT", "long", 100, 98, 103, 105, "1h", 1.0,
                  {"claude_verdict": "market_twin", "maker_order_id": "abc", "exit_policy": exits.DEFAULT_POLICY})
    from trading.paper_trader import MINUTE_MS
    start = (int(time.time() * 1000) // MINUTE_MS + 1) * MINUTE_MS
    w.process_paths({"ETHUSDT": {"bars": [{"open_time": start, "open": 100, "high": 100.1, "low": 97.5, "close": 97.6}], "price": 97.6}})
    rep = w.shadow_report()
    assert rep["results"][0]["maker_order_id"] == "abc"
    assert all(p["trades"] == 0 for p in rep["policies"])


def test_daily_minimum_takes_the_strongest_setup_after_a_quiet_day(auto, monkeypatch):
    monkeypatch.setattr(engine, "DAILY_MIN_ENABLED", True)
    monkeypatch.setattr(strategy, "timing", lambda *a: {"score": 10, "enter": False, "blocked": True, "reasons": ["no room"]})

    async def no_regime():
        return None
    monkeypatch.setattr(engine.regime, "current", no_regime)
    scans = [_scan("SOLUSDT", 0.25), _scan("ETHUSDT", -0.4), _scan("XRPUSDT", 0.1)]
    asyncio.run(engine.on_scans(scans, {"SOLUSDT", "ETHUSDT", "XRPUSDT"}, {}))
    trades = auto.get_portfolio()["open_trades"]
    assert [(t["symbol"], t["direction"], t["forced_daily"]) for t in trades] == [("ETHUSDT", "short", True)]
    asyncio.run(engine.on_scans(scans, {"SOLUSDT", "ETHUSDT", "XRPUSDT"}, {}))
    assert len(auto.get_portfolio()["open_trades"]) == 1                  # it just traded: no second forced entry


def test_daily_minimum_waits_while_the_autotrader_is_trading(auto, monkeypatch):
    monkeypatch.setattr(engine, "DAILY_MIN_ENABLED", True)
    auto.open_trade(symbol="BNBUSDT", direction="long", entry_price=100.0, stop_loss=98.0, take_profit_1=103.0,
                    take_profit_2=106.0, size_pct=5, timeframe="1h", hold_duration="N/A", extra={})
    monkeypatch.setattr(strategy, "timing", lambda *a: {"score": 10, "enter": False, "blocked": True, "reasons": ["x"]})
    asyncio.run(engine.on_scans([_scan("ETHUSDT", -0.4)], {"ETHUSDT"}, {}))
    assert [t["symbol"] for t in auto.get_portfolio()["open_trades"]] == ["BNBUSDT"]


def test_daily_minimum_lowers_its_bar_after_a_long_idle_spell(auto, monkeypatch):
    monkeypatch.setattr(engine, "DAILY_MIN_ENABLED", True)
    monkeypatch.setattr(strategy, "timing", lambda *a: {"score": 10, "enter": False, "blocked": True, "reasons": ["x"]})

    async def no_regime():
        return None
    monkeypatch.setattr(engine.regime, "current", no_regime)
    asyncio.run(engine.on_scans([_scan("SOLUSDT", 0.11)], {"SOLUSDT"}, {}))         # never traded: idle "forever"
    assert [t["symbol"] for t in auto.get_portfolio()["open_trades"]] == ["SOLUSDT"]


def _event_row(status="open", symbol="ABCUSDT", kind="spot listing", close_reason=None):
    import uuid
    from experiments import events
    now = time.time()
    row = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "kind": kind, "title": "Binance Will List Abc (ABC)",
           "announced_ts": now - 120, "opened_ts": now - 60, "deadline_ts": now + 7 * 86400, "coin_entry": 1.0,
           "btc_entry": 100.0, "notional": 1000.0, "status": status, "data": "{}"}
    events._write("INSERT INTO events (id, symbol, kind, title, announced_ts, opened_ts, deadline_ts, coin_entry, btc_entry, notional, "
                  "status, data) VALUES (:id,:symbol,:kind,:title,:announced_ts,:opened_ts,:deadline_ts,:coin_entry,:btc_entry,:notional,:status,:data)", row)
    if status == "closed":
        events._write("UPDATE events SET closed_ts=?, close_reason=?, net=0 WHERE id=?", (now, close_reason or "hold_complete", row["id"]))
    return row


def test_autotrader_mirrors_the_event_book_and_closes_with_it(auto, monkeypatch):
    monkeypatch.setattr(engine.experiment_books, "ENABLED", True)
    monkeypatch.setattr(engine, "EVENTS_IN_AUTO", True)
    asyncio.run(kv.set(kv.key("auto", "events_since"), {"at": 1.0}, 3600))           # the lane has been following for a while
    from experiments import events
    row = _event_row()
    asyncio.run(engine.on_scans([_scan("SOLUSDT", 0.1)], {"SOLUSDT"}, {}))
    held = {t["symbol"]: t for t in auto.get_portfolio()["open_trades"]}
    assert set(held) == {"ABCUSDT", "BTCUSDT"}
    coin, hedge = held["ABCUSDT"], held["BTCUSDT"]
    assert coin["direction"] == "short" and hedge["direction"] == "long"
    assert coin["event_id"] == row["id"] and coin["exit_policy"] == "manual" and coin["expires_at"] == pytest.approx(row["deadline_ts"] + 3600)
    from experiments import events as events_book
    expected = engine._lane_budget("events", 10_000) * events_book.LEG_PCT / 100          # the event lane's budget share × the book's leg share
    assert coin["stop_loss"] == pytest.approx(100.0 * 1.25) and coin["notional_usd"] == pytest.approx(expected, rel=0.01)
    asyncio.run(engine.on_scans([_scan("SOLUSDT", 0.1)], {"SOLUSDT"}, {}))
    assert len(auto.get_portfolio()["open_trades"]) == 2                         # mirrored once
    events._write("UPDATE events SET status='closed', closed_ts=?, close_reason='funding_kill', net=0 WHERE id=?", (time.time(), row["id"]))
    asyncio.run(engine.on_scans([_scan("SOLUSDT", 0.1)], {"SOLUSDT"}, {}))
    closed = {t["symbol"]: t for t in auto.all_trades()["closed"]}
    assert auto.get_portfolio()["open_trades"] == [] and closed["ABCUSDT"]["close_reason"] == "event_funding_kill"
    assert closed["BTCUSDT"]["close_reason"] == "event_funding_kill"


def test_event_trades_are_left_alone_by_the_thesis_watcher(tmp_path, monkeypatch):
    from trading import thesis_exit as TE
    from trading.paper_trader import PaperWallet
    w = PaperWallet(str(tmp_path / "w.db"), label="auto")
    w.reset_portfolio(10_000)
    w.open_trade(symbol="ABCUSDT", direction="short", entry_price=1.0, stop_loss=1.25, take_profit_1=0.5, take_profit_2=0.4,
                 size_pct=5, timeframe="1d", hold_duration="N/A", extra={"event_id": "x", "exit_policy": "manual"})
    monkeypatch.setattr(TE, "fade_note", lambda t, now=None: "thesis faded")
    assert asyncio.run(TE.check(w)) == [] and len(w.get_portfolio()["open_trades"]) == 1
