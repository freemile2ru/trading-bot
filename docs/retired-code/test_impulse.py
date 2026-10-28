import asyncio
import random

import pytest

from scalper import impulse as I
from scalper.market import MarketState, SecondBar

T0 = 1_790_000_000


def _market(spike_bps: float = 0.0, spike_s: int = 60, vol_mult: float = 5.0, n: int = 3600) -> MarketState:
    """An hour of quiet 1s bars (~1 bps noise), then an optional move over the last `spike_s` seconds."""
    rnd = random.Random(7)
    m = MarketState("BTCUSDT")
    px = 100.0
    for i in range(n):
        spiking = spike_bps and i >= n - spike_s
        px *= 1 + (spike_bps / spike_s if spiking else rnd.gauss(0, 1)) / 1e4
        qty = 10.0 * (vol_mult if spiking else 1.0)
        m.bars.append(SecondBar(T0 + i, px, px, px, px, pv=px * qty, vol=qty))
    m.bids, m.asks = [(px * 0.9999, 50.0)], [(px * 1.0001, 50.0)]
    return m


def test_quiet_market_is_not_an_event():
    m = _market()
    assert I.measure(m, I.baseline(m))["hit"] is None


def test_extreme_fast_move_on_heavy_volume_is_an_event():
    m = _market(spike_bps=90)
    ms = I.measure(m, I.baseline(m))
    assert ms["hit"] == 60 and ms[60]["move_bps"] > 80 and ms[60]["z"] > 6 and ms[60]["vol_mult"] > 3


def test_fast_move_without_volume_is_not_extreme():
    m = _market(spike_bps=90, vol_mult=1.0)
    assert I.measure(m, I.baseline(m))["hit"] is None


def test_slow_grind_counts_past_150bps_in_5_minutes():
    m = _market(spike_bps=170, spike_s=300, vol_mult=1.0)
    assert I.measure(m, I.baseline(m))["hit"] == 300


def test_stop_scales_with_the_move_within_bounds():
    assert I.stop_bps(40) == I.SL_MIN_BPS
    assert I.stop_bps(-200) == 100
    assert I.stop_bps(1000) == I.SL_MAX_BPS


def test_market_entry_pays_taker_costs_and_the_stop_trails():
    b = I.ImpulseBook(10_000)
    b.depth["BTCUSDT"] = ([(99.99, 1000.0)], [(100.01, 1000.0)])
    pos = b.open_market("BTCUSDT", "long", 100.01, 80, T0, 2.0, None, {})
    assert pos.entry_price >= 100.01 and pos.fees > 0 and pos.tp is None
    risk = abs(pos.entry_price - pos.sl) * pos.qty
    assert risk == pytest.approx(10_000 * I.RISK_PCT / 100, rel=0.2)
    b.on_trade("BTCUSDT", 102.0, 1.0, False, T0 + 10)             # runs 200 bps our way
    assert pos.sl == pytest.approx(102.0 * (1 - 80 / 1e4), rel=1e-6)
    b.depth["BTCUSDT"] = ([(100.99, 1000.0)], [(101.01, 1000.0)])
    ev = b.on_trade("BTCUSDT", 101.0, 1.0, True, T0 + 20)          # gives back → trailed stop hit
    trade = next(e for e in ev if e["type"] == "position_closed")["trade"]
    assert trade["exit_reason"] == "trailing_stop" and trade["net_pnl"] > 0 and trade["entry_maker"] == 0


def test_max_positions():
    b = I.ImpulseBook(10_000)
    for sym in ("AUSDT", "BUSDT", "CUSDT")[:I.MAX_POSITIONS]:
        b.open_market(sym, "long", 100.0, 80, T0, 1.0, None, {})
    assert b.can_enter("DUSDT", T0) == (False, f"max {I.MAX_POSITIONS} positions")


