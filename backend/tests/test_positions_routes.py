import pytest
from fastapi.testclient import TestClient

from trading.paper_trader import PaperWallet


@pytest.fixture
def client(tmp_path, monkeypatch):
    import main
    from api import positions_routes as pr
    w = PaperWallet(str(tmp_path / "swing.db"), label="main")
    w.reset_portfolio(10_000)
    monkeypatch.setattr(pr, "_swing_wallet", lambda system: w)

    async def price(symbol):
        return 101.0
    monkeypatch.setattr(pr.monitor, "live_price", price)
    return TestClient(main.app), w


def test_experiment_positions_are_hands_off(client):
    c, _ = client
    assert c.post("/api/positions/claude_swing/x/close").status_code == 409
    assert c.delete("/api/positions/orders/signals_rules/x").status_code == 409


def test_engine_managed_levels_are_refused(client):
    c, _ = client
    assert c.post("/api/positions/scalper/x/levels", json={"stop_loss": 1}).status_code == 409


def test_close_and_edit_a_main_wallet_position(client):
    c, w = client
    t = w.open_trade("SOLUSDT", "long", 100.0, 98.0, 103.0, 105.0, risk_pct=1)
    r = c.post(f"/api/positions/main/{t['id']}/levels", json={"stop_loss": 99.0})
    assert r.status_code == 200 and r.json()["stop_loss"] == 99.0
    r = c.post(f"/api/positions/main/{t['id']}/close")
    assert r.status_code == 200 and r.json()["close_reason"] == "manual"
    assert not w.get_portfolio()["open_trades"]
    assert c.post(f"/api/positions/main/{t['id']}/close").status_code == 404


def test_cancel_a_main_wallet_order(client):
    import time
    c, w = client
    o = w.place_order("SOLUSDT", "long", 95.0, 93.0, 99.0, 101.0, None, time.time() + 3600, risk_pct=1)
    assert c.delete(f"/api/positions/orders/main/{o['id']}").status_code == 200
    assert not w.get_orders()["pending"]
