"""Cutting a position to size: the cut part is its own closed row, the remainder keeps only its own share."""
import asyncio

import pytest

from experiments import books, weekly_rs
from storage import state as kv
from trading import capital
from trading.paper_trader import PaperWallet


def _wallet(tmp_path) -> PaperWallet:
    return PaperWallet(str(tmp_path / "w.db"), label="t", initial_balance=5_000)


def _open(w: PaperWallet, symbol="AAAUSDT", direction="short", notional=1_000.0) -> dict:
    s = 1 if direction == "long" else -1
    t = w.open_trade(symbol=symbol, direction=direction, entry_price=100.0, stop_loss=100 * (1 - s * 0.3),
                     take_profit_1=100 * (1 + s * 0.9), take_profit_2=100 * (1 + s * 0.95), notional_usd=notional,
                     timeframe="1w", leverage=2, extra={"exit_policy": "manual", "shadows": False})
    assert not t.get("error"), t
    return t


def test_split_close_books_the_cut_part_and_keeps_the_rest(tmp_path):
    w = _wallet(tmp_path)
    t = _open(w)
    before = w.get_portfolio()["total_equity"]
    part = w.split_close(t["id"], t["quantity"] * 0.75, 80.0, "resize")      # a winning short, cut by three quarters
    p = w.get_portfolio()
    rest = next(x for x in p["open_trades"] if x["id"] == t["id"])
    assert part["status"] == "closed" and part["close_reason"] == "resize" and part["part_of"] == t["id"]
    assert rest["quantity"] == pytest.approx(t["quantity"] * 0.25) and rest["entry_price"] == t["entry_price"]
    assert rest["notional_usd"] == pytest.approx(250, rel=0.01)
    assert part["pnl_usd"] == pytest.approx(150 - 0.75 * t["fees_usd"] - 0.0005 * 600, abs=1.0)
    assert rest["fees_usd"] == pytest.approx(t["fees_usd"] * 0.25, abs=1e-3)
    # Nothing is created or lost: the cut only pays the exit cost on the part.
    exit_cost = part["fees_usd"] - 0.75 * t["fees_usd"]
    w.update_prices({"AAAUSDT": 80.0})
    assert w.get_portfolio()["total_equity"] == pytest.approx(before + 200 - exit_cost - (80 * 0.0005 * part["quantity"]) - 0.0001, abs=2.0)
    # Survives a reload.
    w2 = PaperWallet(str(tmp_path / "w.db"), label="t", initial_balance=5_000)
    assert [x["id"] for x in w2.all_trades()["closed"]] == [part["id"]]


def test_split_close_of_everything_is_a_plain_close(tmp_path):
    w = _wallet(tmp_path)
    t = _open(w)
    part = w.split_close(t["id"], t["quantity"], 100.0, "resize")
    assert part["id"] == t["id"] and w.get_portfolio()["open_trades"] == []


def test_sizing_ignores_capital_waiting_to_be_withdrawn(monkeypatch):
    kv.delete_sync(capital._k("added", "weekly_rs"), capital._k("paused"))
    monkeypatch.setattr(capital, "PLAN", {"weekly_rs": 1_000.0})
    kv.set_sync(capital._k("added", "weekly_rs"), -5_600.0)          # the plan wants $1,000; $4,400 withdrawn so far
    assert capital.shortfall("weekly_rs") == -3_400.0
    assert capital.sizing_equity("weekly_rs", 4_470.0) == pytest.approx(1_070.0)
    kv.set_sync(capital._k("added", "weekly_rs"), -9_000.0)          # fully withdrawn: size from what it has
    assert capital.sizing_equity("weekly_rs", 1_070.0) == 1_070.0
    kv.delete_sync(capital._k("added", "weekly_rs"))


def test_weekly_resize_cuts_big_legs_tops_up_small_ones_and_leaves_close_ones(monkeypatch, tmp_path):
    w = _wallet(tmp_path)
    _open(w, "AAAUSDT", notional=1_000), _open(w, "BBBUSDT", "long", notional=100), _open(w, "CCCUSDT", notional=260)
    monkeypatch.setattr(books, "WALLETS", {**books.WALLETS, "weekly_rs": w})

    async def price(sym):
        return 100.0
    monkeypatch.setattr(weekly_rs.monitor, "live_price", price)
    out = asyncio.run(weekly_rs.resize(equity=1_000.0))
    assert sorted(o["symbol"] for o in out) == ["AAAUSDT", "BBBUSDT"]
    sizes = {t["symbol"]: t["quantity"] * 100 for t in w.get_portfolio()["open_trades"]}
    assert sizes["AAAUSDT"] == pytest.approx(250, rel=0.01) and sizes["BBBUSDT"] == pytest.approx(250, rel=0.01)
    assert sizes["CCCUSDT"] == pytest.approx(260, rel=0.02)
    assert [t["close_reason"] for t in w.all_trades()["closed"]] == ["resize"]
    assert asyncio.run(weekly_rs.resize(equity=1_000.0)) == []       # idempotent


def test_rescale_closed_trades_scales_size_costs_and_pnl_together(tmp_path):
    w = _wallet(tmp_path)
    t = _open(w)
    closed = w.close_trade_manually(t["id"], 130.0, "stop_loss")                 # a short stopped 30% against it
    eq = w.get_portfolio()["total_equity"]
    out = w.rescale_closed_trades({closed["id"]: 0.25}, "test")
    r = w.all_trades()["closed"][0]
    assert out["rescaled"] == 1 and r["notional_usd"] == pytest.approx(250, rel=0.01)
    assert r["pnl_usd"] == pytest.approx(closed["pnl_usd"] * 0.25, abs=0.01) and r["pnl_pct"] == closed["pnl_pct"]
    assert r["fees_usd"] == pytest.approx(closed["fees_usd"] * 0.25, abs=1e-3)
    assert w.get_portfolio()["total_equity"] == pytest.approx(eq - closed["pnl_usd"] * 0.75, abs=0.02)
    w2 = PaperWallet(str(tmp_path / "w.db"), label="t", initial_balance=5_000)      # survives a reload
    assert w2.all_trades()["closed"][0]["pnl_usd"] == r["pnl_usd"]
    assert w.rescale_closed_trades({closed["id"]: 1.0}) == {"rescaled": 0, "pnl_shift": 0.0}
