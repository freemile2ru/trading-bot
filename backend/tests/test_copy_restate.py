"""Copies sized before the 1%-of-book risk cap are restated at today's size (the user, 2026-11-19, after the NEAR copy's
$723 loss on a $32.75k book)."""
import pytest

from experiments import books, copy_trading as ct
from trading.paper_trader import PaperWallet


def _copy(w, symbol, stop_pct, notional, extra):
    s = stop_pct / 100
    t = w.open_trade(symbol=symbol, direction="short", entry_price=100.0, stop_loss=100 * (1 + s), take_profit_1=50.0, take_profit_2=40.0,
                     notional_usd=notional, timeframe="copy", leverage=5,
                     extra={"exit_policy": "manual_lock", "shadows": False, "stop_pct": stop_pct, "strategy": "copy_trading", **extra})
    assert not t.get("error"), t
    return t


def test_oversized_copies_are_restated_to_the_cap_and_the_rest_left_alone(tmp_path, monkeypatch):
    w = PaperWallet(str(tmp_path / "c.db"), label="copy_trading", initial_balance=32_750)
    monkeypatch.setitem(books.WALLETS, "copy_trading", w)
    monkeypatch.setattr(ct, "MAX_RISK_PCT", 1.0)
    # the NEAR shape: ×1.56 on top of a $440 budget figure, no cap stamp, stopped out at 4%
    near = _copy(w, "NEARUSDT", 4.0, 17_000.0, {"size_x": 1.56, "risk_budget": {"on": True, "risk_usd": 440.0}})
    # a copy opened under the cap: stamped, never touched
    capped = _copy(w, "OPUSDT", 4.0, 8_000.0, {"size_x": 1.0, "risk_cap": {"pct": 1.0, "bound": True}, "risk_budget": {"on": True, "risk_usd": 330.0}})
    # a small pre-cap copy already inside the cap: nothing to restate
    small = _copy(w, "SOLUSDT", 4.0, 1_800.0, {"size_x": 0.31, "risk_budget": {"on": True, "risk_usd": 235.0}})
    for t in (near, capped, small):
        w.close_trade_manually(t["id"], 104.0, "stop_loss")
    before = {t["id"]: t for t in w.all_trades()["closed"]}
    eq0 = w.get_portfolio()["total_equity"]
    rows = ct.oversized_copies("copy_trading")
    assert [r["symbol"] for r in rows] == ["NEARUSDT"]
    r = rows[0]
    assert r["equity_at_entry"] == pytest.approx(32_750, rel=0.002)
    assert r["target_notional"] == pytest.approx(32_750 / 4, rel=0.002) and r["factor"] == pytest.approx(0.4816, abs=0.002)   # 1% of the book at a 4% stop
    out = ct.restate_oversized("copy_trading")
    assert out["rescaled"] == 1 and out["rows"][0]["id"] == near["id"]
    after = {t["id"]: t for t in w.all_trades()["closed"]}
    n = after[near["id"]]
    assert n["notional_usd"] == pytest.approx(32_750 / 4, rel=0.002) and n["pnl_usd"] == pytest.approx(before[near["id"]]["pnl_usd"] * r["factor"], abs=0.05)
    assert n["pnl_pct"] == before[near["id"]]["pnl_pct"] and n["rescaled"][0]["factor"] == r["factor"]
    assert after[capped["id"]]["pnl_usd"] == before[capped["id"]]["pnl_usd"] and after[small["id"]]["pnl_usd"] == before[small["id"]]["pnl_usd"]
    assert w.get_portfolio()["total_equity"] == pytest.approx(eq0 + out["pnl_shift"], abs=0.05) and out["pnl_shift"] > 0
    # the book's R is unchanged by the restatement; running it again finds nothing
    assert n["pnl_usd"] / n["risk_usd"] == pytest.approx(before[near["id"]]["pnl_usd"] / before[near["id"]]["risk_usd"], rel=1e-4)
    assert ct.oversized_copies("copy_trading") == [] and ct.restate_oversized("copy_trading")["rescaled"] == 0