def test_engine_trades_an_event_once_and_labels_it(tmp_path, monkeypatch):
    from scalper.store import Store
    monkeypatch.setattr(I.C, "SYMBOLS", ["BTCUSDT"])
    e = I.ImpulseEngine(Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"), I.HORIZONS))
    e.markets["BTCUSDT"] = _market(spike_bps=90)
    monkeypatch.setattr(e, "feed_age", lambda now: 0.0)
    monkeypatch.setattr(I.MarketState, "history_seconds", lambda self: 3600)
    monkeypatch.setattr(I.MarketState, "features", lambda self: {"mid": self.bars[-1].c, "spread_bps": 2.0,
                                                                "median_spread_bps": 1.5})
    now = T0 + 3600
    asyncio.run(e._evaluate(now))
    pos = e.book.positions["BTCUSDT"]
    assert pos.side == "long" and pos.features["window_s"] == 60           # ride = with the move
    assert sorted(x[-1] for x in e.labels) == sorted(I.HORIZONS)
    e.book.positions.clear()
    asyncio.run(e._evaluate(now + 5))                                       # same burst: not a second event
    assert not e.book.positions and e.last_decision["BTCUSDT"]["status"] == "same burst — already counted"
    e.store.close()
    rows = Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"), I.HORIZONS).query(
        "trades", "SELECT decision, window_s, move_bps FROM signals")
    assert len(rows) == 1 and rows[0]["decision"] == "entered" and rows[0]["move_bps"] > 80


def test_blown_out_spread_blocks_the_entry_but_logs_the_event(tmp_path, monkeypatch):
    from scalper.store import Store
    monkeypatch.setattr(I.C, "SYMBOLS", ["BTCUSDT"])
    e = I.ImpulseEngine(Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"), I.HORIZONS))
    e.markets["BTCUSDT"] = _market(spike_bps=-90)
    monkeypatch.setattr(e, "feed_age", lambda now: 0.0)
    monkeypatch.setattr(I.MarketState, "history_seconds", lambda self: 3600)
    monkeypatch.setattr(I.MarketState, "features", lambda self: {"mid": self.bars[-1].c, "spread_bps": 20.0,
                                                                "median_spread_bps": 1.5})
    asyncio.run(e._evaluate(T0 + 3600))
    assert not e.book.positions and "spread" in e.last_decision["BTCUSDT"]["status"]
    assert len(e.labels) == len(I.HORIZONS)                                  # still researched
    e.store.close()


def _klines(n=61, step_bps=1.0, quote=60_000.0):
    rnd = random.Random(3)
    px, out = 100.0, []
    for i in range(n):
        px *= 1 + rnd.gauss(0, step_bps) / 1e4
        out.append([i * 60_000, px, px, px, px, 0, i * 60_000 + 59_999, quote])
    return out


def test_seed_baseline_from_minute_candles():
    base = I.seed_baseline(_klines(step_bps=7.75))
    # σ₁ₘ ≈ 7.75 bps → σ₁ₛ ≈ 0.94 × 7.75 / √60 ≈ 0.94 bps; 60k quote per minute → 1k per second
    assert base["seeded"] and base["sd1_bps"] == pytest.approx(0.94, rel=0.3)
    assert base["value_per_s"] == pytest.approx(1000.0)
    assert I.seed_baseline(_klines(n=10)) is None


def test_seeded_engine_detects_after_five_minutes_not_thirty(tmp_path, monkeypatch):
    from scalper.store import Store
    monkeypatch.setattr(I.C, "SYMBOLS", ["BTCUSDT"])
    e = I.ImpulseEngine(Store(str(tmp_path / "t.db"), str(tmp_path / "m.db"), I.HORIZONS))
    e.markets["BTCUSDT"] = _market(spike_bps=90, n=400)                   # under 7 minutes of 1s history
    monkeypatch.setattr(e, "feed_age", lambda now: 0.0)
    monkeypatch.setattr(I.MarketState, "features", lambda self: {"mid": self.bars[-1].c, "spread_bps": 2.0,
                                                                "median_spread_bps": 1.5})
    now = T0 + 400
    asyncio.run(e._evaluate(now))
    assert e.last_decision["BTCUSDT"]["status"] == "warming up"            # no seed → full warm-up
    e.seeds["BTCUSDT"] = {"sd1_bps": 1.0, "value_per_s": 1000.0, "seeded": True}
    asyncio.run(e._evaluate(now))
    pos = e.book.positions["BTCUSDT"]
    assert pos.features["baseline"] == "seeded"
    e.store.close()
