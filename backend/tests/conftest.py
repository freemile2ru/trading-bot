import os
import sys

# Tests import app modules as top-level packages, the same way uvicorn runs them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
# Live trading can never be enabled from a test run, whatever the developer's .env says.
os.environ["LIVE_TRADING_ALLOWED"] = "false"
os.environ["LIVE_ENV_FILE"] = ""          # ignore backend/.env entirely
os.environ["EXPERIMENTS_ENABLED"] = "false"   # side experiments fetch live prices — tests opt in explicitly
os.environ["NEWS_ENABLED"] = "false"          # the news loop scrapes the web
os.environ["AUTOTRADER_DAILY_MIN"] = "false"  # forces an entry when idle — tests opt in explicitly
# Module-level wallets and books (main/auto paper, main/auto live, scalper) open their databases
# at import. Point every one at a throwaway directory BEFORE any app module is imported — a test
# once kill-switched the real live autotrader wallet and booked a fake close into swing_trading.db.
import tempfile
_TMP = tempfile.mkdtemp(prefix="oracle-tests-")
for _var, _name in (("STATE_DB", "state.db"), ("SWING_DB", "swing_trading.db"), ("SIGNALS_DB", "signals.db"),
                    ("LIVE_DB", "live.db"), ("SCALPER_TRADES_DB", "scalper_trades.db"),
                    ("SCALPER_MARKET_DB", "scalper_market.db"), ("IMPULSE_TRADES_DB", "impulse_trades.db"),
                    ("IMPULSE_MARKET_DB", "impulse_market.db"), ("NEWS_DB", "news.db")):
    os.environ[_var] = os.path.join(_TMP, _name)

import pytest


@pytest.fixture(autouse=True)
def _isolated_databases(tmp_path):
    """Every test gets throwaway state/signals/live databases — never the real ones."""
    from live import config as live_config
    from live import control
    from news import scoring as news_scoring
    from news import store as news_store
    from storage import signals, state
    state.use(str(tmp_path / "state.db"))
    news_store.use(str(tmp_path / "news.db"))
    from trading import guards
    guards._cache.update(at=0.0, trades=[])
    from experiments import carry, fade24, hold72
    hold72.use(str(tmp_path / "hold72.db"))
    fade24.use(str(tmp_path / "fade24.db"))
    from experiments import carry_q, events
    events.use(str(tmp_path / "events.db"))
    carry_q.use(str(tmp_path / "carry_q.db"))
    carry.use(str(tmp_path / "carry.db"))
    news_scoring._snap.update(at=0.0, market=None, coins={})
    signals.use(str(tmp_path / "signals.db"))
    # Retired books are always off in the app; their code is still tested, so tests may switch them.
    from trading import book_switches
    book_switches.RETIRED_BOOKS_ENFORCED = False
    live_config.DB = str(tmp_path / "live.db")
    control._con = None
    yield
    book_switches.RETIRED_BOOKS_ENFORCED = True
    control._con = None


def pytest_sessionstart(session):
    from storage import db
    real = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "data"))
    assert not os.path.realpath(db.SWING_DB).startswith(real), "tests must never open the real databases"


@pytest.fixture(autouse=True)
def _no_regime_network(monkeypatch):
    """The regime lookup fetches BTC candles from Binance — never from a test."""
    from trading import regime

    async def none():
        return None
    monkeypatch.setattr(regime, "current", none)


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch):
    """The data cache would otherwise read and write the running app's Redis (a test once
    cached an empty CryptoPanic result there for 30 minutes). Tests run uncached."""
    from data import cache

    async def none():
        return None
    monkeypatch.setattr(cache, "get_client", none)


@__import__("pytest").fixture(autouse=True)
def _copy_log_in_tmp(tmp_path, monkeypatch):
    """The copy-trading record is a real dataset — tests never write to it."""
    from experiments import copy_trading
    monkeypatch.setattr(copy_trading, "LOG_DB", str(tmp_path / "copy_trading_test.db"))


@__import__("pytest").fixture(autouse=True)
def _no_macro_blackout(request, monkeypatch):
    """Entry tests must not depend on the wall clock: outside tests of the calendar itself, no FOMC/CPI
    blackout is ever in force (the suite failed at 17:03 UTC on an FOMC day — correctly, but uselessly)."""
    if "test_calendar" in str(request.node.fspath):
        return
    from ops import calendar
    monkeypatch.setattr(calendar, "macro_blackout", lambda now=None: None)
