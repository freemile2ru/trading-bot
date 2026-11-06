"""Copy trading: leader bar and score, the too-late rule, mirroring entries and exits against a fake feed."""
import asyncio
import time

import pytest

from data import copy_trading_client as cc
from experiments import copy_trading as ct
from storage import state as kv

NOW = float(int(time.time()))          # the wallet stamps real time, so the mirror's clock must be real too


def _curve(days=90, daily=0.004, dip_at=None):
    pts, v = [], 0.0
    for i in range(days):
        v = (1 + v / 100) * (1 + daily) * 100 - 100
        if dip_at and i == dip_at:
            v = (1 + v / 100) * 0.8 * 100 - 100
        pts.append((NOW - (days - i) * 86400, round(v, 4)))
    return pts


def test_curve_stats_and_bar():
    st = cc.curve_stats(_curve())
    assert st["days"] == 90 and st["mdd_pct"] == 0 and st["weeks_positive_pct"] == 100 and st["last_30d_pct"] > 0
    row = {"copiers": 300, "aum": 500_000, "copier_pnl": 10_000, "mdd": 12.0, "win_rate": 65.0}
    assert ct.passes(row, st) is None
    assert ct.passes({**row, "copiers": 3}, st) == "3 copiers"
    assert ct.passes({**row, "copier_pnl": -5}, st) == "copiers losing"
    assert ct.passes({**row, "mdd": 45}, st).startswith("drawdown")
    dip = cc.curve_stats(_curve(dip_at=60))
    assert dip["mdd_pct"] == pytest.approx(20, abs=0.5)
    assert ct.passes(row, cc.curve_stats(_curve(days=20))) == "20 days of curve"
    assert ct.score(st) > ct.score(dip)


def test_profile_from_history():
    rows = []
    for i in range(12):
        win = i % 3 != 0
        rows.append({"opened_ts": NOW - (i + 1) * 86400, "closed_ts": NOW - (i + 1) * 86400 + 6 * 3600, "leverage": 10.0,
                     "roi": 0.2 if win else -0.1})
    p = ct.profile_from_history(rows)
    assert p["n"] == 12 and p["median_hold_h"] == 6.0 and p["win_move_pct"] == 2.0 and p["loss_move_pct"] == 1.0 and p["win_rate"] == pytest.approx(66.7, abs=0.1)
    assert ct.profile_from_history(rows[:3])["median_hold_h"] is None
    assert ct.profile_passes(p) is None
    assert "scalper" in ct.profile_passes({**p, "median_hold_h": 0.0})
    assert "martingale" in ct.profile_passes({**p, "win_rate": 100.0, "loss_move_pct": None})
    assert "closed trades" in ct.profile_passes({"n": 4})


def test_evaluate_too_late_rules():
    prof = {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0}
    pos = {"side": "long", "entry": 100.0, "opened_ts": NOW - 3600}
    assert ct.evaluate(pos, prof, 100.3, NOW)[0]                                  # fresh and barely moved
    assert not ct.evaluate(pos, prof, 101.2, NOW)[0]                              # +1.2% ≥ half their median win
    assert not ct.evaluate(pos, prof, 98.9, NOW)[0]                               # −1.1% against them: already failing
    assert not ct.evaluate({**pos, "opened_ts": NOW - 8 * 3600}, prof, 100.0, NOW)[0]   # 8h old vs 24h median hold
    ok, why, moved, _ = ct.evaluate({**pos, "side": "short"}, prof, 99.5, NOW)
    assert ok and moved == pytest.approx(0.5) and "entered" in why


