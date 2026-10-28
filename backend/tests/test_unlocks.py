"""Unlock shorts: calendar parsing, the qualifying rule, the entry window, open/close maths."""
import asyncio
import json

import pytest

from data import unlocks_client
from experiments import unlocks
from storage import state as kv

ROW = {"symbol": "ALLO", "slug": "allo", "name": "Allo", "cmcRank": 500, "circulatingSupply": 100_000_000,
       "nextUnlocked": {"tokenAmount": 17_000_000, "tokenAmountUsd": 33_000_000, "tokenAmountPercentage": 17.39, "date": 1_800_000_000_000},
       "nextUnlockedDetail": [{"tokenAmount": 10_000_000, "allocationName": "Series A1 + A2", "vestingType": "cliff"},
                              {"tokenAmount": 6_000_000, "allocationName": "Team", "vestingType": "cliff"},
                              {"tokenAmount": 1_000_000, "allocationName": "Community", "vestingType": "linear"}],
       "quotes": [{"name": "USD", "price": 1.9, "volume24h": 50_000_000, "marketCap": 190_000_000}]}


def test_parse_page_and_normalise():
    html = '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(
        {"props": {"pageProps": {"dehydratedState": {"queries": [{"state": {"data": {"tokenUnlockList": [ROW], "totalCount": 1}}}]}}}}) + "</script></html>"
    rows, total = unlocks_client.parse_page(html)
    assert total == 1 and rows[0]["symbol"] == "ALLO"
    u = unlocks_client.normalise(rows[0])
    assert u["ts"] == 1_800_000_000 and u["pct"] == 17.39 and u["cliff_share"] == 0.941 and u["insider_share"] == 0.941
    assert unlocks_client.parse_page("<html>nothing</html>") == ([], 0)
    assert unlocks_client.normalise({**ROW, "nextUnlocked": {}}) is None


def test_perp_mapping():
    perps = {"ALLOUSDT", "1000PEPEUSDT", "BTCUSDT"}
    assert unlocks_client.perp_for("ALLO", perps) == "ALLOUSDT"
    assert unlocks_client.perp_for("PEPE", perps) == "1000PEPEUSDT"
    assert unlocks_client.perp_for("SERSH (OLD)", perps) is None and unlocks_client.perp_for("XYZ", perps) is None


def test_qualifies_shape():
    base = unlocks_client.normalise(ROW)
    assert unlocks.qualifies(base)
    assert not unlocks.qualifies({**base, "cliff_share": 0.3})                           # a linear drip
    assert not unlocks.qualifies({**base, "pct": 0.5})                                   # too small
    assert not unlocks.qualifies({**base, "insider_share": 0.1, "pct": 2.0})             # ecosystem unlock, not large
    assert unlocks.qualifies({**base, "insider_share": 0.1, "pct": 6.0})                 # large enough regardless
    assert not unlocks.qualifies({**base, "volume_24h": 100_000})                        # illiquid


def test_candidates_window_and_dedupe(tmp_path):
    unlocks.use(str(tmp_path / "u.db"))
    now = 1_800_000_000 - 10 * 86400
    base = unlocks_client.normalise(ROW)
    cal = [base, {**base, "symbol": "FAR", "ts": now + 20 * 86400}, {**base, "symbol": "NEAR", "ts": now + 86400}, {**base, "symbol": "NOPERP"}]
    perps = {"ALLOUSDT", "FARUSDT", "NEARUSDT", "BTCUSDT"}
    got = unlocks.candidates(cal, perps, now)
    assert [c["perp"] for c in got] == ["ALLOUSDT"]                 # FAR is outside 14 days, NEAR inside 2 days, NOPERP has no perp
    unlocks._write("INSERT INTO unlocks (id, symbol, unlock_ts, status) VALUES ('x', 'ALLOUSDT', ?, 'pending')", (1_800_000_000,))
    assert unlocks.candidates(cal, perps, now) == []                 # already on the book


def test_open_mark_close(tmp_path, monkeypatch):
    unlocks.use(str(tmp_path / "u2.db"))
    kv.delete_sync(kv.key("capital", "added", "unlocks"))
    prices = {"ALLOUSDT": 2.0, "BTCUSDT": 80_000.0}

    async def live_price(sym):
        return prices[sym]

    async def no_funding(symbol, start):
        return []
    import data.binance_client
    from trading import monitor
    monkeypatch.setattr(monitor, "live_price", live_price)
    monkeypatch.setattr(data.binance_client, "fetch_funding_history", no_funding)
    now = 1_800_000_000 - 10 * 86400
    unlocks._write("INSERT INTO unlocks (id, symbol, coin, unlock_ts, pct, usd, insider_share, cliff_share, deadline_ts, status) "
                   "VALUES ('a', 'ALLOUSDT', 'ALLO', ?, 17.39, 33e6, 0.94, 0.94, ?, 'pending')", (1_800_000_000, 1_800_000_000))
    p = asyncio.run(unlocks._open(unlocks.pending()[0], now))
    assert p["status"] == "open" and p["notional"] == 10_000 * unlocks.LEG_PCT / 100 and p["coin_entry"] < 2.0 < p["btc_entry"] / 40_000
    # the coin falls 10%, BTC flat → the pair is up ≈ 10%
    assert unlocks.spread_pct(p, 1.8, 80_000) == pytest.approx(10, abs=0.2)
    done = asyncio.run(unlocks.close(p, 1.8, 80_000, "unlock_reached", now + 9 * 86400))
    leg = 10_000 * unlocks.LEG_PCT / 100                     # ≈ +10% of the leg, less four taker fills
    assert 0.09 * leg < done["net"] < 0.10 * leg and unlocks.closed_positions()[0]["close_reason"] == "unlock_reached"
    assert unlocks.balance() == pytest.approx(10_000 + done["net"])
    assert unlocks.rows()[0]["outcome"] == "win" and "17.4% of supply" in unlocks.rows()[0]["note"]
