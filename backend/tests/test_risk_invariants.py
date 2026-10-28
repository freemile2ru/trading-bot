"""Risk invariants — the properties that must hold no matter how the rules are tuned.

If any of these fail, the system can lose more than it was designed to. They are cheap to
run (no network, no Redis) and should gate every change to sizing or exit logic.
"""

import time

import pytest

from trading import futures
from trading import position_manager as pm
from trading.paper_trader import MINUTE_MS, PaperWallet


@pytest.fixture
def wallet(tmp_path):
    """Real futures costs (fees, slippage) — invariants must hold with them on."""
    w = PaperWallet(str(tmp_path / "w.json"))
    w.reset_portfolio(10_000)
    return w


@pytest.fixture
def frictionless(monkeypatch):
    """Zero fees/slippage, for checks of pure accounting arithmetic."""
    monkeypatch.setattr(futures, "MAKER_FEE", 0.0)
    monkeypatch.setattr(futures, "TAKER_FEE", 0.0)
    monkeypatch.setattr(futures, "SLIPPAGE", 0.0)


def _scan(price, conviction=0.5, label="buy", rsi=55, hist=1.0, atr_pct=1.0):
    fast = {"signal": label, "rsi": rsi, "macd_histogram": hist, "bb_position": "inside"}
    return {"symbol": "XUSDT", "price": price, "conviction": conviction,
            "triggers": {"atr_pct": atr_pct},
            "signal_package": {"ta_summary": {"timeframe_signals": {"15m": fast}}}}


def _open_long(w, entry=100.0, stop=98.0):
    # 20% of 10k at 100 → 20 units; 1R = 2 × 20 = $40
    return w.open_trade("XUSDT", "long", entry, stop, 103.0, 105.0, size_pct=20)


def _loss_at_stop(t):
    s = 1 if t["direction"] == "long" else -1
    return (t["entry_price"] - t["stop_loss"]) * s * t["quantity"] - t.get("realized_pnl_usd", 0.0)


# ── Wallet accounting ──────────────────────────────────────────────────────────

def test_partial_exits_settle_once(wallet, frictionless):
    t = _open_long(wallet)
    wallet.adjust_trade(t["id"], "add", 102.0, 10, "add")
    wallet.adjust_trade(t["id"], "reduce", 104.0, 15, "trim")
    closed = wallet.close_trade_manually(t["id"], 105.0)
    avg = (100 * 20 + 102 * 10) / 30
    expected = (104 - avg) * 15 + (105 - avg) * 15
    assert closed["pnl_usd"] == pytest.approx(expected, abs=0.02)
    assert wallet.get_portfolio()["balance"] == pytest.approx(10_000 + expected, abs=0.02)


def test_equity_matches_balance_plus_open_pnl_with_partials(wallet, frictionless):
    t = _open_long(wallet)
    wallet.adjust_trade(t["id"], "reduce", 103.0, 10, "trim")    # realise +30 on half
    wallet.update_prices({"XUSDT": 101.0})                      # remainder +10
    p = wallet.get_portfolio()
    assert p["balance"] == 10_000                               # not settled until close
    assert p["total_equity"] == pytest.approx(10_040, abs=0.02)


