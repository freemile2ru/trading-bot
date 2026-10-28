"""Copy trading, second source: Hyperliquid parsing, episodes and curve, the wallet bar, first-seen position
tracking, consensus sizing; the positioning logger."""
import asyncio
import time

import pytest

from data import hyperliquid_client as hl
from experiments import copy_trading as ct
from storage import state as kv

NOW = float(int(time.time()))


def _fill(t_h, coin, dirn, side, sz, px, pnl=0.0, fee=0.01):
    return {"time": int((NOW - t_h * 3600) * 1000), "coin": coin, "dir": dirn, "side": side, "sz": str(sz), "px": str(px), "closedPnl": str(pnl), "fee": str(fee)}


def test_flatten_and_symbol_mapping():
    row = hl.flatten({"ethAddress": "0xabc", "accountValue": "120000", "displayName": None,
                      "windowPerformances": [["day", {"pnl": "10", "roi": "0.001", "vlm": "5000"}], ["month", {"pnl": "9000", "roi": "0.08", "vlm": "900000"}],
                                             ["week", {"pnl": "100", "roi": "0.01", "vlm": "90000"}], ["allTime", {"pnl": "40000", "roi": "0.5", "vlm": "3e6"}]]})
    assert row["equity"] == 120000 and row["month"]["roi"] == 0.08 and row["all"]["pnl"] == 40000
    perps = {"BTCUSDT", "1000PEPEUSDT"}
    assert hl.binance_symbol("BTC", perps) == "BTCUSDT" and hl.binance_symbol("kPEPE", perps) == "1000PEPEUSDT"
    assert hl.binance_symbol("@166", perps) is None and hl.binance_symbol("XYZ", perps) is None


def test_episodes_and_profile():
    fills = []
    for i in range(12):
        t0 = 10 * (i + 1) + 2                                       # hours ago, open; close 2h later
        win = i % 4 != 0
        fills += [_fill(t0, "ETH", "Open Long", "B", 1, 3000), _fill(t0 - 2, "ETH", "Close Long", "A", 1, 3030 if win else 2985, pnl=30 if win else -15)]
    fills.append(_fill(1, "@166", "Buy", "B", 100, 1))              # spot: ignored
    ep = hl.episodes(fills)
    assert len(ep) == 12 and all(abs(e["hold_h"] - 2) < 1e-6 for e in ep)
    prof = ct.profile_from_history(ep)
    assert prof["n"] == 12 and prof["median_hold_h"] == 2.0 and prof["win_move_pct"] == pytest.approx(0.9997, abs=1e-3) and prof["win_rate"] == 75.0
    assert ct.profile_passes(prof) is None
    curve = hl.pnl_curve(fills, 10_000, days=10)
    assert len(curve) == 11 and curve[-1][1] == pytest.approx((9 * 30 - 3 * 15 - 24 * 0.01) / 10_000 * 100, abs=0.01)


def test_curve_stats_active_only():
    from data import copy_trading_client as cc
    # trades on two days a fortnight apart, flat otherwise: judged on the active weeks only
    curve = [(NOW - (40 - i) * 86400, 0.0 if i < 10 else (1.0 if i < 24 else 2.5)) for i in range(41)]
    plain, active = cc.curve_stats(curve), cc.curve_stats(curve, active_only=True)
    assert plain["weeks_positive_pct"] < 60 and active["weeks_positive_pct"] == 100.0 and active["active_weeks"] == 2 and active["active_days"] == 2


def test_hl_bar():
    good = {"equity": 200_000, "month": {"roi": 0.3, "pnl": 50_000, "volume": 4_000_000}, "week": {"roi": 0.02}, "all": {"roi": 1.2}}
    assert ct.hl_passes(good) is None
    assert ct.hl_passes({**good, "equity": 10_000}).startswith("equity")
    assert ct.hl_passes({**good, "month": {**good["month"], "roi": 50.0}}).startswith("month ROI")       # deposits, not trading
    assert ct.hl_passes({**good, "month": {**good["month"], "volume": 200_000_000}}).startswith("turnover")  # market maker
    assert ct.hl_passes({**good, "week": {"roi": -0.01}}) == "a window negative"


