"""Futures model invariants: leverage, liquidation, fees, funding, margin."""

import time

import pytest

from autotrader import strategy
from trading import futures
from trading.paper_trader import MINUTE_MS, PaperWallet


@pytest.fixture
def wallet(tmp_path):
    w = PaperWallet(str(tmp_path / "w.json"))
    w.reset_portfolio(10_000)
    return w


# ── Liquidation & leverage ─────────────────────────────────────────────────────

def test_liquidation_formula():
    # 20x long on BTC (mmr 0.4%): 100 × (1 − 0.05 + 0.004)
    assert futures.liquidation_price(100, 20, "long", "BTCUSDT") == pytest.approx(95.4)
    assert futures.liquidation_price(100, 20, "short", "BTCUSDT") == pytest.approx(104.6)


@pytest.mark.parametrize("symbol", ["BTCUSDT", "SOLUSDT"])
@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("stop_pct", [0.3, 0.8, 1.5, 2.0, 2.5, 3.5, 5.0, 8.0, 15.0])
def test_liquidation_always_sits_well_beyond_the_stop(symbol, direction, stop_pct):
    lev = futures.safe_leverage(stop_pct, symbol)
    assert 1 <= lev <= futures.MAX_LEVERAGE
    liq = futures.liquidation_price(100, lev, direction, symbol)
    liq_distance = abs(100 - liq)
    if lev > 1:   # at 1x there is nothing lower to fall back to
        assert liq_distance >= futures.LIQ_BUFFER * stop_pct - 1e-9


def test_wide_stops_get_less_leverage():
    assert futures.safe_leverage(2.0, "SOLUSDT") > futures.safe_leverage(4.2, "SOLUSDT")
    assert futures.safe_leverage(0.5, "BTCUSDT") == futures.MAX_LEVERAGE


# ── Opening ────────────────────────────────────────────────────────────────────

def test_risk_sizing_loses_about_risk_pct_at_the_stop(wallet):
    t = wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1)
    loss_at_stop = (t["entry_price"] - t["stop_loss"]) * t["quantity"]
    assert loss_at_stop == pytest.approx(100, rel=0.02)          # 1% of 10k
    assert t["margin_usd"] == pytest.approx(t["notional_usd"] / t["leverage"], abs=0.01)


def test_stop_on_wrong_side_is_rejected(wallet):
    assert "error" in wallet.open_trade("SOLUSDT", "long", 100, 101, 103, 105, risk_pct=1)
    assert "error" in wallet.open_trade("SOLUSDT", "short", 100, 99, 97, 95, risk_pct=1)


def test_insufficient_margin_is_rejected(wallet):
    # 300x-equity notional at 20x needs 15x equity in margin.
    r = wallet.open_trade("BTCUSDT", "long", 100, 99.7, 101, 102, size_pct=30_000)
    assert "insufficient margin" in r["error"]


# ── Exits ──────────────────────────────────────────────────────────────────────

def _walk(wallet, t, bars):
    m0 = (int(time.time() * 1000) // MINUTE_MS + 1) * MINUTE_MS
    wallet.annotate(t["id"], checked_from_ms=m0)
    for i, b in enumerate(bars):
        b["open_time"] = m0 + i * MINUTE_MS
    return wallet.process_paths({t["symbol"]: {"bars": bars, "price": bars[-1]["close"]}})


def test_wick_through_stop_and_liquidation_takes_the_stop(wallet):
    t = wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1)
    ev = _walk(wallet, t, [{"open": 100, "high": 100.2, "low": 90, "close": 99}])
    assert ev[0]["trade"]["close_reason"] == "stop_loss"
    assert ev[0]["trade"]["pnl_usd"] > -t["margin_usd"]


def test_gap_past_liquidation_loses_exactly_the_margin(wallet):
    t = wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1)
    gap = t["liquidation_price"] * 0.98
    ev = _walk(wallet, t, [{"open": gap, "high": gap, "low": gap * 0.99, "close": gap}])
    closed = ev[0]["trade"]
    assert closed["close_reason"] == "liquidated"
    entry_fee = futures.fee(t["notional_usd"], maker=False)
    assert closed["pnl_usd"] == pytest.approx(-(t["margin_usd"] + entry_fee), abs=0.5)


def test_round_trip_costs(wallet):
    t = wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1)
    closed = wallet.close_trade_manually(t["id"], 100)             # flat market
    # two taker fees plus slippage on both legs, and nothing else
    expected = 2 * futures.TAKER_FEE * t["notional_usd"] + 2 * futures.SLIPPAGE * t["notional_usd"]
    assert -closed["pnl_usd"] == pytest.approx(expected, rel=0.05)


def test_target_fills_as_maker_without_slippage(wallet):
    t = wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1, extra={"exit_policy": "fixed"})
    ev = _walk(wallet, t, [{"open": 101, "high": 105.5, "low": 100.8, "close": 105}])
    closed = ev[0]["trade"]
    assert closed["close_reason"] == "take_profit_2"
    assert closed["current_price"] == 105


# ── Funding ────────────────────────────────────────────────────────────────────

def test_funding_long_pays_short_receives_and_charges_once(wallet):
    lt = wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1)
    st = wallet.open_trade("ETHUSDT", "short", 100, 102, 97, 95, risk_pct=1)
    ts = int(time.time() * 1000) + 1000
    events = {"SOLUSDT": [{"time": ts, "rate": 0.0001, "mark": 100}],
              "ETHUSDT": [{"time": ts, "rate": 0.0001, "mark": 100}]}
    wallet.apply_funding(events)
    wallet.apply_funding(events)                                   # replay: must not double-charge
    trades = {t["symbol"]: t for t in wallet.get_portfolio()["open_trades"]}
    assert trades["SOLUSDT"]["funding_usd"] == pytest.approx(lt["quantity"] * 100 * 0.0001, abs=1e-3)
    assert trades["ETHUSDT"]["funding_usd"] == pytest.approx(-st["quantity"] * 100 * 0.0001, abs=1e-3)


# ── Adjustments under margin ───────────────────────────────────────────────────

def test_add_refused_without_free_margin(wallet):
    t = wallet.open_trade("BTCUSDT", "long", 100, 99.7, 101, 102, risk_pct=1)
    assert wallet.adjust_trade(t["id"], "add", 100, t["quantity"] * 1000, "too big") is None
    assert wallet.get_portfolio()["open_trades"][0]["quantity"] == t["quantity"]


def test_position_size_caps():
    assert strategy.position_size_pct(0.1, 0) <= strategy.MAX_POSITION_PCT
    assert strategy.position_size_pct(1.0, strategy.MAX_GROSS_PCT) == 0
