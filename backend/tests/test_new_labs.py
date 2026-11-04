"""The 2026-11-04 labs: fade the headline, one-hour continuation — their pure parts."""
from research import continuation_lab as cl
from research import news_fade_lab as nf


def test_news_fade_rules_sign_the_move_the_right_way():
    bullish = {"direction": 0.8, "r_24h": 2.0, "r_4h": 0.5}
    bearish = {"direction": -0.8, "r_24h": -2.0, "r_4h": 0.5}
    neutral = {"direction": 0.1, "r_24h": 1.0, "r_4h": 0.5}
    assert nf.signed("short_all", bullish, "r_24h") == -2.0 and nf.signed("short_all", bearish, "r_24h") == 2.0
    assert nf.signed("fade_call", bullish, "r_24h") == -2.0 and nf.signed("fade_call", bearish, "r_24h") == -2.0   # the call was right both times: fading lost
    assert nf.signed("follow_call", bullish, "r_24h") == 2.0 and nf.signed("follow_call", bearish, "r_24h") == 2.0
    assert nf.signed("fade_bullish", bearish, "r_24h") is None and nf.signed("fade_call", neutral, "r_24h") is None
    assert nf.evaluate([{**bullish, "seen": 100.0}], "short_all", "r_24h")["avg_pct"] == -2.16          # costs come off


def test_continuation_slots_rank_the_sweep_and_charge_the_round_trip():
    sweep = [{"ts": 1.0 + i, "symbol": f"C{i}", "ret_1h": i - 6.0, "fwd_4h": (i - 6.0) / 2, "xs_4h": (i - 6.0) / 4} for i in range(13)]
    longs_shorts = cl.legs(sweep, "top_bottom", "fwd_4h")
    assert len(longs_shorts) == 2 * cl.TOP_K
    assert longs_shorts[:cl.TOP_K] == [3.0 - 0.16, 2.5 - 0.16, 2.0 - 0.16][::-1] or sorted(longs_shorts[:cl.TOP_K]) == sorted([2.84, 2.34, 1.84])
    assert sorted(longs_shorts[cl.TOP_K:]) == sorted([3.0 - 0.16, 2.5 - 0.16, 2.0 - 0.16])              # shorting the weakest, which fell
    assert cl.legs(sweep, "top_long", "fwd_4h") == longs_shorts[:cl.TOP_K]
    rows = [dict(r, ts=r["ts"] + k * cl.SLOT_S) for k in range(3) for r in sweep]
    assert len(cl.slots(rows)) == 3 and all(len(s) == 13 for s in cl.slots(rows))
    d = cl.deciles(sweep)
    assert d[0]["fwd_4h_bps"] < 0 < d[-1]["fwd_4h_bps"]
