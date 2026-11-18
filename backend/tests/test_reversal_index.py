"""The reversal index leg (2026-11-17): one ALTIDX leg per reversal slot at a coin leg's risk, closed with the slot, paired in R."""
import asyncio
import time

import pytest

from experiments import books, reversal_index as ri
from storage import state as kv
from trading import book_switches, monitor
from trading.paper_trader import PaperWallet

COMPS = ("AUSDT", "BUSDT", "CUSDT", "DUSDT")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    w = PaperWallet(str(tmp_path / "idx.db"), label=ri.BOOK, initial_balance=10_000)
    auto = PaperWallet(str(tmp_path / "auto.db"), label="auto", initial_balance=50_000)
    monkeypatch.setattr(books, "WALLETS", {**books.WALLETS, ri.BOOK: w})
    monkeypatch.setattr(ri, "_auto_wallet", lambda: auto)
    monkeypatch.setattr(ri, "components", lambda: COMPS)
    monkeypatch.setattr(ri, "MIN_COMPONENTS", 3)
    prices = {s: 10.0 for s in COMPS}

    async def live_price(sym):
        return prices.get(sym)
    monkeypatch.setattr(monitor, "live_price", live_price)
    kv.delete_sync(ri._k("base"), ri._k("handled"))
    yield w, auto, prices
    kv.delete_sync(ri._k("base"), ri._k("handled"))


def _coin_leg(auto, sym, bucket, now, direction="short"):
    return auto.open_trade(symbol=sym, direction=direction, entry_price=100.0, stop_loss=106.0, take_profit_1=50.0, take_profit_2=40.0,
                           notional_usd=4_000.0, timeframe="1h", leverage=3,
                           extra={"strategy": "auto", "lane": "reversal", "reversal_id": f"{bucket}:{sym}", "exit_policy": "manual", "shadows": False,
                                  "claude_verdict": "skipped", "sizing": {"notional": 4_000.0, "stop_pct": 6.0}})


def test_the_level_is_rebased_to_100_and_follows_the_components(setup):
    w, auto, prices = setup
    lvl, parts = asyncio.run(ri.level())
    assert lvl == 100.0 and parts == {"priced": 4, "of": 4}
    prices["AUSDT"] = 11.0; prices["BUSDT"] = 9.0; prices["CUSDT"] = None                                 # +10%, −10%, one unpriced
    lvl, parts = asyncio.run(ri.level())
    assert lvl == pytest.approx(100.0) and parts["priced"] == 3


def test_one_index_leg_per_slot_at_the_coin_legs_risk_closed_with_the_slot(setup):
    w, auto, prices = setup
    now = time.time()
    a = _coin_leg(auto, "ZECUSDT", "7", now); b = _coin_leg(auto, "TAOUSDT", "7", now)
    out = asyncio.run(ri.tick(now))
    assert len(out["opened"]) == 1 and out["opened"][0]["direction"] == "short" and out["level"] == 100.0
    t = w.get_portfolio()["open_trades"][0]
    assert t["symbol"] == ri.SYMBOL and t["episode"] == "7" and sorted(t["coin_legs"]) == ["TAOUSDT", "ZECUSDT"]
    assert t["coin_risk_usd"] == pytest.approx(a["risk_usd"], rel=0.01) and t["notional_usd"] == pytest.approx(a["risk_usd"] / 0.04, rel=0.01)
    assert t["stop_loss"] == pytest.approx(104.0, rel=0.001)
    assert asyncio.run(ri.tick(now + 60))["opened"] == []                                                  # the slot is taken once
    for s in COMPS:
        prices[s] = 9.8                                                                                    # the index −2%: marked, open
    asyncio.run(ri.tick(now + 120))
    assert w.get_portfolio()["open_trades"][0]["current_price"] == pytest.approx(98.0)
    auto.close_trade_manually(a["id"], 99.0, "hold_check"); auto.close_trade_manually(b["id"], 98.0, "hold_check")
    out = asyncio.run(ri.tick(now + 180))
    assert out["closed"] == [{"episode": "7", "reason": "episode_over", "level": 98.0}] and w.get_portfolio()["open_trades"] == []
    aud = ri.audit()
    assert aud["n"] == 1 and aud["verdict"] == "collecting" and aud["recent"][0]["coins"] == 2
    assert aud["recent"][0]["index_r"] == pytest.approx(0.5, abs=0.05)                                     # −2% on a 4% stop ≈ +0.5R
    assert aud["recent"][0]["coin_r"] == pytest.approx(0.25, abs=0.05)                                     # −1% and −2% on a 6% stop


