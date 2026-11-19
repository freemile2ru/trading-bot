import time

from trading import verdicts


def test_trade_book_verdicts_follow_the_registered_rules():
    assert verdicts.judge([0.5] * 10)["status"] == "collecting"
    assert verdicts.judge([1.0, 0.5, -0.2] * 20)["status"] == "pass"
    assert verdicts.judge([-1.0, -0.5, 0.2] * 20)["status"] == "fail"
    assert verdicts.judge([1.0, -1.0] * 25)["status"] == "unclear"
    v = verdicts.judge([1.0, -1.0] * 61)
    assert v["status"] == "no_edge" and v["trades"] == 122
    assert v["ci"][0] < 0 < v["ci"][1]


def test_carry_verdict_compares_funding_with_the_holdout():
    now = time.time()
    pos = lambda days, funding: [{"symbol": "BTCUSDT", "notional": 10_000, "opened_ts": now - days * 86400, "funding": funding}]
    assert verdicts.judge_carry(pos(10, 1.0))["status"] == "collecting"
    assert verdicts.judge_carry(pos(61, 61 * 0.8))["status"] == "pass"        # 0.8 bps/day vs 1.0 expected
    assert verdicts.judge_carry(pos(61, -2.0))["status"] == "fail"

    assert verdicts.judge_carry(pos(61, 61 * 0.2))["status"] == "unclear"


def test_books_catalog_carries_a_verdict():
    from trading import book_switches
    books = {b["name"]: b for b in book_switches.catalog()["books"]}
    assert books["main"]["verdict"] is None
    assert books["weekly_rs"]["verdict"]["status"] == "collecting"
    assert books["carry"]["verdict"]["rule"] == "carry"


def test_a_books_verdict_counts_only_trades_opened_under_its_current_rule(monkeypatch):
    monkeypatch.setattr(verdicts, "rule_since", lambda book: 100.0 if book == "cascade" else 0.0)
    rows = [{"system": "cascade", "status": "closed", "r": -1.0, "opened_ts": 50.0, "closed_ts": 60.0}] * 20 + \
           [{"system": "cascade", "status": "closed", "r": 0.5, "opened_ts": 150.0, "closed_ts": 160.0}] * 45
    v = verdicts.for_books(rows)["cascade"]
    assert v["trades"] == 45 and v["avg_r"] == 0.5 and v["status"] == "pass"


def test_rule_since_is_set_for_the_books_whose_rule_changed():
    assert verdicts.rule_since("burst_up") == 0.0 and verdicts.rule_since("main") == 0.0
    assert verdicts.rule_since("copy_trading") == verdicts.rule_since("copy_stocks") > 1.79e9 and verdicts.rule_since("cascade") > 1.79e9
    assert verdicts.rule_since("copy_crypto_wide") == verdicts.rule_since("copy_trading")     # the variants took the caps and the sizing too


def test_carry_q_is_judged_on_its_locked_basis():
    def pos(days, pnl, locked=4.5): return [{"symbol": "BTCUSDT", "notional": 10_000.0, "days": days, "pnl": pnl, "basis_locked_pct": locked}]
    assert verdicts.judge_carry_q(pos(20, 30.0))["status"] == "collecting"
    full = verdicts.judge_carry_q(pos(61, 10_000 * 0.045 * 61 / 365))                     # exactly the locked rate
    assert full["status"] == "pass" and full["basis_bps_day"] == full["locked_bps_day"]
    assert verdicts.judge_carry_q(pos(61, 10_000 * 0.045 * 61 / 365 * 0.3))["status"] == "unclear"
    assert verdicts.judge_carry_q(pos(61, -5.0))["status"] == "fail"
    assert verdicts.judge_carry_q(pos(130, 10_000 * 0.045 * 130 / 365 * 0.3))["status"] == "pass"
    assert verdicts.judge_carry_q([])["status"] == "collecting"


def test_the_sized_line_turns_a_pass_into_oversized_when_the_dollar_drawdown_broke_the_floor(monkeypatch):
    # 2026-11-19: the R rule strips size out; the same trades in dollars at the plan's size decide whether it is deployable
    day = 86_400
    s = verdicts.sized([(1 * day, -300.0), (1 * day + 10, -300.0), (2 * day, 500.0), (3 * day, -2_100.0)], 50_000.0)
    assert s["worst_day"] == "1970-01-04" and s["worst_day_usd"] == -2_100.0 and s["max_dd_usd"] == -2_200.0
    assert s["worst_day_pct"] == -4.2 and s["max_dd_pct"] == -4.4 and s["ok"] is True and s["day_ok"] is False and s["fit_x"] == 1.0
    s = verdicts.sized([(1 * day, -1_500.0), (2 * day, -1_500.0)], 50_000.0)
    assert s["ok"] is False and s["max_dd_pct"] == -6.0 and s["fit_x"] == 0.83
    assert verdicts.sized([(1 * day, -1_500.0)], None)["ok"] is None and verdicts.sized([], 1.0) is None
    good = [1.0, 0.5, -0.2] * 20
    assert verdicts.judge(good)["status"] == "pass" and verdicts.judge(good)["sized"] is None
    v = verdicts.judge(good, s)
    assert v["status"] == "oversized" and "0.83×" in v["note"] and v["sized"]["max_dd_pct"] == -6.0
    assert verdicts.judge(good, verdicts.sized([(1 * day, -500.0)], 50_000.0))["status"] == "pass"      # inside the floor: the R verdict stands
    assert verdicts.judge([-1.0, -0.5, 0.2] * 20, s)["status"] == "fail"                                 # size never rescues a failing signal
    # for_books: the plan's size comes from the capital plan; the dollars are the rows' own net P&L
    from trading import capital
    monkeypatch.setattr(capital, "PLAN", {"burst_up": 500.0})
    monkeypatch.setattr(capital, "plan", lambda: {"burst_up": 500.0})
    rows = [{"system": "burst_up", "status": "closed", "r": 1.0, "net_pnl": 5.0, "opened_ts": 1e9, "closed_ts": 1e9 + 60}] * 40 + \
           [{"system": "burst_up", "status": "closed", "r": -1.0, "net_pnl": -40.0, "opened_ts": 1e9, "closed_ts": 1e9 + 120}]
    v = verdicts.for_books(rows)["burst_up"]
    assert v["status"] == "oversized" and v["sized"]["plan_usd"] == 500.0 and v["sized"]["max_dd_pct"] == -8.0 and v["sized"]["fit_x"] == 0.62
