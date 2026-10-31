"""Lighter as a copy-trading venue: trade rebuilding, symbol mapping, the bar, selection and the position feed."""
import asyncio

import pytest

from data import lighter_client as lt
from experiments import copy_trading as ct
from storage import state as kv

NOW = 1_800_000_000.0
IDX = 4242


def _t(ts_s: float, size: float, price: float, sell: bool, sym: str = "ETH") -> dict:
    return {"market_symbol": sym, "size": str(size), "price": str(price), "timestamp": int(ts_s * 1000),
            "ask_account_id": IDX if sell else 1, "bid_account_id": 1 if sell else IDX}


def test_episodes_rebuild_round_trips_with_average_cost():
    rows = [_t(NOW, 1.0, 100.0, False), _t(NOW + 3600, 1.0, 110.0, False),              # long 2 @ avg 105
            _t(NOW + 7200, 2.0, 115.0, True),                                            # flat: +20 on $210 → +9.5%
            _t(NOW + 10800, 3.0, 120.0, True), _t(NOW + 14400, 3.0, 114.0, False),       # short 3, covered −6 → +18 on $360
            _t(NOW + 20000, 1.0, 50.0, False, "SOL"), _t(NOW + 23600, 2.0, 40.0, True, "SOL")]  # long 1, flip to short 1 at 40
    eps = lt.episodes(rows, IDX)
    assert [(e["coin"], e["side"], round(e["pnl"], 2), round(e["hold_h"], 1)) for e in eps] == [("ETH", "long", 20.0, 2.0), ("ETH", "short", 18.0, 1.0), ("SOL", "long", -10.0, 1.0)]
    assert eps[0]["move_pct"] == pytest.approx(20 / 210 * 100)
    assert eps[2]["move_pct"] == pytest.approx(-20.0)


def test_binance_symbol_maps_aliases_and_stock_names():
    perps = {"BTCUSDT", "AAPLUSDT", "SAMSUNGUSDT", "CLUSDT", "COPPERUSDT", "1000SHIBUSDT", "UNIUSDT"}
    assert lt.binance_symbol("BTC", perps) == "BTCUSDT" and lt.binance_symbol("AAPL", perps) == "AAPLUSDT"
    assert lt.binance_symbol("SAMSUNGUSD", perps) == "SAMSUNGUSDT" and lt.binance_symbol("WTI", perps) == "CLUSDT"
    assert lt.binance_symbol("XCU", perps) == "COPPERUSDT" and lt.binance_symbol("UNI/USDC", perps) == "UNIUSDT"
    assert lt.binance_symbol("US500", perps) is None and lt.binance_symbol("", perps) is None


def test_roi_curve_is_relative_to_the_first_point_and_equity():
    assert lt.roi_curve([(1.0, 1000.0), (2.0, 1100.0), (3.0, 900.0)], 2000.0) == [(1.0, 0.0), (2.0, 5.0), (3.0, -5.0)]
    assert lt.roi_curve([], 2000.0) == [] and lt.roi_curve([(1.0, 1.0)], 0.0) == []


def test_lighter_bar_rejects_tiny_huge_quiet_and_overtraded_accounts():
    ok = {"account_value": 200_000.0, "roi": 25.0, "pnl": 50_000.0, "volume": 4_000_000.0}
    assert ct.lighter_passes(ok) is None
    assert "equity" in ct.lighter_passes({**ok, "account_value": 900.0})
    assert "ROI" in ct.lighter_passes({**ok, "roi": 18180.0})
    assert "negative" in ct.lighter_passes({**ok, "pnl": -1.0})
    assert "turnover" in ct.lighter_passes({**ok, "volume": 200_000.0}) and "turnover" in ct.lighter_passes({**ok, "volume": 9e8})