def test_mirror_enters_follows_and_exits(tmp_path, monkeypatch):
    from experiments import books
    from trading import book_switches, monitor
    from trading.paper_trader import PaperWallet
    w = PaperWallet(str(tmp_path / "c.db"), label="copy_trading", initial_balance=10_000)
    monkeypatch.setitem(books.WALLETS, "copy_trading", w)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    monkeypatch.setattr(ct, "LOG_DB", str(tmp_path / "log.db"))
    monkeypatch.setattr(ct, "VARIANTS", {"base": ct.VARIANTS["base"]})                 # the base rule alone here; variants have their own test
    monkeypatch.setattr(ct, "ALL_BOOKS", {(m, "base"): ct.BOOKS_BY_MARKET[m] for m in ct.MARKETS})
    for k in ("leaders", "skipped", "seen"):
        kv.delete_sync(ct._k(k) if k != "seen" else ct._k("seen", "L1"))
    kv.set_sync(ct._k("leaders"), {"at": NOW, "leaders": [{"id": "L1", "nickname": "x", "nickname_en": "Leader One",
                                                           "profile": {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0}}]})
    feed = {"rows": [{"position_id": "p1", "symbol": "ETHUSDT", "side": "long", "opened_ts": NOW - 1800, "closed_ts": None, "entry": 3000.0,
                      "close_price": None, "pnl": 0.0, "roi": 0.0, "leverage": 10.0, "qty": 2.0, "closed_qty": 0.0, "remaining": 1.0, "status": "Open"},
                     {"position_id": "p2", "symbol": "SOLUSDT", "side": "short", "opened_ts": NOW - 1800, "closed_ts": None, "entry": 200.0,
                      "close_price": None, "pnl": 0.0, "roi": 0.0, "leverage": 10.0, "qty": 10.0, "closed_qty": 0.0, "remaining": 1.0, "status": "Open"}]}   # 10×: inside the leader-leverage cap, so the too-late rule is what skips it
    prices = {"ETHUSDT": 3005.0, "SOLUSDT": 196.0}        # SOL already −2% in the leader's favour: too late

    async def positions(pid, pages=1):
        return feed["rows"]

    async def live_price(sym):
        return prices[sym]
    monkeypatch.setattr(cc, "positions", positions)
    monkeypatch.setattr(monitor, "live_price", live_price)

    out = asyncio.run(ct.mirror(NOW))
    assert [t["symbol"] for t in out["opened"]] == ["ETHUSDT"]
    t = w.get_portfolio()["open_trades"][0]
    assert t["leader_position_id"] == "p1" and t["leverage"] == min(10, ct.MAX_LEVERAGE) and t["leader"] == "Leader One"
    assert t["stop_pct"] == ct.stop_pct({"loss_move_pct": 1.0}) == 2.0 and t["stop_loss"] == pytest.approx(3005.0 * 0.98, rel=1e-4)
    assert t["notional_usd"] == pytest.approx(ct.leg_notional(10_000, 2.0), rel=0.01)
    sk = ct.skipped()
    assert len(sk) == 1 and sk[0]["symbol"] == "SOLUSDT" and sk[0]["reason"].startswith("too late")
    # the same positions again: nothing new, nothing doubled
    assert asyncio.run(ct.mirror(NOW + 60))["opened"] == []
    assert len(w.get_portfolio()["open_trades"]) == 1
    # the leader halves ETH → we close half and ride the rest; SOL closes at 190 → the skipped trade's outcome is filled in (+3.06% from our mark)
    qty = t["quantity"]
    feed["rows"][0] = {**feed["rows"][0], "closed_qty": 1.2, "remaining": 0.4, "status": "Partially Closed"}
    feed["rows"][1] = {**feed["rows"][1], "closed_ts": NOW + 3600, "close_price": 190.0, "remaining": 0.0, "roi": 1.0}
    out = asyncio.run(ct.mirror(NOW + 120))
    assert [(c["close_reason"], c.get("partial")) for c in out["closed"]] == [("leader_reduced", True)]
    rest = w.get_portfolio()["open_trades"]
    assert len(rest) == 1 and rest[0]["quantity"] == pytest.approx(qty * ct.REDUCE_FRAC) and rest[0]["reduced"] is True
    assert [c["close_reason"] for c in w.all_trades()["closed"]] == ["leader_reduced"]
    # still at 0.4 next poll: nothing more happens; then they close → the rest goes as leader_closed
    assert asyncio.run(ct.mirror(NOW + 180))["closed"] == []
    feed["rows"][0] = {**feed["rows"][0], "closed_ts": NOW + 7200, "close_price": 3010.0, "remaining": 0.0, "status": "Closed"}
    out = asyncio.run(ct.mirror(NOW + 240))
    assert [c["close_reason"] for c in out["closed"]] == ["leader_closed"]
    assert w.get_portfolio()["open_trades"] == []
    assert ct.skipped()[0]["outcome_pct"] == pytest.approx((190 / 196 - 1) * -100, abs=0.01)
    st = ct.status()
    assert st["skip_rule"]["judged"] == 1 and st["skip_rule"]["would_have_won_pct"] == 100.0
    for k in ("leaders", "skipped"):
        kv.delete_sync(ct._k(k))
    kv.delete_sync(ct._k("seen", "L1"))


