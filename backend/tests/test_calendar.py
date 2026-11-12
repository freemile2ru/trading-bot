"""Event calendar: macro blackout window, earnings block from the cached list, near-event tags, and the hooks."""
import asyncio

import pytest

from ops import calendar
from storage import state as kv

FOMC = calendar._et("2026-10-28", 14, 0)       # 18:00 UTC
CPI = calendar._et("2026-11-10", 8, 30)


def test_macro_schedule_times_are_et():
    from datetime import datetime, timezone
    assert FOMC == datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc).timestamp()    # 14:00 EDT
    assert CPI == datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc).timestamp()    # 08:30 EST, after the clock change


@pytest.mark.parametrize("offset_min,blocked", [(-61, False), (-60, True), (-1, True), (0, True), (30, True), (31, False)])
def test_macro_blackout_window(offset_min, blocked):
    assert bool(calendar.macro_blackout(FOMC + offset_min * 60)) is blocked
    assert bool(calendar.macro_blackout(CPI + offset_min * 60)) is blocked


def test_blackout_message_names_the_event():
    assert calendar.macro_blackout(FOMC - 600).startswith("FOMC decision in 10 min")
    assert calendar.macro_blackout(FOMC + 600).startswith("FOMC decision 10 min ago")


def test_next_macro_stays_on_event_during_after_window():
    assert calendar.next_macro(FOMC + 20 * 60)["ts"] == FOMC
    assert calendar.next_macro(FOMC + 31 * 60)["ts"] > FOMC


def test_earnings_block_and_near_event(monkeypatch):
    now = 1_800_000_000.0
    kv.set_sync(kv.key("calendar", "earnings"),
                {"at": now, "events": [{"kind": "earnings", "symbol": "NVDAUSDT", "ticker": "NVDA", "ts": now + 1.5 * 86400,
                                        "label": "NVDA earnings (after the close)"}]}, 3600)
    assert calendar.earnings_block("NVDAUSDT", now).startswith("NVDA earnings")
    assert calendar.earnings_block("NVDAUSDT", now - 2 * 86400) is None     # 3.5 days out: fine
    assert calendar.earnings_block("NVDAUSDT", now + 2 * 86400) is None     # already reported: fine
    assert calendar.earnings_block("AAPLUSDT", now) is None
    assert calendar.near_event("NVDAUSDT", now) == "NVDA earnings (after the close)"
    assert calendar.near_event("AAPLUSDT", now) is None
    assert calendar.near_event("BTCUSDT", FOMC - 10 * 3600) == "FOMC decision"
    assert calendar.near_event("BTCUSDT", FOMC - 30 * 3600) is None
    st = calendar.status(now)
    assert st["blackout"] is None and st["upcoming"][0]["kind"] == "earnings"
    kv.delete_sync(kv.key("calendar", "earnings"))


def test_refresh_earnings_parses_nasdaq_rows(monkeypatch):
    now = 1_800_000_000.0
    kv.delete_sync(kv.key("calendar", "earnings"))
    monkeypatch.setattr(calendar, "_tickers", lambda: {"NVDA": "NVDAUSDT", "AAPL": "AAPLUSDT"})
    calls = []

    async def fake_get_json(url, params=None, headers=None):
        calls.append(params["date"])
        if len(calls) == 1:
            return {"data": {"rows": [{"symbol": "NVDA", "time": "time-after-hours"}, {"symbol": "AAPL", "time": "time-pre-market"},
                                      {"symbol": "TSLA", "time": "time-after-hours"}]}}
        if len(calls) == 2:
            raise RuntimeError("blip")
        return {"data": {"rows": None}}

    import data.http
    monkeypatch.setattr(data.http, "get_json", fake_get_json)
    events = asyncio.run(calendar.refresh_earnings(now))
    assert len(calls) == calendar.EARNINGS_LOOKAHEAD_DAYS + 1
    assert {e["symbol"] for e in events} == {"NVDAUSDT", "AAPLUSDT"}
    nv = next(e for e in events if e["ticker"] == "NVDA")
    assert "after the close" in nv["label"] and nv["ts"] > next(e for e in events if e["ticker"] == "AAPL")["ts"]
    calls.clear()
    assert asyncio.run(calendar.refresh_earnings(now + 60)) == events and calls == []      # cached
    kv.delete_sync(kv.key("calendar", "earnings"))


