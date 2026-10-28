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
    for k in ("leaders", "skipped", "seen"):
        kv.delete_sync(ct._k(k) if k != "seen" else ct._k("seen", "L1"))
    kv.set_sync(ct._k("leaders"), {"at": NOW, "leaders": [{"id": "L1", "nickname": "x", "nickname_en": "Leader One",
                                                           "profile": {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0}}]})
    feed = {"rows": [{"position_id": "p1", "symbol": "ETHUSDT", "side": "long", "opened_ts": NOW - 1800, "closed_ts": None, "entry": 3000.0,
                      "close_price": None, "pnl": 0.0, "roi": 0.0, "leverage": 10.0, "qty": 2.0, "closed_qty": 0.0, "remaining": 1.0, "status": "Open"},
                     {"position_id": "p2", "symbol": "SOLUSDT", "side": "short", "opened_ts": NOW - 1800, "closed_ts": None, "entry": 200.0,
                      "close_price": None, "pnl": 0.0, "roi": 0.0, "leverage": 20.0, "qty": 10.0, "closed_qty": 0.0, "remaining": 1.0, "status": "Open"}]}
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
    assert t["leader_position_id"] == "p1" and t["leverage"] == 5 and t["leader"] == "Leader One"
    assert t["notional_usd"] == pytest.approx(500, rel=0.01)
    sk = ct.skipped()
    assert len(sk) == 1 and sk[0]["symbol"] == "SOLUSDT" and sk[0]["reason"].startswith("too late")
    # the same positions again: nothing new, nothing doubled
    assert asyncio.run(ct.mirror(NOW + 60))["opened"] == []
    assert len(w.get_portfolio()["open_trades"]) == 1
    # the leader halves ETH → we close; SOL closes at 190 → the skipped trade's outcome is filled in (+3.06% from our mark)
    feed["rows"][0] = {**feed["rows"][0], "closed_qty": 1.2, "remaining": 0.4, "status": "Partially Closed"}
    feed["rows"][1] = {**feed["rows"][1], "closed_ts": NOW + 3600, "close_price": 190.0, "remaining": 0.0, "roi": 1.0}
    out = asyncio.run(ct.mirror(NOW + 120))
    assert [c["close_reason"] for c in out["closed"]] == ["leader_reduced"]
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
