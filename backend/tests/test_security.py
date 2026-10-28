from fastapi.testclient import TestClient


def _client():
    import main
    return TestClient(main.app)          # no lifespan: engines aren't started


def test_foreign_site_cannot_trigger_state_changes():
    r = _client().post("/api/live/kill", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_own_ui_and_reads_are_allowed():
    c = _client()
    assert c.get("/health", headers={"Origin": "https://evil.example"}).status_code == 200
    r = c.post("/api/books/main", json={"enabled": True}, headers={"Origin": "http://localhost:3000"})
    assert r.status_code != 403


def test_trades_endpoints_accept_every_system():
    c = _client()
    for system in ("main", "scalper_live", "impulse", "signals_claude", "claude_swing", "auto_rules"):
        assert c.get(f"/api/trades/all?system={system}&status=closed").status_code == 200, system
    assert c.get("/api/trades/all?system=nope").status_code == 422
