import asyncio

import pandas as pd
import pytest

from autotrader import strategy
from markets import engine as M
from trading import book_switches
from trading.paper_trader import PaperWallet


@pytest.fixture
def mk(tmp_path, monkeypatch):
    wallets = {n: PaperWallet(str(tmp_path / "m.db"), label=n) for n in M.BOOK_NAMES}
    for w in wallets.values():
        w.reset_portfolio(10_000)
    monkeypatch.setattr(M, "WALLETS", wallets)

    async def price(symbol):
        return 100.0
    monkeypatch.setattr(M.monitor, "live_price", price)

    async def candles(symbol, tf):                      # a steady uptrend with noise
        step = M.TF_MS[tf]
        return [{"open_time": i * step, "open": 100 + i * 0.05, "high": 100.4 + i * 0.05, "low": 99.6 + i * 0.05,
                 "close": 100 + i * 0.05 + (0.2 if i % 3 else -0.2), "volume": 1000 + (i % 7) * 50} for i in range(200)]
    monkeypatch.setattr(M, "_candles", candles)
    return wallets


def test_scan_builds_a_conviction_reading_from_perp_candles(mk):
    sc = asyncio.run(M.scan("XAUUSDT", "futures"))
    assert sc["market"] == "futures" and -1 <= sc["conviction"] <= 1 and sc["best_timeframe"]
    assert "commodity perpetual" in sc["signal_package"]["asset_note"]


def _force_entry(monkeypatch):
    monkeypatch.setattr(strategy, "bias", lambda sc: ("long", ["forced"]))
    monkeypatch.setattr(strategy, "timing", lambda *a: {"score": 80, "enter": True, "blocked": False, "reasons": ["forced"]})


def test_rules_book_enters_and_claude_book_waits_for_claude(mk, monkeypatch):
    _force_entry(monkeypatch)
    calls = []

    async def claude(pkg, gm):
        calls.append(pkg["symbol"])
        return {"trade_recommendation": {"action": "BUY", "confidence": 80}, "reasoning": {"primary_thesis": "t"}}
    import oracle.claude_oracle as co
    monkeypatch.setattr(co, "synthesize_trade", claude)
    sc = asyncio.run(M.scan("XAUUSDT", "futures"))
    asyncio.run(M._rules_and_claude("futures", [sc]))
    assert [t["symbol"] for t in mk["futures_rules"].get_portfolio()["open_trades"]] == ["XAUUSDT"]
    claude_trades = mk["futures_claude"].get_portfolio()["open_trades"]
    assert calls == ["XAUUSDT"] and claude_trades[0]["claude_verdict"] == "approved"
    assert M.claude_calls_today("futures") == 1


def test_claude_decline_vetoes_and_switched_off_books_stay_out(mk, monkeypatch):
    _force_entry(monkeypatch)

    async def claude(pkg, gm):
        return {"trade_recommendation": {"action": "HOLD", "confidence": 30}, "reasoning": {}}
    import oracle.claude_oracle as co
    monkeypatch.setattr(co, "synthesize_trade", claude)
    asyncio.run(book_switches.set_enabled("futures_rules", False))
    sc = asyncio.run(M.scan("XAGUSDT", "futures"))
    asyncio.run(M._rules_and_claude("futures", [sc]))
    assert mk["futures_rules"].get_portfolio()["open_trades"] == []
    assert mk["futures_claude"].get_portfolio()["open_trades"] == []
    assert M.kv.get_sync(M.kv.key("markets", "futures_claude", "veto", "XAGUSDT"))


def test_claude_budget_caps_the_asks(mk, monkeypatch):
    _force_entry(monkeypatch)
    monkeypatch.setattr(M, "CLAUDE_DAILY", 0)
    called = []

    async def claude(pkg, gm):
        called.append(1)
        return {}
    import oracle.claude_oracle as co
    monkeypatch.setattr(co, "synthesize_trade", claude)
    sc = asyncio.run(M.scan("XAUUSDT", "futures"))
    asyncio.run(M._rules_and_claude("futures", [sc]))
    assert called == [] and mk["futures_claude"].get_portfolio()["open_trades"] == []


def test_trend_book_goes_long_on_uptrends_and_closes_on_a_flip(mk, monkeypatch):
    up = pd.Series([100 * (1.001 ** i) for i in range(200)], index=pd.date_range("2026-01-01", periods=200))
    down = pd.Series(list(reversed(up.values)), index=up.index)
    series = {"GLD": up, "SLV": down}

    async def yahoo(t):
        return series.get(t)
    monkeypatch.setattr(M, "_yahoo_daily", yahoo)
    asyncio.run(M.trend_rebalance("futures", force=True))
    held = {t["symbol"]: t for t in mk["futures_trend"].get_portfolio()["open_trades"]}
    assert set(held) == {"XAUUSDT"} and held["XAUUSDT"]["exit_policy"] == "manual"
    assert held["XAUUSDT"]["stop_loss"] == pytest.approx(100 * (1 - M.TREND_DISASTER))
    series["GLD"] = down
    asyncio.run(M.trend_rebalance("futures", force=True))
    assert mk["futures_trend"].get_portfolio()["open_trades"] == []
    assert mk["futures_trend"].all_trades()["closed"][0]["close_reason"] == "trend_flip"


