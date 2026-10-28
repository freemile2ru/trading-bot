"""Dry-market widening: the net grows after DRY_HOURS idle and narrows once something is entered."""
import asyncio
import time

from autotrader import engine
from scanner import scheduler


def test_dry_mode_thresholds(monkeypatch):
    now = time.time()
    monkeypatch.setattr(engine, "ENABLED", True)
    engine._dry_cache.update(at=0.0)
    monkeypatch.setattr(engine, "_last_entry_ts", lambda: now - 3 * 3600)
    assert engine.dry_mode(now) == {"active": False, "idle_h": 3.0, "hours": engine.DRY_HOURS, "universe": None}
    engine._dry_cache.update(at=0.0)
    monkeypatch.setattr(engine, "_last_entry_ts", lambda: now - 20 * 3600)
    d = engine.dry_mode(now)
    assert d["active"] and d["universe"] == engine.DRY_UNIVERSE and d["idle_h"] == 20.0
    engine._dry_cache.update(at=0.0)


def test_scanner_widens_only_when_dry(monkeypatch):
    monkeypatch.setattr(engine, "dry_mode", lambda now=None: {"active": True, "universe": 30, "idle_h": 20.0, "hours": 12})
    assert asyncio.run(scheduler.universe_size()) == 30
    monkeypatch.setattr(engine, "dry_mode", lambda now=None: {"active": False, "universe": None, "idle_h": 1.0, "hours": 12})
    assert asyncio.run(scheduler.universe_size()) == scheduler.UNIVERSE_SIZE

    def boom(now=None):
        raise RuntimeError("db")
    monkeypatch.setattr(engine, "dry_mode", boom)
    assert asyncio.run(scheduler.universe_size()) == scheduler.UNIVERSE_SIZE


def test_wide_net_lowers_the_volume_floor():
    from scanner import selection
    pool = [{"pair": "AUSDT", "symbol": "A", "percent_change_1h": 1, "percent_change_24h": 2, "percent_change_7d": 3, "volume_change_24h": 1},
            {"pair": "BUSDT", "symbol": "B", "percent_change_1h": 1, "percent_change_24h": 2, "percent_change_7d": 3, "volume_change_24h": 1}]
    tickers = {"AUSDT": {"quote_volume": 30e6, "range_pct": 5.0, "price_change_pct": 2.0}, "BUSDT": {"quote_volume": 8e6, "range_pct": 4.0, "price_change_pct": 1.0}}
    assert [c["pair"] for c in selection.score_pool(pool, tickers, set())] == ["AUSDT"]
    assert {c["pair"] for c in selection.score_pool(pool, tickers, set(), selection.DRY_MIN_QUOTE_VOLUME)} == {"AUSDT", "BUSDT"}


def test_ticker_batches_skip_non_ascii_symbols(monkeypatch):
    import asyncio
    from data import binance_client
    calls = []

    async def fake_get_json(url, params=None, **kw):
        import json
        syms = json.loads(params["symbols"])
        calls.append(len(syms))
        return [{"symbol": s, "lastPrice": "1", "priceChangePercent": "1", "highPrice": "1.1", "lowPrice": "0.9", "quoteVolume": "1000", "count": 5} for s in syms]
    import data.http
    monkeypatch.setattr(data.http, "get_json", fake_get_json)
    syms = [f"C{i}USDT" for i in range(70)] + ["币安人生USDT", "BAD-USDT"]
    out = asyncio.run(binance_client.fetch_tickers_24h(syms))
    assert len(out) == 70 and calls == [60, 10]
