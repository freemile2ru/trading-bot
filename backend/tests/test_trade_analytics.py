import pytest

from trading import overview, trade_analytics as ta


def _row(i, net, r, system="auto", symbol="BTCUSDT", direction="long", hold=3.0, mfe=None, gross=None):
    ts = 1_790_000_000 + i * 3600
    return {"id": str(i), "system": system, "symbol": symbol, "direction": direction, "status": "closed",
            "opened_ts": ts - hold * 60, "closed_ts": ts, "hold_min": hold, "net_pnl": net,
            "gross_pnl": gross if gross is not None else net + 1.0, "fees": 1.0, "funding": 0.0, "r": r,
            "notional": 1000.0, "exit_reason": "stop" if net < 0 else "take_profit", "mfe_r": mfe}


@pytest.fixture
def rows(monkeypatch):
    data = [_row(i, 20.0 if i % 3 == 0 else -8.0, 2.0 if i % 3 == 0 else -0.8, mfe=2.5 if i % 3 == 0 else 1.2,
                 direction="long" if i % 2 else "short") for i in range(60)]
    monkeypatch.setattr(overview, "all_rows", lambda: (data, {"auto": 10_000.0}))
    return data


def test_totals_breakdowns_and_distribution_agree(rows):
    a = ta.analytics({})
    assert a["trades"] == 60
    assert a["cumulative"]["total"][-1]["pnl"] == pytest.approx(sum(r["net_pnl"] for r in rows))
    assert sum(b["count"] for b in a["r_distribution"]) == 60
    assert sum(g["trades"] for g in a["by_side"]) == 60
    assert sum(d["trades"] for d in a["daily"]) == 60
    assert a["costs"]["fees"] == 60.0
    assert a["efficiency"]["reached_1r"] == 60 and a["efficiency"]["reached_1r_then_lost"] == 40


def test_filters_are_the_tables_filters(rows):
    assert ta.analytics({"direction": "long"})["trades"] == 30
    assert ta.analytics({"result": "win"})["trades"] == 20


def test_confidence_and_monte_carlo(rows):
    a = ta.analytics({})
    c = a["confidence"]
    # 20 × +20 and 40 × −8 → +1.33 per trade
    assert c["expectancy_usd"] == pytest.approx(1.33, abs=0.01)
    lo, hi = c["expectancy_ci"]
    assert lo < c["expectancy_usd"] < hi
    assert c["trades_needed"] and c["sqn"] > 0
    mc = a["monte_carlo"]
    assert mc["final"]["p5"] <= mc["final"]["p50"] <= mc["final"]["p95"]
    assert mc["max_drawdown"]["p50"] <= mc["max_drawdown"]["p99"]
    assert ta.analytics({}) ["monte_carlo"] == mc                  # deterministic for the same trades


def test_fee_scenarios_recost_the_same_trades(rows):
    s = {x["label"]: x for x in ta.analytics({})["fee_scenarios"]}
    assert s["As traded"]["net_pnl"] == pytest.approx(sum(r["net_pnl"] for r in rows))
    assert s["Zero fees"]["net_pnl"] == pytest.approx(sum(r["gross_pnl"] for r in rows))
    assert s["Maker on both sides (0.02%)"]["fees"] == pytest.approx(60 * 2 * 1000 * 0.0002)


def test_no_trades_is_not_an_error(monkeypatch):
    monkeypatch.setattr(overview, "all_rows", lambda: ([], {}))
    a = ta.analytics({})
    assert a["trades"] == 0 and a["monte_carlo"] is None and not a["confidence"]["enough"]


def test_history_insights_group_decision_inputs(monkeypatch):
    from trading import history
    trades = [{"id": str(i), "wallet": "auto", "symbol": "SOLUSDT", "direction": "long", "pnl_usd": 10.0 if i % 2 else -5.0,
               "r_multiple": 1.0 if i % 2 else -0.5, "setups_at_entry": ["trend", "mtf"], "conviction_at_entry": 0.5,
               "confirmed_by": "claude", "fa_bucket": "with", "entry_type": "market", "armed_minutes": 5, "timeframe": "1h",
               "outcome": "win", "entry_price": 1, "exit_price": 1} for i in range(12)]
    monkeypatch.setattr(history, "load", lambda wallet="all", ledger="both": (trades, 10_000))
    ins = history.insights()
    assert {g["key"] for g in ins["by_setup"]} == {"trend", "mtf"} and ins["by_setup"][0]["trades"] == 12
    assert ins["by_conviction"][0]["key"] == "0.45 – 0.6"
    assert ins["by_source"][0]["key"] == "Claude confirmed" and ins["by_source"][0]["avg_r"] == 0.25
    assert ins["by_armed"][0]["key"] == "under 15m"
    assert ins["best"][0]["r_multiple"] == 1.0 and ins["worst"][0]["r_multiple"] == -0.5



def test_retired_strategies_are_hidden_unless_asked_for(monkeypatch):
    live = [_row(i, 5.0, 0.5, system="auto") for i in range(10)]
    retired_rows = [_row(100 + i, -8.0, -0.8, system="scalper") for i in range(10)]
    monkeypatch.setattr(overview, "all_rows", lambda: (live + retired_rows, {"auto": 10_000.0, "scalper": 10_000.0}))
    assert ta.analytics({})["trades"] == 10
    assert ta.analytics({"include_retired": True})["trades"] == 20
    page = overview.trades({"status": "closed"})
    assert page["total"] == 10 and page["retired_hidden"] == 10 and "scalper" not in page["open_count"]
    assert overview.trades({"status": "closed", "include_retired": True})["total"] == 20
