"""Trend books: profit take after a week at ≥25% ROI."""
import time

from markets import engine
from trading.paper_trader import PaperWallet


def _open(w, sym, price, days_ago, leverage=1):
    t = w.open_trade(symbol=sym, direction="long", entry_price=price, stop_loss=price * (1 - 0.5 / leverage), take_profit_1=price * 3, take_profit_2=price * 4,
                     notional_usd=1000, timeframe="1d", leverage=leverage, extra={"strategy": "stocks_trend", "exit_policy": "manual", "shadows": False})
    assert not t.get("error"), t
    # back-date the open
    from datetime import datetime, timezone
    state = w._load()
    for x in state["open"]:
        if x["id"] == t["id"]:
            x["opened_at"] = datetime.fromtimestamp(time.time() - days_ago * 86400, timezone.utc).isoformat()
    w._save(state)
    return t


def test_profit_take_needs_both_age_and_roi(tmp_path):
    w = PaperWallet(str(tmp_path / "t.db"), label="stocks_trend", initial_balance=10_000)
    young = _open(w, "NVDAUSDT", 100.0, days_ago=2)        # up 30% but only 2 days old
    old_small = _open(w, "AAPLUSDT", 100.0, days_ago=10)   # 10 days old, up 10%
    old_big = _open(w, "MSFTUSDT", 100.0, days_ago=10)     # 10 days old, up 30%
    lev = _open(w, "AMDUSDT", 100.0, days_ago=10, leverage=10)   # 10 days old, up 5% in price = 50% on margin: not enough
    w.update_prices({"NVDAUSDT": 130.0, "AAPLUSDT": 110.0, "MSFTUSDT": 130.0, "AMDUSDT": 105.0})
    assert next(t for t in w.get_portfolio()["open_trades"] if t["id"] == lev["id"])["roe_pct"] > 25
    done = engine.take_profits(w)
    assert [t["symbol"] for t in done] == ["MSFTUSDT"] and done[0]["close_reason"] == "profit_take"
    assert {t["symbol"] for t in w.get_portfolio()["open_trades"]} == {"NVDAUSDT", "AAPLUSDT", "AMDUSDT"}
    assert young["id"] != old_small["id"] != old_big["id"]
