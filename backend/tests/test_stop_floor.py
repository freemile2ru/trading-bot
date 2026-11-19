"""The data review of 2026-11-19, built on 11-20: the stop floor, the restatement note in the capital ledger, and the
stock copies free of the BTC tape gate."""
import pytest

from storage import state as kv
from trading import capital, stop_floor, target


def test_a_stop_inside_the_noise_is_widened_to_the_floor_and_the_rest_left_alone():
    assert stop_floor.apply(100.0, 99.1, "long", "stocks") == (98.5, True)             # 0.9% → 1.5%
    assert stop_floor.apply(100.0, 100.9, "short", "stocks") == (101.5, True)
    assert stop_floor.apply(100.0, 98.0, "long", "stocks") == (98.0, False)            # 2%: already outside
    assert stop_floor.apply(100.0, 101.0, "short", "crypto") == (102.0, True)          # 1% on an alt → 2%
    assert stop_floor.apply(100.0, 98.0, "long", "crypto") == (98.0, False)
    assert stop_floor.apply(100.0, 98.0, "long", None) == (98.0, False)                # unknown market: crypto's floor
    assert stop_floor.apply(0.0, 98.0, "long", "stocks") == (98.0, False)
    assert stop_floor.min_pct("stocks") == 1.5 and stop_floor.min_pct("crypto") == 2.0


def test_the_auction_book_sizes_at_the_floored_stop(monkeypatch):
    # a 0.5% level stop on a $30k book at 1% risk wanted $60k of notional; floored to 1.5% it is $20k — the same $300 at the stop
    from experiments import auction
    monkeypatch.setattr(auction, "MAX_GROSS", 10.0)
    notional, _ = auction._size(30_000.0, 100.0, 99.5)
    stop, floored = stop_floor.apply(100.0, 99.5, "long", "stocks")
    floored_notional, _ = auction._size(30_000.0, 100.0, stop)
    assert floored and notional == pytest.approx(60_000.0) and floored_notional == pytest.approx(20_000.0)
    assert floored_notional * 0.015 == pytest.approx(300.0)


def test_a_restatement_note_leaves_the_days_pnl_but_moves_no_capital():
    kv.delete_sync(capital._k("ledger"), capital._k("added", "copy_trading"))
    day = 86_400.0
    e = capital.note_adjustment("copy_trading", 364.7, "restated 3 copies", ts=10 * day + 3600)
    assert e["kind"] == "restatement" and e["amount"] == 364.7 and capital.added("copy_trading") == 0.0
    assert capital.deposits()[-1] == e
    # the equity curve jumps by the restatement inside the day: the day's P&L is only the trading
    series = [(9 * day + 1000, 32_000.0), (9 * day + 80_000, 32_400.0), (10 * day + 1000, 32_393.0), (10 * day + 7200, 32_758.0), (10 * day + 80_000, 32_900.0)]
    pnl = target.daily_pnl(series, [d for d in capital.deposits() if d["book"] == "copy_trading"], "copy_trading")
    assert pnl[target._day(10 * day)] == pytest.approx(32_900.0 - 32_400.0 - 364.7, abs=0.01)
    kv.delete_sync(capital._k("ledger"))


def test_restating_copies_notes_the_shift_in_the_ledger(tmp_path, monkeypatch):
    from experiments import books, copy_trading as ct
    from trading.paper_trader import PaperWallet
    kv.delete_sync(capital._k("ledger"))
    w = PaperWallet(str(tmp_path / "c.db"), label="copy_trading", initial_balance=32_750)
    monkeypatch.setitem(books.WALLETS, "copy_trading", w)
    t = w.open_trade(symbol="NEARUSDT", direction="short", entry_price=100.0, stop_loss=104.0, take_profit_1=50.0, take_profit_2=40.0,
                     notional_usd=17_000.0, timeframe="copy", leverage=5,
                     extra={"exit_policy": "manual_lock", "shadows": False, "stop_pct": 4.0, "size_x": 1.56, "risk_budget": {"on": True, "risk_usd": 440.0}})
    w.close_trade_manually(t["id"], 104.0, "stop_loss")
    out = ct.restate_oversized("copy_trading")
    notes = [d for d in capital.deposits() if d.get("kind") == "restatement"]
    assert out["rescaled"] == 1 and len(notes) == 1 and notes[0]["amount"] == out["pnl_shift"] and notes[0]["book"] == "copy_trading"
    kv.delete_sync(capital._k("ledger"))


def test_the_btc_tape_gate_applies_to_crypto_copies_only():
    import inspect
    from experiments import copy_trading as ct
    src = inspect.getsource(ct)
    assert 'and market == "crypto" else None' in src                                     # the gate line carries the market test
    assert ct.market_of("NVDAUSDT") == "stocks" and ct.market_of("ZECUSDT") == "crypto"