def test_leaderboard_parsing(monkeypatch):
    async def fake_post(path, body):
        assert path == "home-page/query-list" and body["timeRange"] == "90D"
        return {"list": [{"leadPortfolioId": 123, "nickname": "A", "roi": "150.5", "pnl": "1000", "aum": "250000", "mdd": "9.5", "winRate": "71",
                          "currentCopyCount": 400, "maxCopyCount": 1000, "copierPnl": "5000", "sharpRatio": "4.1", "tradFiTag": "Tradfi",
                          "apiKeyTag": "API_KEY_TRADE", "chartItems": [{"value": 0, "dataType": "ROI", "dateTime": 1790000000000},
                                                                         {"value": 1.5, "dataType": "ROI", "dateTime": 1790086400000}]}]}
    monkeypatch.setattr(cc, "_post", fake_post)
    rows = asyncio.run(cc.leaderboard("90D", "ROI"))
    assert rows[0]["id"] == "123" and rows[0]["roi"] == 150.5 and rows[0]["copiers"] == 400 and rows[0]["tradfi"] and rows[0]["api"]
    assert rows[0]["curve"] == [(1790000000.0, 0.0), (1790086400.0, 1.5)]


def test_a_leader_is_not_copied_into_a_side_just_stopped_out_elsewhere(tmp_path, monkeypatch):
    """ZEC, 2026-11-06 14:42 UTC: two minutes after the auto ledger's short was stopped out by a spike, a Lighter leader
    shorted ZEC at the spike's top and the copy book followed. The skip is recorded like any other, so it is judged."""
    from experiments import books
    from trading import book_switches, coin_lock, monitor
    from trading.paper_trader import PaperWallet
    w = PaperWallet(str(tmp_path / "c.db"), label="copy_trading", initial_balance=10_000)
    monkeypatch.setitem(books.WALLETS, "copy_trading", w)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    monkeypatch.setattr(ct, "LOG_DB", str(tmp_path / "log.db"))
    monkeypatch.setattr(ct, "VARIANTS", {"base": ct.VARIANTS["base"]})
    monkeypatch.setattr(ct, "ALL_BOOKS", {(m, "base"): ct.BOOKS_BY_MARKET[m] for m in ct.MARKETS})
    for k in ("leaders", "skipped"):
        kv.delete_sync(ct._k(k))
    kv.delete_sync(ct._k("seen", "L1"))
    kv.set_sync(ct._k("leaders"), {"at": NOW, "leaders": [{"id": "L1", "nickname": "x", "nickname_en": "Leader One",
                                                           "profile": {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0}}]})
    rows = [{"position_id": "p1", "symbol": "ZECUSDT", "side": "short", "opened_ts": NOW - 60, "closed_ts": None, "entry": 1374.0,
             "close_price": None, "pnl": 0.0, "roi": 0.0, "leverage": 6.0, "qty": 2.0, "closed_qty": 0.0, "remaining": 1.0, "status": "Open"}]

    async def positions(pid, pages=1):
        return rows

    async def live_price(sym):
        return 1372.0
    monkeypatch.setattr(cc, "positions", positions)
    monkeypatch.setattr(monitor, "live_price", live_price)
    coin_lock.record_stop_out("auto", {"symbol": "ZECUSDT", "direction": "short", "close_reason": "stop_loss", "current_price": 1382.0, "pnl_usd": -546.0})
    try:
        assert asyncio.run(ct.mirror(NOW))["opened"] == []
        sk = ct.skipped()
        assert len(sk) == 1 and sk[0]["symbol"] == "ZECUSDT" and "stopped out on auto" in sk[0]["reason"]
        assert w.get_portfolio()["open_trades"] == []
    finally:
        kv.delete_sync(coin_lock._stop_key("ZECUSDT", "short"))
        for k in ("leaders", "skipped"):
            kv.delete_sync(ct._k(k))
        kv.delete_sync(ct._k("seen", "L1"))


