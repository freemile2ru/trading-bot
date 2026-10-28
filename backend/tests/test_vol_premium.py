"""Vol premium condors: instrument parsing, structure design and sizing, marking and settlement."""
import asyncio
import json

import pytest

from data import deribit_client as dc
from experiments import vol_premium as vp
from storage import state as kv

NOW = 1_800_000_000.0


def test_instrument_parsing_and_fee():
    p = dc.parse("BTC-27NOV26-80000-C")
    assert p["type"] == "call" and p["strike"] == 80_000 and p["coin"] == "BTC"
    from datetime import datetime, timezone
    assert dc.expiry_ts("BTC-27NOV26-80000-C") == datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc).timestamp()
    assert dc.fee_usd(0.01, 80_000, 1) == 24.0                          # 0.03% of underlying
    assert dc.fee_usd(0.0001, 80_000, 1) == 1.0                         # capped at 12.5% of the premium


def _chain(s=80_000.0, iv=50.0, expiry=NOW + 7 * 86400, coin="BTC"):
    """A synthetic chain: strikes every 1,000, marks from a crude |moneyness| decay, bid/ask ±5%."""
    rows = []
    for k in range(60_000, 101_000, 1_000):
        for kind in ("put", "call"):
            dist = abs(k - s) / s
            mark = max(0.0005, 0.045 * (1 - dist / 0.3)) if dist < 0.3 else 0.0005
            name = f"{coin}-27NOV26-{k}-{'C' if kind == 'call' else 'P'}"
            rows.append({"coin": coin, "expiry_ts": expiry, "strike": float(k), "type": kind, "name": name, "mark": mark,
                         "bid": mark * 0.95, "ask": mark * 1.05, "iv": iv, "oi": 1.0, "underlying": s})
    return rows


def test_design_picks_sigma_strikes_and_sizes_to_risk():
    d = vp.design(_chain(), "BTC", NOW + 7 * 86400, NOW, 10_000)
    assert d is not None
    sig = 0.5 * (7 / 365) ** 0.5                                        # ≈ 6.9%
    strikes = {(l["type"], l["side"]): l["strike"] for l in d["legs"]}
    assert strikes[("put", "sell")] == pytest.approx(80_000 * (1 - sig), abs=600)
    assert strikes[("call", "sell")] == pytest.approx(80_000 * (1 + sig), abs=600)
    assert strikes[("put", "buy")] < strikes[("put", "sell")] < 80_000 < strikes[("call", "sell")] < strikes[("call", "buy")]
    assert d["credit"] > 0 and d["max_loss"] > 0
    assert d["max_loss"] <= 10_000 * vp.RISK_PCT / 100 + 1e-6          # sized to the risk budget
    assert d["qty"] * 10 == pytest.approx(round(d["qty"] * 10)) and d["qty"] >= 0.1      # whole 0.1 BTC lots
    assert vp.design(_chain()[:4], "BTC", NOW + 7 * 86400, NOW, 10_000) is None     # too thin a chain
    assert vp.design(_chain(), "BTC", NOW + 7 * 86400, NOW, 100) is None            # can't afford a lot


def test_payout_and_structure_value():
    legs = [{"type": "put", "strike": 74_000, "side": "sell", "name": "p1"}, {"type": "call", "strike": 86_000, "side": "sell", "name": "c1"},
            {"type": "put", "strike": 68_000, "side": "buy", "name": "p2"}, {"type": "call", "strike": 92_000, "side": "buy", "name": "c2"}]
    assert vp.payout_at(legs, 80_000) == 0                              # expired between the shorts
    assert vp.payout_at(legs, 70_000) == 4_000                          # put side in the money
    assert vp.payout_at(legs, 60_000) == 6_000                          # capped by the wing
    assert vp.payout_at(legs, 95_000) == 6_000
    by = {"p1": {"mark": 0.01}, "c1": {"mark": 0.01}, "p2": {"mark": 0.002}, "c2": {"mark": 0.002}}
    assert vp.structure_value(legs, by, 80_000) == pytest.approx((0.01 + 0.01 - 0.002 - 0.002) * 80_000)
    by["c1"]["underlying"] = 81_000                                               # a leg quoted against its own future
    assert vp.structure_value(legs, by, 80_000) == pytest.approx((0.01 - 0.002 - 0.002) * 80_000 + 0.01 * 81_000)
    assert vp.structure_value(legs, {"p1": {"mark": 0.01}}, 80_000) is None


def test_open_mark_settle(tmp_path, monkeypatch):
    vp.use(str(tmp_path / "v.db"))
    kv.delete_sync(kv.key("capital", "added", "vol_premium"))
    chain = _chain()

    async def fetch_chain(coin):
        return chain if coin == "BTC" else []

    async def delivery(coin, expiry):
        return 79_000.0
    monkeypatch.setattr(dc, "chain", fetch_chain)
    monkeypatch.setattr(dc, "delivery_price", delivery)
    monkeypatch.setattr(vp, "COINS", ["BTC"])
    from trading import book_switches
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    from experiments import books
    monkeypatch.setattr(books, "ENABLED", True)
    asyncio.run(vp.tick(NOW))
    open_ = vp.open_positions()
    assert len(open_) == 1 and open_[0]["coin"] == "BTC" and open_[0]["credit"] > 0
    asyncio.run(vp.tick(NOW + 60))
    open_ = vp.open_positions()
    assert len(open_) == 1                                               # one structure per coin
    # marked at the same chain: nothing has moved, so the mark is the bid/ask spread paid at entry plus fees
    legs = json.loads(open_[0]["legs"])
    cost = vp.structure_value(legs, {r["name"]: r for r in chain}, 80_000) * open_[0]["qty"]
    assert open_[0]["mark"] == pytest.approx(open_[0]["credit"] - cost - open_[0]["fees"], abs=0.01)
    assert -open_[0]["credit"] < open_[0]["mark"] < 0
    # expiry: settles between the shorts → keeps the whole credit less fees
    closed = asyncio.run(vp.tick(NOW + 7 * 86400 + 600))
    assert len(closed) == 1 and closed[0]["payout"] == 0
    row = vp.closed_positions()[0]
    assert row["net"] == pytest.approx(row["credit"] - row["fees"], abs=0.01) and row["close_reason"] == "expired"
    assert vp.rows()[0]["outcome"] == "win" and vp.rows()[0]["symbol"] == "BTCUSDT"
    assert json.loads(row["legs"])[0]["side"] == "sell"
    assert vp.balance() == pytest.approx(10_000 + row["net"])
