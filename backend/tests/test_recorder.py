"""The impulse scalper was retired on 2026-10-06; its 1-second market recorder was kept."""
from fastapi.testclient import TestClient

from scalper import config as C
from scalper.recorder import MarketRecorder
from scalper.store import Store

T0 = 1_790_000_000.0


def test_recorder_writes_bars_and_liquidations_and_never_trades(tmp_path):
    store = Store(str(tmp_path / "rec.db"), str(tmp_path / "market.db"))
    r = MarketRecorder(store)
    assert r.book.paused and not r.book.positions
    sym = C.SYMBOLS[0]
    for i in range(5):
        r.on_message("depth20@100ms", sym, {"b": [["100", "5"]], "a": [["100.1", "5"]], "E": (T0 + i) * 1000})
        r.on_message("aggTrade", sym, {"p": "100.05", "q": "1", "m": False, "E": (T0 + i) * 1000})
    r.on_message("forceOrder", "!FORCEORDER", {"o": {"s": sym, "S": "SELL", "p": "99", "ap": "99", "q": "2", "z": "2",
                                                     "T": int(T0 * 1000)}})
    for m in r.markets.values():
        m.roll(T0 + 10)
    r._persist_bars()
    store.close()
    bars = Store(str(tmp_path / "rec.db"), str(tmp_path / "market.db")).query("market", "SELECT * FROM bars_1s")
    liqs = Store(str(tmp_path / "rec.db"), str(tmp_path / "market.db")).query("market", "SELECT * FROM liquidations")
    assert bars and {b["symbol"] for b in bars} == {sym} and len(liqs) == 1
    assert not r.book.positions                                     # nothing could have been opened


def test_impulse_is_retired_everywhere(tmp_path):
    import main
    from trading import book_switches, overview
    c = TestClient(main.app)
    assert "impulse" in overview.RETIRED_SYSTEMS
    assert "impulse" not in book_switches.NAMES
    assert any(b["name"] == "impulse" for b in c.get("/api/books").json()["retired"])
    assert c.get("/api/impulse/live").status_code == 404
    assert c.post("/api/positions/impulse/x/close", headers={"Origin": "http://localhost:3000"}).status_code == 409
    ret = {r["key"]: r for r in c.get("/api/experiments/retired").json()["retired"]}
    assert ret["impulse_scalper"]["category"] == "Strategy"
