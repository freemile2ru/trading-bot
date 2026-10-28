import asyncio

import pytest

from ops import watchdog as W


@pytest.fixture
def wd(monkeypatch):
    sent, feed = [], {"price": 1000.0, "sweep": 1000.0}

    async def notify(title, body):
        sent.append(title)

    async def probe():
        return feed["price"], feed["sweep"]
    monkeypatch.setattr(W, "_notify", notify)
    monkeypatch.setattr(W, "_probe", probe)
    monkeypatch.setattr(W, "_mem", {"last_check": None, "last_price_ok": None, "started": 1000.0})
    return sent, feed


def test_raises_an_outage_once_and_clears_it_when_data_returns(wd):
    sent, feed = wd
    assert asyncio.run(W.check(1000 + 60))["status"] == "ok"
    s = asyncio.run(W.check(1000 + W.STALE_S))                         # 30 min with no new price or sweep
    assert s["status"] == "stale" and "no Binance price" in s["reason"] and sent == ["Crypto Oracle: market data stopped"]
    asyncio.run(W.check(1000 + W.STALE_S + 60))
    assert len(sent) == 1                                               # not re-sent every minute
    feed.update(price=1000 + W.STALE_S + 120, sweep=1000 + W.STALE_S + 120)
    s = asyncio.run(W.check(1000 + W.STALE_S + 120))
    assert s["status"] == "ok" and sent[-1] == "Crypto Oracle: data back"
    assert s["outages"][0]["minutes"] == 32 and W.status()["outages"]


def test_a_gap_between_checks_is_recorded_as_sleep(wd):
    sent, feed = wd
    asyncio.run(W.check(1000 + 60))
    feed.update(price=1000 + 5 * 3600, sweep=1000 + 5 * 3600)
    s = asyncio.run(W.check(1000 + 5 * 3600))
    assert sent == ["Crypto Oracle was asleep"] and "sleep" in s["outages"][0]["reason"] and s["status"] == "ok"


def test_shutdown_steps_are_bounded(monkeypatch):
    import asyncio
    import main as app_main

    async def hangs():
        await asyncio.sleep(60)

    async def fails():
        raise RuntimeError("boom")
    loop = asyncio.new_event_loop()
    t = loop.time()
    loop.run_until_complete(app_main._stop_bounded("x", hangs(), 0.2))
    loop.run_until_complete(app_main._stop_bounded("y", fails(), 0.2))
    assert loop.time() - t < 2                                             # neither a hang nor an error blocks shutdown
    loop.close()
