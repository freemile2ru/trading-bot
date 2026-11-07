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
