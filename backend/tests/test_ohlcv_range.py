"""The review chart's candles: a window of perp klines around a trade, the interval picked from the span (2026-11-18)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import main
    from data import derivatives_client
    calls = []

    async def klines(symbol, interval, start_ms, end_ms):
        calls.append((symbol, interval, start_ms, end_ms))
        if symbol == "NOPEUSDT":
            raise RuntimeError("Client error '400 Bad Request' for url")
        step = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}[interval] * 1000
        return [[t, 1.0, 1.1, 0.9, 1.05, 10.0, 10.5] for t in range(start_ms - start_ms % step, end_ms, step)]
    monkeypatch.setattr(derivatives_client, "fetch_klines_range", klines)
    return TestClient(main.app), calls


def test_a_three_hour_trade_gets_one_minute_candles_padded_either_side(client):
    c, calls = client
    start, end = 1_794_937_516, 1_794_949_406                           # the AMD prop trade: 3h18m
    r = c.get(f"/api/ohlcv/AMDUSDT/range?start={start}&end={end}")
    assert r.status_code == 200
    d = r.json()
    assert d["interval"] == "1m" and d["interval_s"] == 60 and d["symbol"] == "AMDUSDT"
    sym, interval, s_ms, e_ms = calls[0]
    assert sym == "AMDUSDT" and s_ms == int((start - (end - start) / 2) * 1000) and e_ms == int((end + (end - start) / 2) * 1000)
    assert 380 <= len(d["candles"]) <= 400 and d["candles"][0]["time"] <= start - 60 and set(d["candles"][0]) == {"time", "open", "high", "low", "close", "volume"}


def test_a_four_day_hold_gets_thirty_minute_candles_and_a_short_hold_at_least_twelve_bars(client):
    c, calls = client
    assert c.get("/api/ohlcv/ZECUSDT/range?start=1794000000&end=1794345600").json()["interval"] == "30m"
    r = c.get("/api/ohlcv/ZECUSDT/range?start=1794000000&end=1794000300").json()                  # 5 minutes held
    assert r["interval"] == "1m" and calls[-1][2] == (1794000000 - 12 * 60) * 1000


def test_bad_windows_and_symbols_without_candles(client):
    c, _ = client
    assert c.get("/api/ohlcv/ZECUSDT/range?start=10&end=5").status_code == 400
    assert c.get("/api/ohlcv/ZECUSDT/range?start=1&end=2&interval=7m").status_code == 400
    r = c.get("/api/ohlcv/NOPEUSDT/range?start=1794000000&end=1794345600")
    assert r.status_code == 404 and "no candles" in r.json()["detail"]
