"""The side switch (2026-11-17): listed books open no long, each refused long is written down, longs return on the record."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_executor import _reversal_setup                          # noqa: E402

from autotrader import engine                                       # noqa: E402
from experiments import entry_cells                                 # noqa: E402
from trading import sides                                           # noqa: E402


def _rows(book, pcts, settled=True):
    return [{"book": book, "symbol": f"S{i}USDT", "opened_ts": 1_000.0 + i, "price": 1.0, "switch": "side_off",
             "result": {"coin_pct": p} if settled else None} for i, p in enumerate(pcts)]


def test_the_default_switch_and_the_record_that_earns_longs_back(monkeypatch):
    assert set(sides.LONGS_OFF) == {"auto:ta", "auto:weekly", "auto:factor", "weekly_rs"}
    monkeypatch.setattr(entry_cells, "gated_records", lambda gate_only=True: [])
    assert sides.longs_off("auto:weekly") and not sides.longs_off("auto:reversal") and not sides.longs_off("cascade")
    monkeypatch.setattr(entry_cells, "gated_records", lambda gate_only=True: _rows("auto:weekly", [0.5] * 20))
    rec = sides.long_record("auto:weekly")
    assert rec["settled"] == 20 and rec["mean_pct"] == pytest.approx(0.5) and rec["earned"] and not sides.longs_off("auto:weekly")
    monkeypatch.setattr(entry_cells, "gated_records", lambda gate_only=True: _rows("auto:weekly", [2.0] * 10 + [-0.5] * 10))   # one half negative
    assert not sides.long_record("auto:weekly")["earned"] and sides.longs_off("auto:weekly")
    monkeypatch.setattr(entry_cells, "gated_records", lambda gate_only=True: _rows("auto:weekly", [0.5] * 19))               # one short of the record
    assert not sides.long_record("auto:weekly")["earned"]
    monkeypatch.setattr(entry_cells, "gated_records", lambda gate_only=True: _rows("auto:ta", [0.5] * 20))                   # another book's record
    assert sides.longs_off("auto:weekly") and not sides.longs_off("auto:ta")
    rep = sides.report()
    assert set(rep["books"]) == set(sides.LONGS_OFF) and rep["books"]["auto:ta"]["longs_off"] is False


def test_a_lane_long_is_refused_and_written_down_while_a_short_goes_through(tmp_path, monkeypatch):
    active = _reversal_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(engine.lanes, "NET_CAP_PCT", 100.0)                                          # the ledger's net cap is not what this test is about
    written = []

    async def refuse_long(book, symbol, now, price, why):
        written.append((book, symbol, price, why))
    async def gate_long(book, symbol, now, price):
        return None
    monkeypatch.setattr(entry_cells, "refuse_long", refuse_long)
    monkeypatch.setattr(entry_cells, "gate_long", gate_long)
    from trading import tape

    async def no_tape(symbol):
        return None
    monkeypatch.setattr(tape, "coin_tape", no_tape)                                                  # the per-coin entry gate reads Binance
    monkeypatch.setattr(entry_cells, "gated_records", lambda gate_only=True: [])
    from trading import coin_lock
    from storage import state as kv
    for sym, d in (("ZECUSDT", "short"), ("ZECUSDT", "long"), ("TAOUSDT", "long")):
        kv.delete_sync(coin_lock._stop_key(sym, d))
    leg = asyncio.run(engine._open_mirror_leg(active, "ZECUSDT", "long", 5.0, 0.5, "weekly mirror", "a week", None, {"lane": "weekly"}))
    assert leg is None and written == [("auto:weekly", "ZECUSDT", 1248.0, "side_off")]
    leg = asyncio.run(engine._open_mirror_leg(active, "ZECUSDT", "short", 5.0, 0.5, "weekly mirror", "a week", None, {"lane": "weekly"}))
    assert leg and not leg.get("error") and leg["direction"] == "short"
    monkeypatch.setattr(entry_cells, "gated_records", lambda gate_only=True: _rows("auto:weekly", [0.5] * 20))               # earned back
    leg = asyncio.run(engine._open_mirror_leg(active, "TAOUSDT", "long", 5.0, 0.5, "weekly mirror", "a week", None, {"lane": "weekly"}))
    assert leg and not leg.get("error") and leg["direction"] == "long" and len(written) == 1