def test_hyperliquid_rows_track_first_seen_and_close(monkeypatch):
    from trading import monitor
    kv.delete_sync(ct._k("hl", "0xL"))
    live = {"rows": [{"coin": "BTC", "side": "long", "size": 1.0, "entry": 80_000.0, "leverage": 3.0, "roe": 0.0}]}

    async def positions(addr):
        return live["rows"]

    async def perps(now):
        return {"BTCUSDT", "ETHUSDT"}

    async def live_price(sym):
        return 81_000.0
    monkeypatch.setattr(hl, "positions", positions)
    monkeypatch.setattr(ct, "_perps", perps)
    monkeypatch.setattr(monitor, "live_price", live_price)
    L = {"id": "0xL", "venue": "hyperliquid"}
    first = asyncio.run(ct._hyperliquid_rows(L, NOW))
    assert first[0]["untracked"] and first[0]["opened_ts"] == 0.0                 # held before we watched: never copied
    live["rows"].append({"coin": "ETH", "side": "short", "size": 10.0, "entry": 3000.0, "leverage": 5.0, "roe": 0.0})
    rows = asyncio.run(ct._hyperliquid_rows(L, NOW + 90))
    eth = next(r for r in rows if r["symbol"] == "ETHUSDT")
    assert not eth.get("untracked") and eth["opened_ts"] == NOW + 90 and eth["remaining"] == 1.0
    live["rows"][1] = {**live["rows"][1], "size": 4.0}                              # trimmed to 40%
    rows = asyncio.run(ct._hyperliquid_rows(L, NOW + 180))
    assert next(r for r in rows if r["symbol"] == "ETHUSDT")["remaining"] == pytest.approx(0.4)
    live["rows"] = live["rows"][:1]                                                 # ETH gone → closed at the live price
    rows = asyncio.run(ct._hyperliquid_rows(L, NOW + 270))
    gone = next(r for r in rows if r["symbol"] == "ETHUSDT")
    assert gone["closed_ts"] == NOW + 270 and gone["close_price"] == 81_000.0 and gone["remaining"] == 0.0
    kv.delete_sync(ct._k("hl", "0xL"))


def test_consensus_sizes_up(tmp_path, monkeypatch):
    from experiments import books
    from trading import book_switches, monitor
    from trading.paper_trader import PaperWallet
    monkeypatch.setattr(ct, "LOG_DB", str(tmp_path / "log.db"))
    w = PaperWallet(str(tmp_path / "c.db"), label="copy_trading", initial_balance=10_000)
    monkeypatch.setitem(books.WALLETS, "copy_trading", w)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    for lid in ("A", "B"):
        kv.delete_sync(ct._k("seen", lid))
    kv.delete_sync(ct._k("skipped"))
    prof = {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0}
    kv.set_sync(ct._k("leaders"), {"at": NOW, "leaders": [{"id": "A", "venue": "binance", "nickname": "a", "profile": prof},
                                                          {"id": "B", "venue": "binance", "nickname": "b", "profile": prof}]})
    row = {"position_id": "pA", "symbol": "ETHUSDT", "side": "long", "opened_ts": NOW - 600, "closed_ts": None, "entry": 3000.0, "close_price": None,
           "leverage": 10.0, "remaining": 1.0, "roi": 0.0, "status": "Open"}

    async def rows(L, now):
        return [{**row, "position_id": "p" + L["id"]}]

    async def live_price(sym):
        return 3003.0
    monkeypatch.setattr(ct, "leader_rows", rows)
    monkeypatch.setattr(monitor, "live_price", live_price)
    out = asyncio.run(ct.mirror(NOW))
    assert len(out["opened"]) == 1                                                   # the same trade isn't copied twice per leader
    t = w.get_portfolio()["open_trades"][0]
    assert t["consensus"] == 2 and t["notional_usd"] == pytest.approx(10_000 * ct.LEG_PCT / 100 * ct.CONSENSUS_X, rel=0.01)
    assert kv.get_sync(ct._k("consensus")) == [{"symbol": "ETHUSDT", "side": "long", "leaders": 2}]
    st = ct.status()
    held = {L["id"]: L["positions"] for L in st["leaders"]}
    assert held["A"][0]["symbol"] == "ETHUSDT" and held["A"][0]["side"] == "long" and held["A"][0]["state"] == "copied"
    assert held["B"][0]["state"] == "seen" and held["A"][0]["moved_pct"] == pytest.approx(0.1)
    # the record: one observation per leader position per poll, replayable later
    assert st["log"]["observations"] == 2 and st["log"]["positions"] == 2
    obs = ct.observations(leader_id="A")
    assert obs[0]["symbol"] == "ETHUSDT" and obs[0]["state"] == "copied" and obs[0]["mark"] == 3003.0
    asyncio.run(ct.mirror(NOW + 90))
    assert ct.log_status()["observations"] == 4
    kv.delete_sync(ct._k("positions"))
    kv.delete_sync(ct._k("leaders"), ct._k("skipped"), ct._k("consensus"), ct._k("seen", "A"), ct._k("seen", "B"))


