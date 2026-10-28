import asyncio

from trading import regime


def _bars(ranges):
    """Hourly bars whose 24h high-low range is ranges[i] (as a fraction) — flat closes at 100."""
    out = []
    for r in ranges:
        out.append({"open": 100.0, "high": 100.0 * (1 + r / 2), "low": 100.0 * (1 - r / 2), "close": 100.0})
    return out


def test_activity_compares_the_last_24h_range_with_the_30_day_median(monkeypatch):
    import data.binance_client as bc
    history = [0.02] * (24 * 30)

    async def fetch(symbol, interval, limit):
        return _bars(history + today)
    monkeypatch.setattr(bc, "fetch_ohlcv", fetch)
    today = [0.005] * 24                                  # every hour of the last day moves 0.5%
    a = asyncio.run(regime._activity())
    assert a["activity"] == "quiet" and a["btc_range_24h_pct"] < a["btc_range_median_pct"]
    today = [0.02] * 23 + [0.06]                          # a 6% hour in the last 24h
    assert asyncio.run(regime._activity())["activity"] == "volatile"
