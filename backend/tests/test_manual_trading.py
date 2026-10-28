"""Trade tab: manual orders on the paper wallet (the live ledger reuses the same methods)."""

import time

import pytest

from trading import exits
from trading.paper_trader import PaperWallet


@pytest.fixture
def w(tmp_path):
    wallet = PaperWallet(str(tmp_path / "w.db"))
    wallet.reset_portfolio(10_000)
    return wallet


def _manual(w, **kw):
    args = dict(symbol="SOLUSDT", direction="long", entry_price=100.0, stop_loss=97.0, take_profit_1=5000.0,
                take_profit_2=5000.0, size_pct=None, timeframe="manual", notional_usd=2_000, leverage=5,
                extra={"exit_policy": "manual", "tp_set": False})
    args.update(kw)
    return w.open_trade(**args)


def test_size_in_usdt_and_chosen_leverage(w):
    t = _manual(w)
    assert "error" not in t, t
    assert t["notional_usd"] == pytest.approx(2_000 * t["entry_price"] / 100.0, rel=0.01)   # ~$2,000
    assert t["leverage"] == 5 and t["margin_usd"] == pytest.approx(t["notional_usd"] / 5, rel=0.01)
    assert t["exit_policy"] == "manual"


def test_leverage_that_would_liquidate_before_the_stop_is_refused(w):
    r = _manual(w, stop_loss=90.0, leverage=20)          # 20x liquidates ~4% away, stop is 10% away
    assert "error" in r and "liquidation" in r["error"]


def test_manual_policy_has_no_target_breakeven_or_expiry(w):
    t = _manual(w)
    wl = w
    far = t["opened_ts"] + 200 * 86400
    path = {"SOLUSDT": {"bars": [{"open_time": int(far * 1000), "open": 104.0, "high": 106.0, "low": 103.5,
                                  "close": 105.0}], "price": 105.0}}
    wl.process_paths(path, now=far + 60)
    open_ = wl.get_portfolio()["open_trades"]
    assert open_ and open_[0]["stop_loss"] == 97.0                    # no breakeven move, still open 200 days on
    assert "manual" not in exits.SHADOW_POLICIES


def test_edit_levels_validates_sides_and_liquidation(w):
    t = _manual(w)
    assert "error" in w.update_levels(t["id"], stop_loss=101.0)       # above price for a long
    assert "error" in w.update_levels(t["id"], take_profit=99.0)      # target below price
    assert "error" in w.update_levels(t["id"], stop_loss=70.0)        # beyond 5x liquidation
    u = w.update_levels(t["id"], stop_loss=95.0, take_profit=110.0)   # loosening by hand is allowed
    assert u["stop_loss"] == 95.0 and u["take_profit_2"] == 110.0 and u["tp_set"] is True


def test_limit_order_uses_notional_and_leverage(w):
    o = w.place_order("ETHUSDT", "short", 2000.0, 2050.0, 1900.0, 1900.0, None, time.time() + 3600,
                      notional_usd=1_000, leverage=10)
    assert "error" not in o, o
    assert o["notional_usd"] == 1_000 and o["leverage"] == 10 and o["margin_usd"] == 100
