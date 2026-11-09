"""The copy book's second look: the tape concerns, the verdict, and what veto / record mode do to a copy."""
import asyncio
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_copy_trading import NOW, _feed_book, _row            # noqa: E402

from experiments import copy_second_look as sl                  # noqa: E402
from experiments import copy_trading as ct                      # noqa: E402


def test_concerns_name_what_was_plainly_wrong_and_nothing_else():
    cap_short = {"r24h_pct": 26.8, "vs_vwap24_pct": 4.6, "pos24_pct": 88, "btc_tilt": "up", "btc_vs_vwap_pct": 0.4}       # prod 2026-11-05: −$168
    c = sl.concerns(cap_short, "short")
    assert any(x.startswith("parabolic") for x in c) and any(x.startswith("BTC tape against") for x in c) and not any(x.startswith("chasing") for x in c)
    eth_long = {"r24h_pct": 7.3, "vs_vwap24_pct": 2.8, "pos24_pct": 91, "btc_tilt": "up", "btc_vs_vwap_pct": 2.2}            # prod 2026-11-05: −$14
    assert [x.split(":")[0] for x in sl.concerns(eth_long, "long")] == ["extreme"]
    zec_fade = {"r24h_pct": 7.1, "vs_vwap24_pct": 6.4, "pos24_pct": 84, "btc_tilt": "down", "btc_vs_vwap_pct": -0.9}        # prod 2026-11-06: +$91 — a fade, not a chase
    assert sl.concerns(zec_fade, "short") == []
    chase = {"r24h_pct": 4.0, "vs_vwap24_pct": -3.5, "pos24_pct": 40, "btc_tilt": None, "btc_vs_vwap_pct": None}
    assert [x.split(":")[0] for x in sl.concerns(chase, "short")] == ["chasing"]
    assert sl.concerns({"r24h_pct": None, "vs_vwap24_pct": None, "pos24_pct": None, "btc_tilt": None}, "long") == []


def test_verdict_rules():
    assert sl.verdict([], "we are short ZECUSDT on auto (reversal lane)", None)[0] == "disagree"
    assert sl.verdict(["extreme: buying at 95%"], None, {"view": "disagree", "reason": "exhausted"})[0] == "disagree"
    assert sl.verdict([], None, {"view": "disagree", "reason": "hunch"})[0] == "doubt"                  # Claude alone never vetoes
    assert sl.verdict(["chasing: +3.4%"], None, None)[0] == "doubt"
    assert sl.verdict([], None, {"view": "agree", "reason": "clean"}) == ("agree", "tape clear · Claude agrees — clean")
    assert sl.verdict([], None, None)[0] == "agree"


def _reads(monkeypatch, view, x):
    async def look(symbol, side, leader=None, now=None):
        return {"view": view, "why": f"test {view}", "x": x, "concerns": [], "claude": None, "features": {"r24h_pct": 1.0}}
    monkeypatch.setattr(sl, "look", look)


def test_a_disagree_vetoes_the_copy_as_a_judged_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "MODE", "veto")
    rows = [_row("p1", "ETHUSDT", 5.0)]
    w = _feed_book(tmp_path, monkeypatch, rows, {"ETHUSDT": 100.0})
    _reads(monkeypatch, "disagree", 1.0)
    assert asyncio.run(ct.mirror(NOW))["opened"] == []
    sk = ct.skipped()
    assert len(sk) == 1 and sk[0]["reason"] == "second look: test disagree" and sk[0]["mark"] == 100.0 and sk[0]["second_look"]["view"] == "disagree"
    assert w.get_portfolio()["open_trades"] == []


def test_a_doubt_halves_the_leg_and_the_read_rides_on_the_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "MODE", "veto")
    rows = [_row("p1", "ETHUSDT", 5.0)]
    w = _feed_book(tmp_path, monkeypatch, rows, {"ETHUSDT": 100.0})
    _reads(monkeypatch, "doubt", 0.5)
    assert len(asyncio.run(ct.mirror(NOW))["opened"]) == 1
    t = w.get_portfolio()["open_trades"][0]
    assert t["second_look_view"] == "doubt" and t["second_look_x"] == 0.5 and t["size_x"] == pytest.approx(0.5) and t["second_look_tape"] == {"r24h_pct": 1.0}
    assert t["notional_usd"] == pytest.approx(ct.leg_notional(10_000, t["stop_pct"], "crypto") * 0.5, rel=0.01)


def test_record_mode_keeps_the_read_and_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "MODE", "record")
    rows = [_row("p1", "ETHUSDT", 5.0)]
    w = _feed_book(tmp_path, monkeypatch, rows, {"ETHUSDT": 100.0})
    _reads(monkeypatch, "disagree", 1.0)                                 # look() itself returns x=1.0 outside veto mode
    assert len(asyncio.run(ct.mirror(NOW))["opened"]) == 1
    t = w.get_portfolio()["open_trades"][0]
    assert t["second_look_view"] == "disagree" and t["size_x"] == pytest.approx(1.0) and ct.skipped() == []


def test_audit_groups_closed_copies_by_view_and_counts_vetoes():
    closed = [{"second_look_view": "agree", "pnl_usd": 90.0}, {"second_look_view": "agree", "pnl_usd": -20.0}, {"second_look_view": "doubt", "pnl_usd": -168.0}, {"pnl_usd": 5.0}]
    skipped = [{"reason": "second look: opposite position", "outcome_pct": -2.5}, {"reason": "too late", "outcome_pct": 1.0}, {"reason": "second look: x"}]
    a = sl.audit(closed, skipped)
    assert a["by_view"]["agree"] == {"n": 2, "pnl": 70.0, "wins": 1} and a["by_view"]["doubt"]["n"] == 1 and a["by_view"]["disagree"]["n"] == 0
    assert a["vetoed"] == {"n": 2, "judged": 1, "avg_outcome_pct": -2.5, "would_have_won_pct": 0.0}


def test_a_feed_failure_never_silences_the_book(monkeypatch):
    monkeypatch.setattr(sl, "MODE", "veto")
    async def boom(*a, **k):
        raise RuntimeError("no klines")
    monkeypatch.setattr(sl, "features", boom)
    out = asyncio.run(sl.look("ETHUSDT", "long", {}, NOW))
    assert out["view"] == "agree" and out["x"] == 1.0 and "unavailable" in out["why"]


def test_features_read_perpetual_candles_so_stock_perps_get_a_second_look(monkeypatch):
    import asyncio
    from data import binance_client
    calls = []

    async def perp_bars(symbol, interval="1h", limit=48):
        calls.append((symbol, interval, limit))
        return [{"open_time": i * 3_600_000, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0 + i * 0.1, "volume": 10.0, "close_time": i * 3_600_000 + 3_599_999}
                for i in range(48)]

    async def spot_bars(*a, **k):
        raise AssertionError("the spot endpoint has no stock perps")

    async def btc(now=None):
        return {"tilt": "flat", "pct": 0.0}
    monkeypatch.setattr(binance_client, "fetch_futures_klines", perp_bars)
    monkeypatch.setattr(binance_client, "fetch_ohlcv", spot_bars)
    from trading import tape
    monkeypatch.setattr(tape, "btc_tape", btc)
    f = asyncio.run(sl.features("SKHYNIXUSDT", "short", now=48 * 3600))
    assert calls == [("SKHYNIXUSDT", "1h", 48)] and f["px"] > 100 and f["vs_vwap24_pct"] is not None and f["pos24_pct"] is not None
