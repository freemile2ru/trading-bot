"""Erasing ledger artefacts: the rows go, the P&L is reversed, and nothing else moves."""
from trading.paper_trader import PaperWallet


def _open_and_close(w: PaperWallet, symbol: str, exit_price: float) -> dict:
    t = w.open_trade(symbol=symbol, direction="long", entry_price=100.0, stop_loss=90.0, take_profit_1=110.0, take_profit_2=120.0,
                     notional_usd=1_000.0, timeframe="1d", leverage=2, extra={"exit_policy": "manual", "shadows": False})
    assert not t.get("error"), t
    return w.close_trade_manually(t["id"], exit_price, "dedup")


def test_erase_reverses_pnl_and_removes_rows(tmp_path):
    w = PaperWallet(str(tmp_path / "w.db"), label="t", initial_balance=10_000)
    a = _open_and_close(w, "AAAUSDT", 110.0)        # a winner
    b = _open_and_close(w, "BBBUSDT", 95.0)         # a loser
    keep = _open_and_close(w, "CCCUSDT", 101.0)     # stays
    before = w.get_portfolio()
    assert before["total_equity"] == round(10_000 + a["pnl_usd"] + b["pnl_usd"] + keep["pnl_usd"], 2)
    out = w.erase_closed_trades({a["id"], b["id"]}, "test")
    assert out["removed"] == 2 and out["pnl"] == round(a["pnl_usd"] + b["pnl_usd"], 4)
    after = w.get_portfolio()
    assert [t["id"] for t in w.all_trades()["closed"]] == [keep["id"]]
    assert after["total_equity"] == round(10_000 + keep["pnl_usd"], 2)          # as if the two never happened
    assert after["initial_balance"] == 10_000
    # Survives a fresh load from disk.
    w2 = PaperWallet(str(tmp_path / "w.db"), label="t", initial_balance=10_000)
    assert [t["id"] for t in w2.all_trades()["closed"]] == [keep["id"]] and w2.get_portfolio()["total_equity"] == after["total_equity"]
    assert w.erase_closed_trades({"nope"}) == {"removed": 0, "pnl": 0.0}