def test_summary_and_api(mk):
    from fastapi.testclient import TestClient
    import main
    s = M.summary("stocks")
    assert [b["kind"] for b in s["books"]] == ["trend", "rules", "claude"] and "NVDAUSDT" in s["symbols"]
    c = TestClient(main.app)
    assert c.get("/api/markets/futures").json()["label"] == "Futures"
    assert c.get("/api/markets/forex").status_code == 404


def test_claude_daily_picks_enter_on_claudes_own_call_once_a_day(mk, monkeypatch):
    asked = []

    async def claude(pkg, gm):
        asked.append(pkg["symbol"])
        return ({"trade_recommendation": {"action": "SELL", "confidence": 80}, "reasoning": {"primary_thesis": "rolling over"}}
                if pkg["symbol"] == "XAUUSDT" else {"trade_recommendation": {"action": "HOLD", "confidence": 40}, "reasoning": {}})
    import oracle.claude_oracle as co
    monkeypatch.setattr(co, "synthesize_trade", claude)
    monkeypatch.setattr(M, "PICKS_PER_DAY", 2)
    scans = [asyncio.run(M.scan(s, "futures")) for s in ("XAUUSDT", "XAGUSDT", "CLUSDT")]
    scans[0]["conviction"], scans[1]["conviction"], scans[2]["conviction"] = 0.6, -0.5, 0.1
    picks = asyncio.run(M.claude_picks("futures", scans, force=True))
    assert asked == ["XAUUSDT", "XAGUSDT"]                              # the two strongest, whatever the rules say
    t = mk["futures_claude"].get_portfolio()["open_trades"]
    assert [(x["symbol"], x["direction"], x["entry_source"]) for x in t] == [("XAUUSDT", "short", "daily_pick")]
    assert [p["entered"] for p in picks] == [True, False]
    assert M.summary("futures")["picks"]["picks"][0]["thesis"] == "rolling over"
    assert asyncio.run(M.claude_picks("futures", scans)) == []         # already run today


def test_rules_asks_leave_claudes_budget_for_the_daily_picks(mk, monkeypatch):
    _force_entry(monkeypatch)
    monkeypatch.setattr(M, "CLAUDE_DAILY", 4)
    monkeypatch.setattr(M, "PICKS_PER_DAY", 4)

    async def claude(pkg, gm):
        return {"trade_recommendation": {"action": "HOLD", "confidence": 30}, "reasoning": {}}
    import oracle.claude_oracle as co
    monkeypatch.setattr(co, "synthesize_trade", claude)
    sc = asyncio.run(M.scan("XAUUSDT", "futures"))
    asyncio.run(M._rules_and_claude("futures", [sc]))
    assert M.claude_calls_today("futures") == 0                        # all 4 held for the picks
    asyncio.run(M.claude_picks("futures", [sc], force=True))
    assert M.claude_calls_today("futures") == 1


def test_trend_band_stops_flapping_around_zero(mk, monkeypatch):
    idx = pd.date_range("2026-01-01", periods=200)
    wiggle = [1.0 + (0.002 if i % 2 else -0.002) for i in range(200)]       # some volatility to size with
    level = lambda six_month: pd.Series([100.0 * wiggle[i] if i < 74 else 100.0 * (1 + six_month) * wiggle[i] for i in range(200)],
                                        index=idx)                         # 126d return ≈ six_month
    series = {"GLD": level(0.03)}

    async def yahoo(t):
        return series.get(t)
    monkeypatch.setattr(M, "_yahoo_daily", yahoo)
    asyncio.run(M.trend_rebalance("futures", force=True))
    assert mk["futures_trend"].get_portfolio()["open_trades"] == []          # +3%: inside the band, no entry
    series["GLD"] = level(0.08)
    asyncio.run(M.trend_rebalance("futures", force=True))
    assert len(mk["futures_trend"].get_portfolio()["open_trades"]) == 1        # +8%: enters
    series["GLD"] = level(-0.03)
    asyncio.run(M.trend_rebalance("futures", force=True))
    assert len(mk["futures_trend"].get_portfolio()["open_trades"]) == 1        # −3%: holds through the dip
    series["GLD"] = level(-0.07)
    asyncio.run(M.trend_rebalance("futures", force=True))
    assert mk["futures_trend"].get_portfolio()["open_trades"] == []          # −7%: exits


def test_trend_books_drop_leftover_shadows_once(mk, monkeypatch):
    w = mk["stocks_trend"]
    w.open_trade(symbol="NVDAUSDT", direction="long", entry_price=100.0, stop_loss=85.0, take_profit_1=150.0,
                 take_profit_2=200.0, size_pct=5, timeframe="1d", hold_duration="N/A", extra={})
    assert w._snapshot()["shadows"]                                            # an old-style trend trade
    monkeypatch.setattr(M, "_trend_shadows_cleared", False)

    async def none(_w):
        return []
    monkeypatch.setattr(M.monitor, "run", none)
    asyncio.run(M.monitor_all())
    assert w._snapshot()["shadows"] == [] and w.get_portfolio()["open_trades"]
