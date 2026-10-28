"""Direction symmetry: a chart and its upside-down mirror must score as opposites.

Every signal so far has been a long. Most of that was the market, but a mirror test also
found the scoring itself leaned bullish (Fibonacci ties went to support, a Bollinger
squeeze scored as bullish, on-chain activity and liquidity/supply added to direction).
These tests keep any such lean from creeping back in.
"""

import numpy as np
import pytest

from analysis.scoring import build_signal_package
from analysis.technical import compute_ta, fibonacci_levels, ohlcv_to_df
from scanner.fundamentals import compute_fundamentals


def _candles(seed: int, n: int = 200, drift: float = 0.0) -> list[dict]:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.002, n)))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.001, (2, n))) * close
    return [{"open_time": i * 900_000, "open": o, "high": max(o, c) + w1, "low": min(o, c) - w2,
             "close": c, "volume": v}
            for i, (o, c, w1, w2, v) in enumerate(zip(open_, close, wick[0], wick[1],
                                                      rng.lognormal(10, 0.5, n)))]


def _mirror(cs: list[dict]) -> list[dict]:
    """Reflect prices about the window's midpoint: rallies become sell-offs, volume unchanged."""
    k = max(c["high"] for c in cs) + min(c["low"] for c in cs)
    return [{**c, "open": k - c["open"], "high": k - c["low"], "low": k - c["high"], "close": k - c["close"]}
            for c in cs]


def test_ta_scores_have_no_directional_lean():
    sums = []
    for seed in range(150):
        cs = _candles(seed, drift=0.0004 * (seed % 3 - 1))
        a, b = compute_ta(cs)["signal_score"], compute_ta(_mirror(cs))["signal_score"]
        sums.append(a + b)
    # The mirror can't preserve %-based thresholds exactly (price sits at a different level),
    # so a few windows land either side of one; the average and the typical window can't lean.
    assert abs(np.mean(sums)) / 2 < 0.01     # lean per score, on a −1…+1 scale
    assert np.percentile(np.abs(sums), 90) < 0.01


@pytest.mark.parametrize("last,near", [(100.30, "support"), (100.45, "resistance")])
def test_fib_tie_goes_to_the_closer_level(last, near):
    """Levels here sit ~0.3% apart, so price is within 1% of both — only the closer counts.
    Swing 99–101 → levels …100.0, 100.236, 100.528…"""
    bars = [{"open_time": i * 60_000, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
            for i in range(60)]
    bars[-1] = {**bars[-1], "close": last}
    f = fibonacci_levels(ohlcv_to_df(bars))
    assert f["near_support"] is (near == "support")
    assert f["near_resistance"] is (near == "resistance")


def _coin(h1, h24, d7, **kw):
    return {"symbol": "X", "percent_change_1h": h1, "percent_change_24h": h24, "percent_change_7d": d7,
            "market_cap": 1e9, "volume_24h": kw.get("volume", 2e8), "volume_change_24h": kw.get("vol_chg", 80),
            "circulating_supply": 95, "max_supply": 100}


def test_fa_is_antisymmetric():
    btc, mkt = {"symbol": "BTC", "percent_change_24h": 0.0}, {"market_cap_change_24h_pct": 0.0, "btc_dominance_pct": 55}
    up = compute_fundamentals(_coin(1.0, 6.0, 15.0), btc, mkt)
    down = compute_fundamentals(_coin(-1.0, -6.0, -15.0), btc, mkt)
    assert up["score"] == pytest.approx(-down["score"])
    assert up["label"] == "bullish" and down["label"] == "bearish"


def test_liquidity_and_supply_scale_but_never_point():
    btc, mkt = {"symbol": "BTC", "percent_change_24h": 0.0}, {"market_cap_change_24h_pct": 0.0, "btc_dominance_pct": 55}
    flat = compute_fundamentals(_coin(0, 0, 0), btc, mkt)
    assert flat["quality"] > 0.4 and flat["score"] == 0     # excellent quality, no direction → 0
    liquid = compute_fundamentals(_coin(-1, -6, -15), btc, mkt)
    thin = compute_fundamentals(_coin(-1, -6, -15, volume=5e6, vol_chg=0), btc, mkt)
    assert liquid["score"] < thin["score"] < 0              # a liquid sell-off is *more* bearish, not less


def test_neutral_inputs_give_a_zero_composite():
    """No input should contribute direction by merely existing (the old on-chain score did)."""
    pkg = build_signal_package(symbol="BTCUSDT", ta_result={"timeframes": {}, "consensus_score": 0.0},
                               news={}, macro={}, fear_greed={"value": 50}, social={}, coingecko={})
    assert pkg["composite_score"] == 0
    assert "onchain" not in pkg["component_scores"]
