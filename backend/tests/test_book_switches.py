import asyncio

import pytest
from fastapi.testclient import TestClient

from experiments import books, signal_books
from trading import book_switches
from trading.paper_trader import PaperWallet


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def test_every_book_is_listed_and_starts_on(client):
    d = client.get("/api/books").json()
    names = [b["name"] for b in d["books"]]
    assert names == ["main", "auto", "claude_swing", "carry", "futures_trend", "weekly_rs", "copy_stocks", "copy_futures", *[f"copy_{m}_{v}" for m in ("crypto", "stocks", "futures") for v in ("wide", "fast", "late")], "auction", "multi_factor", "carry_q", "cascade", "events", "copy_trading", "unlocks", "vol_premium"]
    main = d["books"][0]
    assert main["enabled"] and main["mode"] == "paper" and main["unavailable"] is None
    retired = [r["name"] for r in d["retired"]]
    assert {"auto_rules", "signals_rules", "signals_claude", "hold72", "fade24", "impulse", "scalper"} <= set(retired)


def test_books_turned_off_in_env_cannot_be_switched(client):
    # conftest sets EXPERIMENTS_ENABLED=false
    exp = next(b for b in client.get("/api/books").json()["books"] if b["name"] == "claude_swing")
    assert not exp["enabled"] and "EXPERIMENTS_ENABLED" in exp["unavailable"]
    assert client.post("/api/books/claude_swing", json={"enabled": True}).status_code == 409
    assert client.post("/api/books/nope", json={"enabled": False}).status_code == 404


def test_main_off_refuses_manual_orders_and_survives_a_restart(client):
    assert client.post("/api/books/main", json={"enabled": False}).json()["enabled"] is False
    r = client.post("/api/trade/order", json={"symbol": "BTCUSDT", "side": "long", "type": "market",
                                              "notional_usd": 100, "leverage": 2, "stop_loss": 1})
    assert r.status_code == 409 and "switched off" in r.json()["detail"]
    assert not book_switches.enabled("main")                     # read back from state.db
    client.post("/api/books/main", json={"enabled": True})
    assert book_switches.enabled("main")


def test_auto_switch_is_the_autotrader_pause(client):
    from autotrader import engine
    client.post("/api/books/auto", json={"enabled": False})
    assert asyncio.run(engine.is_paused())
    client.post("/api/books/auto", json={"enabled": True})
    assert not asyncio.run(engine.is_paused())


def test_signal_book_switched_off_takes_nothing(tmp_path, monkeypatch):
    w = PaperWallet(str(tmp_path / "s.db"), label="signals_claude")
    w.reset_portfolio(10_000)
    monkeypatch.setattr(books, "WALLETS", {**books.WALLETS, "signals_claude": w})
    monkeypatch.setattr(books, "ENABLED", True)

    async def price(symbol):
        return 100.0
    monkeypatch.setattr(signal_books.monitor, "live_price", price)
    fired = {"symbol": "ETHUSDT", "action": "BUY", "entry_price": 100.0, "stop_loss": 97.0,
             "take_profit_1": 104.0, "take_profit_2": 106.0, "confidence": 70, "thesis": "x", "timeframe": "4h"}
    asyncio.run(book_switches.set_enabled("signals_claude", False))
    assert asyncio.run(signal_books.on_fired(fired)) is None
    asyncio.run(book_switches.set_enabled("signals_claude", True))
    assert asyncio.run(signal_books.on_fired(fired))["symbol"] == "ETHUSDT"


def test_ledger_filter_splits_paper_wallets_from_live_ledgers():
    from trading.history import in_ledger
    assert in_ledger("auto", "both") and in_ledger("auto_live", "both")
    assert in_ledger("auto", "paper") and not in_ledger("auto_live", "paper")
    assert in_ledger("main_live", "live") and not in_ledger("signals_rules", "live")


def test_each_book_lists_its_own_wallets_and_the_switch_filters_the_record(client):
    books_ = {b["name"]: b for b in client.get("/api/books").json()["books"]}
    assert [w["system"] for w in books_["auto"]["wallets"]] == ["auto", "auto_live"]
    assert [w["system"] for w in books_["claude_swing"]["wallets"]] == ["claude_swing"]
    live_view = client.get("/api/books?ledger=live").json()
    assert live_view["ledger"] == "live" and client.get("/api/books?ledger=bogus").status_code == 422
    for path in ("/api/experiments?ledger=paper", "/api/history/insights?ledger=live", "/api/exits/compare?ledger=paper"):
        assert client.get(path).status_code == 200, path


def test_retired_books_stay_off_and_cannot_be_switched(client):
    book_switches.RETIRED_BOOKS_ENFORCED = True
    try:
        assert not book_switches.enabled("hold72") and not book_switches.enabled("signals_claude")
        assert client.post("/api/books/hold72", json={"enabled": True}).status_code == 404
        trades = client.get("/api/trades/all").json()
        assert "hold72" not in trades["open_count"] and "signals_rules" not in trades["open_count"]
    finally:
        book_switches.RETIRED_BOOKS_ENFORCED = False
