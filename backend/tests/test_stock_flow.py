"""Stock flow shadow: the cross-sectional score, a long/short slot net of costs and funding, and the report's verdict."""
import pytest

from experiments import stock_flow as sf
from storage import state as kv


def _rows(n=12):
    return [{"symbol": f"S{i}", "price": 100.0, "taker_buy_sell_24h": 1.0 + i / 10, "ret_24h": float(i), "funding_rate": None} for i in range(n)]


def test_score_ranks_each_reading_signed_and_skips_missing_ones():
    sc = sf.score(_rows(), {"taker_buy_sell_24h": 1.0, "funding_rate": -1.0})
    assert max(sc, key=sc.get) == "S11" and min(sc, key=sc.get) == "S0"          # funding missing everywhere: ignored
    flipped = sf.score(_rows(), {"taker_buy_sell_24h": -1.0})
    assert max(flipped, key=flipped.get) == "S0"


def test_a_slot_is_long_the_top_short_the_bottom_net_of_costs_and_funding():
    rows = _rows()
    fwd = {f"S{i}": (1.0 if i >= 7 else -1.0 if i < 5 else 0.0, 0.01) for i in range(12)}   # top five +1%, bottom five −1%
    r = sf.slot_return(rows, fwd, {"taker_buy_sell_24h": 1.0})
    assert r == pytest.approx((1.0 - 0.01 + 1.0 + 0.01) / 2 - 2 * sf.COST_BPS / 100)
    assert sf.slot_return(rows[:8], fwd, {"taker_buy_sell_24h": 1.0}) is None              # too few names for 5/5


def test_the_report_measures_until_the_verdict_days(monkeypatch):
    kv.delete_sync(sf._k("index"))
    marks = [sf.SINCE + i * sf.MARK_S for i in range(12)]
    for i, m in enumerate(marks):
        kv.set_sync(sf._k("snap", str(int(m))), {"mark": m, "rows": _rows(), "result": {"crypto": -0.1, "stock": 0.2 + 0.01 * i, "names": 12}})
    kv.set_sync(sf._k("index"), [int(m) for m in marks])
    rep = sf.report()
    assert rep["scored_marks"] == 12 and rep["scores"]["stock"]["verdict"] == "measuring"
    assert rep["scores"]["stock"]["all"]["mean_pct"] > 0 > rep["scores"]["crypto"]["all"]["mean_pct"]
    assert rep["scores"]["stock"]["daily"]["n"] == 2                                       # 00:00 UTC marks only
