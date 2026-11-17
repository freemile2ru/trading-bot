"""2026-11-17: every book's open trade can be closed by hand from the Trades tab, after a preview of what the close books."""
import pytest
from fastapi.testclient import TestClient

from experiments import books
from trading import monitor
from trading.paper_trader import PaperWallet


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def test_a_copy_book_trade_is_previewed_then_closed_by_hand(client, tmp_path, monkeypatch):
    w = PaperWallet(str(tmp_path / "c.db"), label="copy_trading")
    w.reset_portfolio(50_000)
    monkeypatch.setattr(books, "WALLETS", {**books.WALLETS, "copy_trading": w})

    async def price(symbol):
        return 3.10
    monkeypatch.setattr(monitor, "live_price", price)
    t = w.open_trade(symbol="NEARUSDT", direction="short", entry_price=3.184, stop_loss=3.3124, take_profit_1=1.6, take_profit_2=1.3, notional_usd=5000,
                     timeframe="copy", leverage=5, extra={"strategy": "copy_trading", "exit_policy": "manual", "shadows": False, "leader_id": "0xabc", "leader": "x"})
    p = client.get(f"/api/positions/copy_trading/{t['id']}/preview").json()
    assert p["mark"] == 3.10 and p["fill"] < 3.11 and p["exit_fee"] > 0 and p["fees_so_far"] > 0 and not p["live"]
    assert p["gross_pnl"] > 0 and abs(p["net_pnl"] - (p["gross_pnl"] - p["fees_so_far"] - p["exit_fee"] - p["funding"])) < 0.05
    r = client.post(f"/api/positions/copy_trading/{t['id']}/close")
    assert r.status_code == 200 and r.json()["close_reason"] == "manual" and r.json()["status"] == "closed"
    closed = w.all_trades()["closed"][-1]
    assert closed["id"] == t["id"] and abs(closed["pnl_usd"] - p["net_pnl"]) < 0.05       # the preview was the close
    assert client.get(f"/api/positions/copy_trading/{t['id']}/preview").status_code == 404
    assert client.post(f"/api/positions/copy_trading/{t['id']}/close").status_code == 404


def test_pair_books_and_retired_scalpers_still_refuse(client):
    assert client.post("/api/positions/carry/x/close").status_code == 409
    assert client.post("/api/positions/scalper/x/close").status_code == 409
    assert client.post("/api/positions/nope/x/close").status_code == 409
    assert client.post("/api/positions/copy_trading/x/levels", json={"stop_loss": 1.0}).status_code == 409   # stops stay with the engine
