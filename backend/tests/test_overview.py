"""Portfolio metrics: profit factor vs payoff, drawdown, streaks, exposure."""

from pytest import approx

from trading import overview


def _t(net, ts, direction="long", gross=None, risk=10.0, status="closed", notional=100.0):
    return {"status": status, "net_pnl": net, "gross_pnl": gross if gross is not None else net + 1, "fees": 1.0,
            "funding": 0.0, "closed_ts": ts, "opened_ts": (ts or 0) - 60, "hold_min": 1.0, "direction": direction,
            "symbol": "XUSDT", "r": net / risk, "risk_usd": risk, "notional": notional, "margin": 10.0}


def test_profit_factor_is_gross_wins_over_gross_losses():
    rows = [_t(30, 1), _t(-10, 2), _t(-10, 3), _t(10, 4)]
    m = overview.metrics(rows, 1000, [(1, 1000), (2, 1030), (3, 1010), (4, 1020)])
    assert m["profit_factor"] == approx(2.0)          # 40 / 20 — not avg win / avg loss (which is 2.0 too here…)
    assert m["payoff_ratio"] == approx(2.0)           # 20 / 10
    rows2 = [_t(30, 1), _t(-10, 2)]                    # …so check a case where they differ
    m2 = overview.metrics(rows2 + [_t(-10, 3), _t(-10, 4)], 1000, [])
    assert m2["profit_factor"] == approx(1.0) and m2["payoff_ratio"] == approx(3.0)
    assert m2["breakeven_win_rate"] == approx(25.0)


def test_drawdown_and_streaks():
    series = [(0, 100), (3600, 120), (7200, 90), (10800, 110), (14400, 130)]
    dd = overview._drawdown(series)
    assert dd["max_drawdown_usd"] == 30 and dd["max_drawdown_pct"] == approx(25.0)
    assert dd["current_drawdown_pct"] == 0 and dd["longest_drawdown_h"] == approx(3.0)   # peak at 1h → new high at 4h
    s = overview._streaks([_t(1, 1), _t(1, 2), _t(-1, 3), _t(-1, 4), _t(-1, 5)])
    assert s == {"current_streak": -3, "longest_win_streak": 2, "longest_loss_streak": 3}


def test_exposure_counts_open_positions_only():
    rows = [_t(5, None, status="open", notional=300), _t(-2, None, "short", status="open", notional=100), _t(10, 5)]
    m = overview.metrics(rows, 1000, [(1, 1000), (2, 1010)])
    e = m["exposure"]
    assert (e["long_notional"], e["short_notional"], e["net_notional"]) == (300, 100, 200)
    assert e["gross_leverage"] == approx(0.4, abs=0.01) and e["open_risk_usd"] == 20
    assert m["unrealized_pnl"] == 3 and m["trades"] == 1