def test_select_lighter_builds_a_leader_from_the_board_and_its_trades(monkeypatch):
    board = [{"rank": 1, "address": "0xabc", "account_value": 200_000.0, "pnl": 50_000.0, "roi": 25.0, "volume": 4_000_000.0, "streak": 3, "biggest_asset": "ETH"},
             {"rank": 2, "address": "0xdef", "account_value": 500.0, "pnl": 100.0, "roi": 20.0, "volume": 5_000.0, "streak": 1, "biggest_asset": None}]
    trades = []
    for i in range(12):                                                  # twelve 3-hour winners, one a day
        t0 = NOW - (60 - i * 4) * 86400
        trades += [_t(t0, 1.0, 100.0, False), _t(t0 + 3 * 3600, 1.0, 102.0, True)]
    trades += [_t(NOW - 5 * 86400, 1.0, 100.0, False), _t(NOW - 5 * 86400 + 7200, 1.0, 99.0, True)]   # and one loser
    series = [(NOW - (60 - d) * 86400, 1000.0 + d * 50.0) for d in range(61)]

    async def leaderboard(window, sort, limit, offset=0):
        return board if offset == 0 else []

    async def perps(now):
        return {"ETHUSDT", "BTCUSDT"}
    monkeypatch.setattr(ct, "_perps", perps)

    async def account_index(addr):
        return IDX if addr == "0xabc" else None

    async def fetch_trades(idx, start_ms):
        return trades

    async def pnl_series(idx, days):
        return series
    monkeypatch.setattr(lt, "leaderboard", leaderboard)
    monkeypatch.setattr(lt, "account_index", account_index)
    monkeypatch.setattr(lt, "trades", fetch_trades)
    monkeypatch.setattr(lt, "pnl_series", pnl_series)
    chosen = asyncio.run(ct._select_lighter(NOW))
    assert len(chosen) == 1
    L = chosen[0]
    assert L["id"] == f"lighter:{IDX}" and L["venue"] == "lighter" and L["index"] == IDX and L["aum"] == 200_000.0
    assert L["profile"]["n"] == 13 and L["profile"]["median_hold_h"] == 3.0 and L["profile"]["win_rate"] == pytest.approx(92.3, abs=0.1)
    assert L["stats"]["weeks_positive_pct"] == 100.0 and L["score"] > 0


def test_lighter_rows_track_first_seen_and_close(monkeypatch):
    L = {"id": f"lighter:{IDX}", "venue": "lighter", "index": IDX, "nickname": "x"}
    live = [{"coin": "AAPL", "side": "long", "size": 10.0, "entry": 250.0, "leverage": 5.0, "roe": 0.1}]

    async def positions(idx):
        assert idx == IDX
        return live

    async def perps(now):
        return {"AAPLUSDT", "BTCUSDT"}

    async def price(sym):
        return 260.0
    monkeypatch.setattr(lt, "positions", positions)
    monkeypatch.setattr(ct, "_perps", perps)
    monkeypatch.setattr(ct.monitor, "live_price", price)
    kv.delete_sync(ct._k("lt", L["id"]))
    first = asyncio.run(ct._lighter_rows(L, NOW))
    assert first[0]["symbol"] == "AAPLUSDT" and first[0]["untracked"] and first[0]["opened_ts"] == 0.0        # held before we watched
    live.append({"coin": "BTC", "side": "short", "size": 1.0, "entry": 70_000.0, "leverage": 3.0, "roe": 0.0})
    second = asyncio.run(ct._lighter_rows(L, NOW + 90))
    btc = next(r for r in second if r["symbol"] == "BTCUSDT")
    assert not btc.get("untracked") and btc["opened_ts"] == NOW + 90 and btc["remaining"] == 1.0
    del live[0]
    third = asyncio.run(ct._lighter_rows(L, NOW + 180))
    closed = next(r for r in third if r["symbol"] == "AAPLUSDT")
    assert closed["closed_ts"] == NOW + 180 and closed["close_price"] == 260.0 and closed["remaining"] == 0.0
    assert ct.market_of("AAPLUSDT") == "stocks"
    kv.delete_sync(ct._k("lt", L["id"]))