def _closed_copy(w, side, leader, lev, win):
    s = 1 if side == "long" else -1
    t = w.open_trade(symbol="ETHUSDT", direction=side, entry_price=100.0, stop_loss=100.0 * (1 - s * 0.02), take_profit_1=150.0, take_profit_2=160.0,
                     notional_usd=1_000.0, timeframe="copy", leverage=5,
                     extra={"strategy": "copy_trading", "leader_id": leader, "venue": "lighter", "leader_leverage": lev, "exit_policy": "manual", "shadows": False})
    w.close_trade_manually(t["id"], 100.0 * (1 + s * (0.03 if win else -0.02)), "leader_closed")


def test_our_record_sizes_copies_by_side_leverage_bucket_and_leader(tmp_path, monkeypatch):
    """Prod's first eight copies (2026-11-06): shorts won, the 15–20× leaders lost. Each cell sizes once it has PATTERN_N copies."""
    from trading.paper_trader import PaperWallet
    w = PaperWallet(str(tmp_path / "r.db"), label="copy_trading", initial_balance=10_000)
    monkeypatch.setattr(ct, "_unique_wallets", lambda: [w])
    for _ in range(ct.PATTERN_N):
        _closed_copy(w, "short", "L1", 5.0, True)
        _closed_copy(w, "long", "L2", 20.0, False)
    rec = ct.records()
    assert rec["sides"]["short"]["avg_r"] > 0 > rec["sides"]["long"]["avg_r"] and rec["sides"]["short"]["n"] == ct.PATTERN_N
    assert rec["leverage"]["≤10x"]["avg_r"] > 0 > rec["leverage"][">10x"]["avg_r"]
    x, why = ct.pattern_multiplier(rec, "short", 5.0, "L1")
    assert x == pytest.approx(min(ct.PATTERN_UP ** 3, ct.PATTERN_X_RANGE[1])) and "shorts" in why and "this leader" in why
    x, why = ct.pattern_multiplier(rec, "long", 20.0, "L2")
    assert x == pytest.approx(ct.PATTERN_DOWN ** 2) and "this leader" not in why          # the leader's losing cell is probation, not a size
    assert ct.pattern_multiplier(rec, "long", 5.0, "L9")[0] == pytest.approx(ct.PATTERN_DOWN * ct.PATTERN_UP)
    assert ct.pattern_multiplier({"sides": {}, "leverage": {}, "leaders": {}}, "long", 5.0, "L9") == (1.0, f"no cell has {ct.PATTERN_N} copies yet")


