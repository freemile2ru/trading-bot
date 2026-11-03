"""The scanner's universe survives a CoinMarketCap outage: the last resolved pool serves until the feed is back."""
import asyncio
import logging

import httpx
import pytest

from data import cache, coinmarketcap_client as cmc
from storage import state as kv


def test_last_pool_serves_when_cmc_is_down(monkeypatch):
    kv.set_sync(cmc.LAST_POOL_KEY, {"at": 1_793_000_000, "pool": [{"symbol": "BTC", "pair": "BTCUSDT"}, {"symbol": "ETH", "pair": "ETHUSDT"}, {"symbol": "SOL", "pair": "SOLUSDT"}]})

    async def down(*a, **k):
        raise httpx.ConnectTimeout("")
    monkeypatch.setattr(cache, "cached", down)
    pool = asyncio.run(cmc.get_tradeable_pool(2))
    assert [c["pair"] for c in pool] == ["BTCUSDT", "ETHUSDT"]
    kv.delete_sync(cmc.LAST_POOL_KEY)
    with pytest.raises(httpx.ConnectTimeout):                     # nothing remembered: the outage is still an error
        asyncio.run(cmc.get_tradeable_pool(2))


def test_a_feed_outage_is_one_warning_line_not_a_traceback(caplog):
    from scanner import scheduler
    calls = {"n": 0}

    async def sweep():
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("")
        raise asyncio.CancelledError

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await scheduler._loop("general", 0, sweep)

    async def no_sleep(_):
        return None
    scheduler_sleep = asyncio.sleep
    try:
        asyncio.sleep = no_sleep                                  # the loop would otherwise wait five seconds between passes
        with caplog.at_level(logging.WARNING, logger="scanner.scheduler"):
            asyncio.run(run())
    finally:
        asyncio.sleep = scheduler_sleep
    msgs = [r for r in caplog.records if "feed is unavailable" in r.getMessage()]
    assert len(msgs) == 1 and msgs[0].levelno == logging.WARNING and msgs[0].exc_info is None and "ConnectTimeout" in msgs[0].getMessage()
