"""Control data: a closed trade replayed on its bars under plain controls, the book report, and the late-mirror guard."""
import asyncio
import time

import pytest

from autotrader import engine
from experiments import control
from storage import state as kv


def _path(start: float, closes: list[float], step_s: int = 300, spread: float = 0.2) -> list[dict]:
    return [{"t": start + i * step_s, "o": c, "h": c + spread, "l": c - spread, "c": c} for i, c in enumerate(closes)]


def _trade(direction="long", entry=100.0, stop=92.0, qty=10.0, opened=1_000_000.0, closed=1_000_000.0 + 3600, pnl=5.0, **more):
    return {"id": "t1", "symbol": "ZECUSDT", "direction": direction, "entry_price": entry, "stop_loss": stop, "quantity": qty,
            "opened_ts": opened, "closed_ts": closed, "pnl_usd": pnl, "close_reason": "expired", "notional_usd": entry * qty, **more}


def test_replay_holds_with_the_stop_and_measures_the_move_after_the_exit():
    o = 1_000_000.0
    # 48h of bars: the price drifts from 100 to 110 over the first 8 hours, then sits at 110
    closes = [100 + min(i, 96) * (10 / 96) for i in range(48 * 12)]
    rec = control.replay(_trade(closed=o + 3600, pnl=5.0), _path(o, closes))
    fee = 1000 * control.FEE_BPS / 1e4
    assert rec["held"]["1h"] == pytest.approx((closes[11] - 100) * 10 - fee, abs=0.01)
    assert rec["held"]["8h"] == pytest.approx((closes[95] - 100) * 10 - fee, abs=0.01) and rec["held"]["48h"] == pytest.approx(100.0 - fee, abs=0.01)
    assert rec["stopped_at_h"] is None and rec["best_pct"]["48h"] == pytest.approx(10.2, abs=0.05) and rec["worst_pct"]["1h"] < 0
    assert rec["after_exit_pct"]["4h"] == pytest.approx((closes[5 * 12 - 1] - closes[12]) / 100 * 100, abs=0.2)   # it kept going up after we left
    assert rec["hold_h"] == 1.0 and rec["actual"] == 5.0


def test_replay_applies_the_stop_pessimistically_and_carries_it_forward():
    o = 1_000_000.0
    closes = [100.0] * 12 + [91.0] * 12 + [120.0] * (46 * 12)          # a dip through the stop in hour 2, then a moonshot we never see
    rec = control.replay(_trade(stop=92.0), _path(o, closes))
    assert rec["stopped_at_h"] == pytest.approx(1.0, abs=0.1)
    assert rec["held"]["1h"] == pytest.approx(-1000 * control.FEE_BPS / 1e4, abs=0.01)                 # flat at the hour
    assert rec["held"]["2h"] == pytest.approx((92 - 100) * 10 - 1000 * control.FEE_BPS / 1e4, abs=0.01)
    assert rec["held"]["48h"] == rec["held"]["2h"]                                                      # stopped: the later windows keep the stop's P&L
    short = control.replay(_trade(direction="short", stop=108.0, closed=o + 7200, pnl=20.0), _path(o, closes))
    assert short["stopped_at_h"] == pytest.approx(24 * 2 / 24, abs=0.1) and short["held"]["48h"] == pytest.approx((100 - 108) * 10 - 0.5, abs=0.01)


def test_best_and_worst_take_the_right_extremes_for_a_short():
    o = 1_000_000.0
    closes = [100.0, 103.0, 97.0, 100.0] + [100.0] * (48 * 12 - 4)      # a spike up to 103.2, then a dip to 96.8
    rec = control.replay(_trade(direction="short", stop=110.0, closed=o + 3600, pnl=0.0), _path(o, closes))
    assert rec["best_pct"]["1h"] == pytest.approx(3.2, abs=0.01)         # the lowest low is the short's best
    assert rec["worst_pct"]["1h"] == pytest.approx(-3.2, abs=0.01)       # the highest high its worst
    assert rec["v"] == control.REPLAY_V


def test_a_record_from_an_older_replay_is_redone(monkeypatch):
    kv.delete_sync(control._k("index", "cascade"))
    t = _trade(opened=1_000_000.0, closed=1_003_600.0)
    kv.set_sync(control._k(t["id"]), {"trade_id": t["id"], "v": control.REPLAY_V - 1, "held": {}, "best_pct": {}, "worst_pct": {}, "after_exit_pct": {}, "actual": 0.0, "stopped_at_h": None, "hold_h": 1.0, "opened_ts": t["opened_ts"], "symbol": "ZECUSDT", "direction": "long", "closed_ts": t["closed_ts"], "close_reason": "expired", "notional": 1000.0, "lane": None, "hold_rule": None})
    kv.set_sync(control._k("index", "cascade"), [t["id"]])
    monkeypatch.setattr(control, "closed_trades", lambda book: [{**t, "opened_ts": t["opened_ts"], "closed_ts": t["closed_ts"]}])
    assert control.report("cascade")["pending"] == 1
    seen = []

    async def fake_record(book, trade):
        seen.append(trade["id"])
        return {"trade_id": trade["id"]}
    monkeypatch.setattr(control, "record", fake_record)
    assert asyncio.run(control.run("cascade"))["recorded"] == 1 and seen == [t["id"]]