def test_a_stale_slot_is_not_taken_and_the_stop_closes_a_leg(setup):
    w, auto, prices = setup
    now = time.time()
    _coin_leg(auto, "ZECUSDT", "8", now)
    assert asyncio.run(ri.tick(now + ri.FRESH_S + 60))["opened"] == []                                     # first seen after a restart: not taken
    setup_monkeypatch = pytest.MonkeyPatch(); setup_monkeypatch.setattr(ri, "FRESH_S", 10 ** 6)            # the wallet stamps opened_at with the clock, not our `now`
    _coin_leg(auto, "SUIUSDT", "9", now)
    assert len(asyncio.run(ri.tick(now + 2_000))["opened"]) == 1
    setup_monkeypatch.undo()
    for s in COMPS:
        prices[s] = 10.5                                                                                   # +5%: through the 4% stop
    asyncio.run(ri.tick(now + 2_060))
    assert w.get_portfolio()["open_trades"] == [] and w.all_trades()["closed"][0]["close_reason"] == "stop_loss"


def test_the_index_has_candles_and_a_ticker_built_from_its_components(monkeypatch):
    """ALTIDX on the Trade tab and the review chart (2026-11-18): each bar is 100 × the mean of the components' bars over
    their base; a bar with too few components is dropped; the ticker's change is over the window's first open."""
    import asyncio
    from data import derivatives_client
    from experiments import reversal_index as ri
    from storage import state as kv
    comps = ri.components()
    kv.set_sync(ri._k("base"), {s: 10.0 for s in comps})               # every component based at 10

    async def klines(symbol, interval, start_ms, end_ms):
        if symbol == comps[0]:
            return [[0, 10.0, 11.0, 9.0, 10.5, 1.0, 100.0]]            # one component lacks the second bar
        return [[0, 10.0, 11.0, 9.0, 10.5, 1.0, 100.0], [60_000, 10.5, 12.0, 10.0, 11.0, 1.0, 50.0]]
    monkeypatch.setattr(derivatives_client, "fetch_klines_range", klines)
    bars = asyncio.run(ri.candles("1m", 0, 120_000))
    assert [b["time"] for b in bars] == [0, 60]
    assert bars[0] == {"time": 0, "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "volume": 100.0 * len(comps)}
    assert bars[1]["close"] == 110.0 and bars[1]["volume"] == 50.0 * (len(comps) - 1)

    async def few(symbol, interval, start_ms, end_ms):
        return [[0, 10.0, 11.0, 9.0, 10.5, 1.0, 100.0]] if symbol in comps[:5] else []
    monkeypatch.setattr(derivatives_client, "fetch_klines_range", few)
    assert asyncio.run(ri.candles("1m", 0, 120_000)) == []              # 5 of 20 priced: no bar

    async def lvl(now=None):
        return 112.0, {}
    monkeypatch.setattr(ri, "level", lvl)
    monkeypatch.setattr(derivatives_client, "fetch_klines_range", klines)
    t = asyncio.run(ri.ticker())
    assert t["last"] == 112.0 and t["change_pct"] == pytest.approx(12.0) and t["high"] == 120.0 and t["low"] == 90.0 and t["mark"] == 112.0
