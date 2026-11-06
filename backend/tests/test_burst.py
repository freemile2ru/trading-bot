"""The burst book: the lab's trigger on the last closed minute, the two rules' delays, the cooldown, the late rule."""
import asyncio
import time

import numpy as np
import pandas as pd
import pytest

from experiments import books, burst
from storage import state as kv
from trading import book_switches, monitor
from trading.paper_trader import PaperWallet

SYM = "TESTUSDT"


def _frame(now: float, last5: list[float], burst_vol: float = 100.0) -> pd.DataFrame:
    """1500 quiet closed minutes (close ≈ 100, volume 10), then five minutes walking to the given closes."""
    end = int(now * 1000) // 60_000 * 60_000 - 60_000                 # the last CLOSED minute's open time
    n = 1500
    t = np.array([end - (n - 1 - i) * 60_000 for i in range(n)])
    rng = np.random.default_rng(3)
    c = 100.0 + np.cumsum(rng.normal(0, 0.02, n)); c -= c[-6] - 100.0   # the close five minutes before the burst is 100
    c[-5:] = last5
    v = np.full(n, 10.0); v[-5:] = burst_vol
    return pd.DataFrame({"o": c, "h": c + 0.05, "l": c - 0.05, "c": c, "v": v}, index=t)


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    ws = {b: PaperWallet(str(tmp_path / f"{b}.db"), label=b, initial_balance=5_000) for b in burst.BOOKS}
    monkeypatch.setattr(books, "WALLETS", {**books.WALLETS, **ws})
    frames = {}

    async def universe(now=None):
        return [SYM]

    async def bars_for(symbol, now_ms):
        return frames[symbol]

    async def price(symbol):
        return 106.0
    monkeypatch.setattr(burst, "universe", universe)
    monkeypatch.setattr(burst, "bars_for", bars_for)
    monkeypatch.setattr(monitor, "live_price", price)
    monkeypatch.setattr(burst, "_last_eval", {})
    kv.delete_sync(burst.PENDING_KEY, kv.key("burst", "cd", SYM))
    yield ws, frames
    kv.delete_sync(burst.PENDING_KEY, kv.key("burst", "cd", SYM))


def test_the_trigger_is_the_labs_filter():
    now = time.time()
    hit = burst.trigger(_frame(now, [101, 102.5, 104, 105.5, 106]))
    assert hit and hit["dir"] == 1 and hit["move_pct"] == pytest.approx(6.0, abs=0.01) and hit["sigma_x"] > 4 and hit["vol_x"] > 3
    assert burst.trigger(_frame(now, [101, 102.5, 104, 105.5, 106], burst_vol=20.0)) is None          # on ordinary volume: not a burst
    assert burst.trigger(_frame(now, [100.5, 101, 101.5, 102, 103])) is None                       # 3%: under the threshold
    assert burst.trigger(_frame(now, [99, 97.5, 96, 94.5, 94]))["dir"] == -1


def test_an_up_burst_is_ridden_fifteen_minutes_later_by_the_up_book_only(book):
    ws, frames = book
    now = time.time()
    frames[SYM] = _frame(now, [101, 102.5, 104, 105.5, 106])
    assert asyncio.run(burst.tick(now)) == []                                         # queued, not traded
    q = burst.pending()
    assert [x["book"] for x in q] == ["burst_up"] and q[0]["symbol"] == SYM
    closed_at = int(frames[SYM].index[-1]) / 1000 + 60
    assert q[0]["fire_at"] == pytest.approx(closed_at + 15 * 60)
    assert asyncio.run(burst.tick(now + 60)) == [] and len(burst.pending()) == 1        # the same bar again: not queued twice
    assert asyncio.run(burst.tick(q[0]["fire_at"] - 30)) == []
    opened = asyncio.run(burst.tick(q[0]["fire_at"] + 20))
    assert len(opened) == 1 and opened[0]["direction"] == "long" and opened[0]["symbol"] == SYM and opened[0]["strategy"] == "burst_up"
    t = ws["burst_up"].get_portfolio()["open_trades"][0]
    assert t["notional_usd"] == pytest.approx(5_000 * burst.LEG_PCT / 100, rel=0.01)
    assert t["stop_loss"] == pytest.approx(106 * (1 - burst.STOP_PCT / 100)) and t["expires_at"] == pytest.approx(q[0]["fire_at"] + 20 + burst.HOLD_H * 3600)
    assert t["burst_move_pct"] == pytest.approx(6.0, abs=0.01) and t["entry_delay_min"] == 15
    assert ws["burst_dip"].get_portfolio()["open_trades"] == [] and burst.pending() == []


def test_a_down_burst_is_bought_an_hour_later_by_the_dip_book(book):
    ws, frames = book
    now = time.time()
    frames[SYM] = _frame(now, [99, 97.5, 96, 94.5, 94])
    asyncio.run(burst.tick(now))
    q = burst.pending()
    assert [x["book"] for x in q] == ["burst_dip"] and q[0]["dir"] == -1
    closed_at = int(frames[SYM].index[-1]) / 1000 + 60
    assert q[0]["fire_at"] == pytest.approx(closed_at + 3600)
    opened = asyncio.run(burst.tick(q[0]["fire_at"] + 5))
    assert [(t["strategy"], t["direction"]) for t in opened] == [("burst_dip", "long")]
    assert ws["burst_up"].get_portfolio()["open_trades"] == []


def test_a_fire_missed_by_more_than_ten_minutes_is_dropped_and_a_coin_fires_once_an_hour(book):
    ws, frames = book
    now = time.time()
    frames[SYM] = _frame(now, [101, 102.5, 104, 105.5, 106])
    asyncio.run(burst.tick(now))
    fire = burst.pending()[0]["fire_at"]
    assert asyncio.run(burst.tick(fire + burst.LATE_S + 1)) == [] and burst.pending() == []      # the server was down: no late entry
    assert ws["burst_up"].get_portfolio()["open_trades"] == []
    frames[SYM] = _frame(now + 120, [101, 102.5, 104, 105.5, 106])                                # a fresh bar two minutes on, still bursting
    assert asyncio.run(burst.tick(now + 120)) == [] and burst.pending() == []                      # inside the hour's cooldown: not queued