def test_the_report_sums_actual_against_the_controls_and_flags_early_exits(monkeypatch):
    kv.delete_sync(control._k("index", "cascade"))
    o = 1_000_000.0
    closes = [100 + i * 0.01 for i in range(48 * 12)]
    for i, (hold_h, pnl) in enumerate(((0.3, -5.0), (4.0, 2.0), (4.1, 1.0))):
        t = _trade(opened=o + i * 10_000, closed=o + i * 10_000 + hold_h * 3600, pnl=pnl)
        t["id"] = f"t{i}"
        rec = {**control.replay(t, _path(o + i * 10_000, closes)), "book": "cascade", "trade_id": t["id"], "symbol": "ZECUSDT", "direction": "long",
               "opened_ts": t["opened_ts"], "closed_ts": t["closed_ts"], "close_reason": "expired", "notional": 1000.0, "lane": None, "hold_rule": None}
        kv.set_sync(control._k(t["id"]), rec)
        kv.set_sync(control._k("index", "cascade"), (kv.get_sync(control._k("index", "cascade")) or []) + [t["id"]])
    monkeypatch.setattr(control, "closed_trades", lambda book: [])
    rep = control.report("cascade")
    assert rep["all"]["n"] == 3 and rep["all"]["actual"] == -2.0 and rep["early"]["n"] == 1
    assert rep["all"]["held"]["4h"] > rep["all"]["actual"]                                             # holding every one for 4h beat what we did
    assert rep["early"]["after_exit_avg_pct"]["4h"] > 0 and rep["by_reason"]["expired"]["n"] == 3
    assert rep["horizon_h"] == 4.0 and len(rep["rows"]) == 3


def test_a_book_position_past_half_its_life_or_under_an_hour_is_not_mirrored():
    now = 1_000_000.0
    fresh = {"opened_ts": now - 600, "expires_at": now - 600 + 4 * 3600}
    assert engine.mirror_too_late(fresh, now) is None
    spent = {"opened_ts": now - 3 * 3600, "expires_at": now + 3600}
    assert "75% through" in engine.mirror_too_late(spent, now)
    ending = {"opened_ts": now - 600, "expires_at": now + 1200}
    assert "20 min left" in engine.mirror_too_late(ending, now)
    assert engine.mirror_too_late({"opened_ts": now - 10 * 3600}, now) is None                         # no expiry: never late


def test_the_mirror_loop_skips_and_remembers_a_late_position(tmp_path, monkeypatch):
    from experiments import books
    from trading.paper_trader import PaperWallet
    src = PaperWallet(str(tmp_path / "src.db"), label="cascade", initial_balance=500)
    active = PaperWallet(str(tmp_path / "active.db"), label="auto", initial_balance=5_000)
    monkeypatch.setitem(books.WALLETS, "cascade", src)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(engine, "_active", lambda: active)
    monkeypatch.setattr(engine, "_wallets", lambda: [active])

    async def not_paused():
        return False

    async def no_regime():
        return None

    async def price(sym):
        return 100.0
    monkeypatch.setattr(engine, "is_paused", not_paused)
    monkeypatch.setattr(engine.regime, "current", no_regime)
    monkeypatch.setattr(engine.monitor, "live_price", price)
    asyncio.run(kv.delete(kv.key("auto", "cascade_mirror")))
    asyncio.run(kv.set(kv.key("auto", "cascade_since"), {"at": 1.0}, 3600))
    t = src.open_trade(symbol="SOLUSDT", direction="long", entry_price=100.0, stop_loss=92.0, take_profit_1=150.0, take_profit_2=190.0,
                       notional_usd=50.0, timeframe="4h", leverage=3, extra={"exit_policy": "manual", "shadows": False, "strategy": "cascade"})
    src.annotate(t["id"], opened_ts=time.time() - 3.5 * 3600, expires_at=time.time() + 0.5 * 3600)        # 30 minutes to the bell
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    assert active.get_portfolio()["open_trades"] == []
    m = asyncio.run(kv.hgetall(kv.key("auto", "cascade_mirror")))
    assert m[t["id"]]["late"] and m[t["id"]]["trade"] is None
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))                                   # not re-offered
    assert active.get_portfolio()["open_trades"] == []
    src.close_trade_manually(t["id"], 101.0, "expired")
    asyncio.run(engine._mirror_wallet_book("cascade", "cascade", True))
    assert t["id"] not in asyncio.run(kv.hgetall(kv.key("auto", "cascade_mirror")))                        # forgotten once the book closes it
