"""Entry cells: the conditions at each entry, the long gate, the short cells, the reversal long shadow; the E-ratio in the controls."""
import asyncio

import pytest

from experiments import control
from experiments import entry_cells as ec
from storage import state as kv


def test_the_long_gate_flags_each_failed_condition_and_leaves_unmeasured_ones_alone():
    assert ec.gate_flags({"fund_pct": 0.005, "btc24_pct": -1.0, "pre4_pct": -0.5, "hour": 3}) == []
    assert ec.gate_flags({"fund_pct": 0.01, "btc24_pct": 0.0, "pre4_pct": 0.1, "hour": 12}) == ["funding_paid", "btc_up", "chasing_4h", "us_open"]
    assert ec.gate_flags({"fund_pct": None, "btc24_pct": None, "pre4_pct": None, "hour": 15}) == ["us_open"]
    assert ec.short_cells({"btc24_pct": 2.0, "hour": 16}) == ["btc_up_day", "late_us"] and ec.short_cells({"btc24_pct": 1.9, "hour": 20}) == []


def test_record_reads_the_conditions_once_and_sorts_the_trade_into_its_cells(monkeypatch):
    async def conditions(symbol, direction, ts, price):
        return {"fund_pct": 0.02, "btc24_pct": 2.5, "pre4_pct": -1.0, "hour": 17}
    monkeypatch.setattr(ec, "conditions", conditions)
    kv.delete_sync(ec._k("index", "cascade"))
    long = {"id": "ec-l1", "symbol": "HYPEUSDT", "direction": "long", "opened_ts": 1_000.0, "closed_ts": 5_000.0, "notional_usd": 1000.0, "pnl_usd": -12.0, "entry_price": 10.0}
    rec = asyncio.run(ec.record("cascade", long))
    assert rec["flags"] == ["funding_paid", "btc_up"] and rec["kept"] is False and rec["actual_pct"] == pytest.approx(-1.2)
    short = {**long, "id": "ec-s1", "direction": "short", "pnl_usd": 30.0}
    rec = asyncio.run(ec.record("cascade", short))
    assert rec["cells"] == ["btc_up_day", "late_us"] and [r["trade_id"] for r in ec.records("cascade")] == ["ec-l1", "ec-s1"]
    assert ec.is_crypto("NVDAUSDT") is False and ec.is_crypto("HYPEUSDT")


def test_two_groups_waits_for_forty_then_needs_the_gap_t_and_both_halves():
    mk = lambda i, x: {"opened_ts": float(i), "actual_pct": x}
    kept = [mk(i, 0.6 + 0.1 * (i % 3)) for i in range(0, 80, 2)]
    rej = [mk(i, -0.6 - 0.1 * (i % 3)) for i in range(1, 80, 2)]
    assert ec.two_groups(kept, rej[:39], "b")["verdict"] == "measuring"
    v = ec.two_groups(kept, rej, "b")
    assert v["verdict"] == "works" and v["gap_pct"] > 1 and v["t"] > 2 and all(h > 0 for h in v["halves_pct"])
    flipped = [mk(i, 0.6 if i < 40 else -1.5) for i in range(0, 80, 2)]            # kept wins early, loses late
    assert ec.two_groups(flipped, rej, "b")["verdict"] == "no difference"


def test_the_reversal_long_shadow_writes_the_top_k_and_settles_them_hedged(monkeypatch):
    prices = {"AUSDT": (10.0, 11.0), "BUSDT": (20.0, 20.0), "BTCUSDT": (100.0, 104.0)}

    async def price_at(sym, ts):
        a, b = prices[sym]
        return a if ts < 50_000 else b
    monkeypatch.setattr("experiments.index_shadow.price_at", price_at)
    for key in kv.get_sync(ec._k("revlong", "index")) or []:
        kv.delete_sync(ec._k("revlong", key))
    kv.delete_sync(ec._k("revlong", "index"))
    n = asyncio.run(ec.shadow_reversal_longs({"AUSDT": 0.9, "BUSDT": 0.5, "CUSDT": -0.8}, 2, 7, 1_000.0))
    assert n == 2 and asyncio.run(ec.shadow_reversal_longs({"AUSDT": 0.9, "BUSDT": 0.5}, 2, 7, 1_000.0)) == 0   # a slot is written once
    assert asyncio.run(ec.settle_shadow(1_000.0 + 3600)) == 0                                                  # not yet 24h
    assert asyncio.run(ec.settle_shadow(1_000.0 + 25 * 3600)) == 2
    recs = {r["symbol"]: r["result"] for r in ec.shadow_records()}
    assert recs["AUSDT"]["coin_pct"] == pytest.approx(10.0 - 0.1) and recs["AUSDT"]["hedged_pct"] == pytest.approx(10.0 - 0.1 - 4.0 - 0.1)
    assert recs["BUSDT"]["hedged_pct"] == pytest.approx(-0.1 - 4.0 - 0.1)
    rep = ec.report()
    assert rep["reversal_long_shadow"]["settled"] == 2 and rep["reversal_long_shadow"]["verdict"] == "measuring"


def test_control_report_carries_the_e_ratio(monkeypatch):
    rows = [{"actual": 1.0, "held": {}, "after_exit_pct": {}, "best_pct": {"1h": 2.0, "4h": 3.0, "24h": 4.0}, "worst_pct": {"1h": -1.0, "4h": -3.0, "24h": -8.0},
             "stopped_at_h": None, "hold_h": 2.0, "opened_ts": 1.0, "trade_id": "x", "v": control.REPLAY_V}]
    monkeypatch.setattr(control, "records", lambda book: rows)
    monkeypatch.setattr(control, "closed_trades", lambda book: [])
    e = control.report("cascade")["all"]["e_ratio"]
    assert e == {"1h": 2.0, "4h": 1.0, "24h": 0.5}
