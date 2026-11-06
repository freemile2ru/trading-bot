"""The cross-book stop-out cooldown: a stop-loss close on any ledger holds every automated same-side re-entry for an hour."""
import pytest

from storage import state as kv
from trading import coin_lock
from trading.paper_trader import PaperWallet


@pytest.fixture(autouse=True)
def _clean():
    for d in ("long", "short"):
        kv.delete_sync(coin_lock._stop_key("ZECUSDT", d))
    yield
    for d in ("long", "short"):
        kv.delete_sync(coin_lock._stop_key("ZECUSDT", d))


def _short(tmp_path, label="auto"):
    w = PaperWallet(str(tmp_path / f"{label}.db"), label=label, initial_balance=50_000)
    t = w.open_trade(symbol="ZECUSDT", direction="short", entry_price=1248.0, stop_loss=1373.0, take_profit_1=1100.0, take_profit_2=1000.0,
                     notional_usd=1_000.0, timeframe="1d", leverage=8, extra={"exit_policy": "manual", "shadows": False, "strategy": "auto"})
    return w, t


def test_a_stop_loss_close_starts_a_same_side_cooldown_on_the_coin(tmp_path):
    """ZEC, 2026-11-06: the reversal lane's short was stopped out and the copy book shorted the coin two minutes later."""
    w, t = _short(tmp_path)
    assert coin_lock.recent_stop_out("ZECUSDT", "short") is None
    w.close_trade_manually(t["id"], 1382.0, "stop_loss")
    why = coin_lock.recent_stop_out("ZECUSDT", "short")
    assert why and why.startswith("ZECUSDT short was stopped out on auto 0 min ago") and "60 min" in why
    assert coin_lock.recent_stop_out("ZECUSDT", "long") is None          # the other side is not a re-entry into the move
    assert coin_lock.recent_stop_out("ETHUSDT", "short") is None


def test_only_a_stop_starts_the_cooldown(tmp_path):
    w, t = _short(tmp_path)
    w.close_trade_manually(t["id"], 1200.0, "trailing_stop")              # a trailing stop is a win being kept
    assert coin_lock.recent_stop_out("ZECUSDT", "short") is None
    w, t = _short(tmp_path, "copy_trading")
    w.close_trade_manually(t["id"], 1300.0, "leader_closed")
    assert coin_lock.recent_stop_out("ZECUSDT", "short") is None


def test_the_cooldown_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(coin_lock, "STOP_OUT_COOLDOWN_S", 0)
    w, t = _short(tmp_path)
    w.close_trade_manually(t["id"], 1382.0, "stop_loss")
    assert coin_lock.recent_stop_out("ZECUSDT", "short") is None
