"""Ledger guards: no zero-size positions, no division by an empty starting balance."""
from trading.paper_trader import PaperWallet


def test_empty_wallet_refuses_zero_size_trades_and_reports_zero_return(tmp_path):
    w = PaperWallet(str(tmp_path / "e.db"), label="empty", initial_balance=0.0)
    t = w.open_trade(symbol="BTCUSDT", direction="long", entry_price=100.0, stop_loss=95.0, take_profit_1=110.0, take_profit_2=120.0, notional_usd=500)
    assert "size is zero" in t["error"]
    o = w.place_order(symbol="BTCUSDT", direction="long", limit_price=99.0, stop_loss=95.0, take_profit_1=110.0, take_profit_2=120.0,
                      size_pct=5.0, expires_at=9e9)
    assert "size is zero" in o["error"]
    p = w.get_portfolio()
    assert p["total_return_pct"] == 0.0 and p["open_trades"] == []