def test_stop_wins_when_bar_touches_both(wallet):
    t = _open_long(wallet)
    m0 = (int(time.time() * 1000) // MINUTE_MS + 1) * MINUTE_MS
    wallet.annotate(t["id"], checked_from_ms=m0)
    events = wallet.process_paths({"XUSDT": {"bars": [
        {"open_time": m0, "open": 100, "high": 106, "low": 97, "close": 101}], "price": 101}})
    assert events[0]["trade"]["close_reason"] == "stop_loss"


def test_limit_fill_bar_cannot_book_a_target(wallet):
    now = time.time()
    o = wallet.place_order("XUSDT", "long", 99.0, 97.0, 102.0, 104.0, 20, now + 3600,
                           trade_fields={"extra": {"expires_at": now + 99_999}})
    m0 = o["checked_from_ms"]
    events = wallet.process_paths({"XUSDT": {"bars": [
        {"open_time": m0, "open": 100, "high": 105, "low": 98.9, "close": 100}], "price": 100}}, now=now + 90)
    assert [e["type"] for e in events] == ["filled"]
    assert wallet.get_portfolio()["open_trades"][0]["status"] == "open"


# ── Position manager ───────────────────────────────────────────────────────────

def test_pyramid_keeps_total_risk_at_or_below_1r(wallet):
    t = _open_long(wallet)
    init_risk = t["risk_usd"]
    d = pm.decide(t, _scan(102.5), time.time(), 100, 10_000)       # +1.25R
    assert d["action"] == "add" and d["kind"] == "pyramid"
    after = wallet.adjust_trade(t["id"], "add", 102.5, d["quantity"], d["reason"], d["new_stop"], d["kind"])
    assert _loss_at_stop(after) <= init_risk + 0.01
    assert after["stop_loss"] > t["stop_loss"]                     # the stop only ever tightens


def test_second_pyramid_still_within_1r(wallet):
    t = _open_long(wallet)
    init_risk = t["risk_usd"]
    d = pm.decide(t, _scan(102.5), time.time(), 100, 10_000)
    t = wallet.adjust_trade(t["id"], "add", 102.5, d["quantity"], d["reason"], d["new_stop"], d["kind"])
    t["adjustments"][-1]["ts"] -= pm.MIN_GAP + 1                   # past the spacing window
    d2 = pm.decide(t, _scan(104.5), time.time(), 100, 10_000)
    if d2["action"] == "add":
        t = wallet.adjust_trade(t["id"], "add", 104.5, d2["quantity"], d2["reason"], d2["new_stop"], d2["kind"])
    assert _loss_at_stop(t) <= init_risk + 0.01


def test_average_down_is_capped_and_happens_once(wallet):
    t = _open_long(wallet)
    init_risk = t["risk_usd"]
    d = pm.decide(t, _scan(99.0, conviction=0.5), time.time(), 100, 10_000)   # −0.5R, bounce
    assert d["action"] == "add" and d["kind"] == "average_down"
    t = wallet.adjust_trade(t["id"], "add", 99.0, d["quantity"], d["reason"], None, d["kind"])
    assert _loss_at_stop(t) <= pm.MAX_RISK_R * init_risk + 0.01
    t["adjustments"][-1]["ts"] -= pm.MIN_GAP + 1
    again = pm.decide(t, _scan(98.9, conviction=0.5), time.time(), 100, 10_000)
    assert not (again["action"] == "add" and again.get("kind") == "average_down")


def test_no_average_down_without_a_bounce(wallet):
    t = _open_long(wallet)
    d = pm.decide(t, _scan(99.0, conviction=0.5, label="sell", hist=-1.0), time.time(), 100, 10_000)
    assert d["action"] != "add"


def test_no_average_down_when_disabled(wallet, monkeypatch):
    monkeypatch.setattr(pm, "AVERAGE_DOWN", False)
    t = _open_long(wallet)
    assert pm.decide(t, _scan(99.0), time.time(), 100, 10_000)["action"] == "hold"


def test_reduce_never_goes_below_floor(wallet):
    t = _open_long(wallet)
    for _ in range(6):
        d = pm.decide(t, _scan(98.7, conviction=-0.2, label="sell"), time.time(), 100, 10_000)   # −0.65R, against
        if d["action"] != "reduce":
            break
        t = wallet.adjust_trade(t["id"], "reduce", 98.7, d["quantity"], d["reason"], None, d["kind"])
        t["adjustments"][-1]["ts"] -= pm.MIN_GAP + 1
    assert t["quantity"] >= pm.MIN_REMAINING * t["initial_quantity"] - 1e-9


def test_adjustments_are_spaced(wallet):
    t = _open_long(wallet)
    d = pm.decide(t, _scan(102.5), time.time(), 100, 10_000)
    t = wallet.adjust_trade(t["id"], "add", 102.5, d["quantity"], d["reason"], d["new_stop"], d["kind"])
    assert pm.decide(t, _scan(103.0), time.time(), 100, 10_000)["action"] == "hold"


def test_respects_gross_exposure_room(wallet):
    t = _open_long(wallet)
    d = pm.decide(t, _scan(102.5), time.time(), 0.5, 10_000)      # only 0.5% of wallet left
    assert d["action"] == "hold"
