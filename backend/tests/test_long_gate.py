"""The live long gate: a long failing a condition is refused, written down and scored; the gate's own reverse verdict."""
import asyncio

import pytest

from experiments import entry_cells as ec
from storage import state as kv


def _clear():
    for key in kv.get_sync(ec._k("gated", "index")) or []:
        kv.delete_sync(ec._k("gated", key))
    kv.delete_sync(ec._k("gated", "index"))


def test_a_long_into_a_flagged_cell_is_refused_and_written_down_once(monkeypatch):
    _clear()
    monkeypatch.setattr(ec, "LONG_GATE", True)

    async def conditions(symbol, direction, ts, price):
        return {"fund_pct": 0.02, "btc24_pct": 1.5, "pre4_pct": 0.8, "hour": 13}
    monkeypatch.setattr(ec, "conditions", conditions)
    why = asyncio.run(ec.gate_long("copy_trading", "HYPEUSDT", 1_794_700_000.0, 30.0))
    assert why.startswith("long gate: funding_paid, btc_up, chasing_4h, us_open")
    assert asyncio.run(ec.gate_long("copy_trading", "HYPEUSDT", 1_794_700_100.0, 30.1)) is not None       # same quarter hour: refused again…
    recs = ec.gated_records()
    assert len(recs) == 1 and recs[0]["price"] == 30.0 and recs[0]["flags"] == ["funding_paid", "btc_up", "chasing_4h", "us_open"]   # …written once
    assert asyncio.run(ec.gate_long("copy_trading", "NVDAUSDT", 1_794_700_000.0, 180.0)) is None          # stock perps are outside the gate
    monkeypatch.setattr(ec, "LONG_GATE", False)
    assert asyncio.run(ec.gate_long("copy_trading", "HYPEUSDT", 1_794_700_000.0, 30.0)) is None


def test_a_passing_long_is_not_refused(monkeypatch):
    _clear()
    monkeypatch.setattr(ec, "LONG_GATE", True)

    async def conditions(symbol, direction, ts, price):
        return {"fund_pct": 0.0, "btc24_pct": -1.0, "pre4_pct": -2.0, "hour": 3}
    monkeypatch.setattr(ec, "conditions", conditions)
    assert asyncio.run(ec.gate_long("auto:weekly", "SOLUSDT", 1_794_700_000.0, 100.0)) is None and ec.gated_records() == []


def test_refused_longs_settle_after_a_day_and_judge_the_gate(monkeypatch):
    _clear()
    monkeypatch.setattr(ec, "LONG_GATE", True)

    async def conditions(symbol, direction, ts, price):
        return {"fund_pct": 0.05, "btc24_pct": 3.0, "pre4_pct": 2.0, "hour": 14}
    monkeypatch.setattr(ec, "conditions", conditions)

    async def price_at(sym, ts):
        return 110.0                                                                # every refused long would have made +10%
    monkeypatch.setattr("experiments.index_shadow.price_at", price_at)
    t0 = ec.SINCE + 1000
    for i in range(45):
        asyncio.run(ec.gate_long("copy_trading", f"C{i}USDT", t0 + i * 1800, 100.0))
    assert asyncio.run(ec.settle_gated(t0 + 1800)) == 0
    assert asyncio.run(ec.settle_gated(t0 + 45 * 1800 + 25 * 3600)) == 45
    monkeypatch.setattr(ec, "records", lambda book=None: [{"book": "copy_trading", "direction": "long", "opened_ts": t0 + i * 1800, "actual_pct": -1.0 - 0.1 * (i % 3), "kept": True, "flags": []} for i in range(45)]
                        + [{"book": "copy_trading", "direction": "short", "opened_ts": t0, "actual_pct": 0.5, "cells": []}])
    monkeypatch.setattr(ec, "closed_trades", lambda b: [])
    g = ec.report()["gate"]
    assert g["refused"] == 45 and g["settled"] == 45 and g["would_have"]["mean_pct"] == pytest.approx(10.0 - ec.FEE_RT_PCT)
    assert g["verdict"]["verdict"] == "gate wrong — switch it off" and g["by_flag"]["btc_up"] == 45