def test_positioning_logger(tmp_path, monkeypatch):
    from data import positioning

    async def fake_get_json(url, params=None, timeout=None, **kw):
        return [{"timestamp": 1_800_000_000_000, "longShortRatio": "1.5", "buySellRatio": "0.9"},
                {"timestamp": 1_800_000_300_000, "longShortRatio": "1.6", "buySellRatio": "1.1"}]
    import data.http
    monkeypatch.setattr(data.http, "get_json", fake_get_json)
    monkeypatch.setattr(positioning, "DB", str(tmp_path / "p.db"))
    monkeypatch.setattr(positioning, "COINS", ["BTCUSDT"])
    n = asyncio.run(positioning.tick())
    assert n == 8 and positioning.status()["rows"] == 8
    assert asyncio.run(positioning.tick()) == 0                                     # idempotent
    assert positioning.series("BTCUSDT", "taker") == [(1_800_000_000, 0.9), (1_800_000_300, 1.1)]


def test_dropped_leader_is_followed_out_not_force_closed(tmp_path, monkeypatch):
    from experiments import books
    from trading import book_switches, monitor
    from trading.paper_trader import PaperWallet
    w = PaperWallet(str(tmp_path / "d.db"), label="copy_trading", initial_balance=10_000)
    monkeypatch.setitem(books.WALLETS, "copy_trading", w)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    monkeypatch.setattr(ct, "LOG_DB", str(tmp_path / "log.db"))
    kv.delete_sync(ct._k("seen", "X"), ct._k("skipped"), ct._k("leader", "X"))
    prof = {"median_hold_h": 3.0, "win_move_pct": 2.0, "loss_move_pct": 1.0}
    L = {"id": "X", "venue": "binance", "nickname": "x", "profile": prof}
    ct.remember(L)
    kv.set_sync(ct._k("leaders"), {"at": NOW, "leaders": [L]})
    feed = {"rows": [{"position_id": "pX", "symbol": "ETHUSDT", "side": "long", "opened_ts": NOW - 600, "closed_ts": None, "entry": 3000.0,
                      "close_price": None, "leverage": 10.0, "remaining": 1.0, "roi": 0.0, "status": "Open"}]}

    async def rows(L, now):
        return feed["rows"]

    async def live_price(sym):
        return 3003.0
    monkeypatch.setattr(ct, "leader_rows", rows)
    monkeypatch.setattr(monitor, "live_price", live_price)
    assert len(asyncio.run(ct.mirror(NOW))["opened"]) == 1
    # the leader drops off tomorrow's selection: the copy stays, and a NEW position of theirs is not copied
    kv.set_sync(ct._k("leaders"), {"at": NOW + 86400, "leaders": []})
    feed["rows"].append({**feed["rows"][0], "position_id": "pX2", "symbol": "SOLUSDT", "opened_ts": NOW + 86400 - 60})
    out = asyncio.run(ct.mirror(NOW + 12 * 3600))
    assert out["closed"] == [] and out["opened"] == [] and len(w.get_portfolio()["open_trades"]) == 1
    assert any(L.get("retired") for L in ct.leaders_to_poll())
    # a fast leader's copy still gets a full day: 3h median → 9h cap would have closed it at 12h, the floor keeps it
    assert w.get_portfolio()["open_trades"][0]["symbol"] == "ETHUSDT"
    # ...and when the ex-leader closes, so do we
    feed["rows"][0] = {**feed["rows"][0], "closed_ts": NOW + 13 * 3600, "close_price": 3010.0, "remaining": 0.0}
    out = asyncio.run(ct.mirror(NOW + 13 * 3600 + 60))
    assert [c["close_reason"] for c in out["closed"]] == ["leader_closed"]
    kv.delete_sync(ct._k("leaders"), ct._k("skipped"), ct._k("seen", "X"), ct._k("leader", "X"), ct._k("consensus"), ct._k("positions"))