def test_a_leaders_own_week_sizes_their_copies():
    kv.delete_sync(ct._week_key("LW"))
    try:
        assert ct.week_multiplier("LW", NOW)[0] == 1.0
        for i, roi in enumerate((0.10, 0.05, -0.02)):                                       # ROE at 5×: +2%, +1%, −0.4% price moves
            ct.note_close("LW", {"position_id": f"p{i}", "closed_ts": NOW - 3600 * (i + 1), "roi": roi, "leverage": 5.0, "symbol": "X", "side": "long"}, NOW)
        ct.note_close("LW", {"position_id": "p0", "closed_ts": NOW - 3600, "roi": 0.10, "leverage": 5.0}, NOW)          # the same close twice: once
        ct.note_close("LW", {"position_id": "old", "closed_ts": NOW - 8 * 86400, "roi": -0.5, "leverage": 5.0}, NOW)     # last week: off the week
        wk = ct.leader_week("LW", NOW)
        assert wk["n"] == 3 and wk["wins"] == 2 and wk["sum_move_pct"] == pytest.approx(2.6)
        assert ct.week_multiplier("LW", NOW)[0] == ct.PATTERN_UP
        for i in range(3):
            ct.note_close("LW", {"position_id": f"l{i}", "closed_ts": NOW - 60 * (i + 1), "roi": -0.25, "leverage": 5.0}, NOW)
        x, why = ct.week_multiplier("LW", NOW)                                              # 2 of 6 won, the sum negative
        assert x == ct.PATTERN_DOWN and "2/6 won" in why
    finally:
        kv.delete_sync(ct._week_key("LW"))


def _feed_book(tmp_path, monkeypatch, rows, prices):
    from experiments import books
    from trading import book_switches, monitor
    from trading.paper_trader import PaperWallet
    w = PaperWallet(str(tmp_path / "c.db"), label="copy_trading", initial_balance=10_000)
    monkeypatch.setitem(books.WALLETS, "copy_trading", w)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    monkeypatch.setattr(ct, "LOG_DB", str(tmp_path / "log.db"))
    monkeypatch.setattr(ct, "VARIANTS", {"base": ct.VARIANTS["base"]})
    monkeypatch.setattr(ct, "ALL_BOOKS", {(m, "base"): ct.BOOKS_BY_MARKET[m] for m in ct.MARKETS})
    for k in ("leaders", "skipped"):
        kv.delete_sync(ct._k(k))
    kv.delete_sync(ct._k("seen", "L1"))
    kv.set_sync(ct._k("leaders"), {"at": NOW, "leaders": [{"id": "L1", "nickname": "x", "nickname_en": "Leader One",
                                                           "profile": {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0}}]})

    async def positions(pid, pages=1):
        return rows

    async def live_price(sym):
        return prices[sym]
    monkeypatch.setattr(cc, "positions", positions)
    monkeypatch.setattr(monitor, "live_price", live_price)
    return w


def _row(pid, sym, lev):
    return {"position_id": pid, "symbol": sym, "side": "long", "opened_ts": NOW - 60, "closed_ts": None, "entry": 100.0, "close_price": None,
            "pnl": 0.0, "roi": 0.0, "leverage": lev, "qty": 1.0, "closed_qty": 0.0, "remaining": 1.0, "status": "Open"}


def test_high_leverage_leaders_are_not_copied_and_one_leader_holds_at_most_two_copies(tmp_path, monkeypatch):
    rows = [_row("p1", "ETHUSDT", 20.0), _row("p2", "SOLUSDT", 5.0), _row("p3", "BTCUSDT", 5.0), _row("p4", "XRPUSDT", 5.0)]
    w = _feed_book(tmp_path, monkeypatch, rows, {s: 100.0 for s in ("ETHUSDT", "SOLUSDT", "BTCUSDT", "XRPUSDT")})
    try:
        out = asyncio.run(ct.mirror(NOW))
        assert sorted(t["symbol"] for t in out["opened"]) == ["BTCUSDT", "SOLUSDT"]
        sk = {e["symbol"]: e["reason"] for e in ct.skipped()}
        assert "above the 10× leader-leverage cap" in sk["ETHUSDT"] and "leader already has 2 copies open" in sk["XRPUSDT"]
        t = out["opened"][0]
        assert t["pattern_x"] == 1.0 and t["week_x"] == 1.0 and t["notional_usd"] <= 10_000 * ct.MAX_LEG_PCT / 100 + 1
    finally:
        for k in ("leaders", "skipped"):
            kv.delete_sync(ct._k(k))
        kv.delete_sync(ct._k("seen", "L1"))
