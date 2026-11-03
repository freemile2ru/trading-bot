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
