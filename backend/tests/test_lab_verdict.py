"""The Lab's standalone verdict, with the 2026-11-07 amendment: a straddling range at 40 trades extends to 90 when the
bootstrap mostly favours the book, stops when it mostly doesn't, and stays "inconclusive" in between."""
import random

from experiments import lab


def _closed(pnls):
    return [{"status": "closed", "net_pnl": p, "r": p / 50.0, "closed_ts": float(i)} for i, p in enumerate(pnls)]


def test_a_straddling_range_at_forty_extends_when_the_bootstrap_mostly_favours_the_book():
    rng = random.Random(1)
    small_edge = [30.0, -20.0, 28.0, -22.0, 26.0, -18.0, 32.0, -24.0] * 5 + [25.0, 10.0]        # 42 trades, about +$5 a trade, noisy
    v = lab._verdict_single(_closed(small_edge), rng)
    assert v["trades"] == 42 and v["expectancy_ci"][0] < 0 < v["expectancy_ci"][1]
    assert v["positive_share"] >= lab.EXTEND_SHARE
    assert v["status"] == "extend" and v["extend_to"] == lab.EXTEND_TRADES and "to 90" in v["label"]


def test_a_straddling_range_that_mostly_loses_is_no_edge_at_forty():
    rng = random.Random(1)
    mostly_losing = [-30.0, 20.0, -28.0, 22.0, -26.0, 18.0, -32.0, 24.0] * 5 + [-25.0, -10.0]
    v = lab._verdict_single(_closed(mostly_losing), rng)
    assert v["expectancy_ci"][0] < 0 < v["expectancy_ci"][1] and v["positive_share"] <= lab.STOP_SHARE
    assert v["status"] == "no_edge" and "positive" in v["label"]


def test_the_middle_stays_inconclusive_and_fewer_than_forty_still_collects():
    rng = random.Random(1)
    coin_flip = [50.0, -50.0] * 21
    v = lab._verdict_single(_closed(coin_flip), rng)
    assert v["status"] == "inconclusive"
    assert lab._verdict_single(_closed([50.0, -40.0] * 10), rng)["status"] == "collecting"


def test_a_clear_edge_or_a_clear_loss_is_judged_as_before():
    rng = random.Random(1)
    assert lab._verdict_single(_closed([40.0, 30.0, -10.0] * 14), rng)["status"] == "edge"
    assert lab._verdict_single(_closed([-40.0, -30.0, 10.0] * 14), rng)["status"] == "no_edge"