def test_trend_book_skips_name_into_earnings(monkeypatch):
    """The stocks trend book's open branch consults earnings_block; a blocked name is not opened."""
    from markets import engine
    src = open(engine.__file__).read()
    assert 'calendar.earnings_block(perp) if market == "stocks"' in src
    assert '"near_event": calendar.near_event(perp)' in src


def test_autotrader_hooks_present():
    from autotrader import engine
    src = open(engine.__file__).read()
    assert src.count("calendar.macro_blackout(") == 2          # _enter and _daily_minimum
    assert src.count('"near_event": calendar.near_event(') == 3   # _enter, the daily minimum, and every lane / mirror leg
    from experiments import burst, cascade, claude_swing, copy_trading, prop
    s2 = open(claude_swing.__file__).read()
    assert "calendar.macro_blackout()" in s2 and '"near_event": calendar.near_event(sym)' in s2
    for mod in (copy_trading, prop, cascade, burst):                   # every book stamps the tag (2026-11-10)
        assert '"near_event":' in open(mod.__file__).read(), mod.__name__


def test_page_lists_rules_and_splits_tagged_trades(monkeypatch):
    now = FOMC - 2 * 3600

    class W:
        def all_trades(self):
            return {"closed": [{"symbol": "BTCUSDT", "direction": "long", "near_event": "FOMC decision", "pnl_usd": 50.0, "risk_usd": 100.0,
                                "opened_at": "2026-10-28T10:00:00", "closed_at": "2026-10-28T20:00:00", "close_reason": "window_exit"},
                               {"symbol": "ETHUSDT", "direction": "short", "near_event": None, "pnl_usd": -100.0, "risk_usd": 100.0,
                                "opened_at": "2026-10-29T10:00:00", "closed_at": "2026-10-29T20:00:00", "close_reason": "stop_loss"},
                               {"symbol": "SOLUSDT", "direction": "short", "pnl_usd": -100.0, "risk_usd": 100.0,      # pre-tagging: ignored
                                "opened_at": "2026-10-20T10:00:00", "closed_at": "2026-10-20T20:00:00"},
                               {"symbol": "ZECUSDT", "direction": "short", "near_event": None, "pnl_usd": 20.0,      # no risk_usd: R from the stop
                                "entry_price": 100.0, "stop_loss": 110.0, "quantity": 2.0,
                                "opened_at": "2026-10-29T12:00:00", "closed_at": "2026-10-29T20:00:00", "close_reason": "expired"}],
                    "open": [{"symbol": "NVDAUSDT", "direction": "long", "near_event": "NVDA earnings (after the close)", "opened_at": "2026-10-30T10:00:00"}]}

    monkeypatch.setattr(calendar, "_wallets", lambda: {"auto": W()})

    async def no_reaction(e):
        return {"hour_pct": -0.5, "range_2h_pct": 1.7, "day_pct": -3.1}
    monkeypatch.setattr(calendar, "_btc_reaction", no_reaction)
    kv.delete_sync(kv.key("calendar", "earnings"))
    d = asyncio.run(calendar.page(now))
    nxt = next(e for e in d["events"] if e["ts"] >= now)
    assert d["blackout"] is None and nxt["kind"] == "FOMC" and "no new autotrader" in nxt["rule"]
    assert nxt["window"] == [FOMC - 3600, FOMC + 1800]
    assert all(now - calendar.PAGE_PAST_DAYS * 86400 <= e["ts"] <= now + calendar.PAGE_AHEAD_DAYS * 86400 for e in d["events"])
    assert d["events"][0]["ts"] < now          # history is in the grid too
    assert len(d["past"]) == calendar.PAST_MACRO and d["past"][0]["ts"] > d["past"][1]["ts"] and d["past"][0]["btc"]["day_pct"] == -3.1
    tr = d["trades"]
    assert tr["near"] == {"trades": 2, "closed": 1, "avg_r": 0.5, "pnl_usd": 50.0, "win_pct": 100}
    assert tr["clear"] == {"trades": 2, "closed": 2, "avg_r": 0.0, "pnl_usd": -80.0, "win_pct": 50}
    assert [r["symbol"] for r in tr["rows"]] == ["NVDAUSDT", "BTCUSDT"] and tr["rows"][0]["r"] is None
