"""Degenerate candle series must not put NaN into a scan (FastAPI refuses to serialise NaN)."""
import json
import math

from analysis.technical import compute_ta, compute_multi_timeframe_ta


def _candles(closes):
    return [{"open_time": 1_700_000_000_000 + i * 900_000, "open": c, "high": c * 1.001, "low": c * 0.999,
             "close": c, "volume": 100.0} for i, c in enumerate(closes)]


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def _assert_finite(ta):
    bad = [v for v in _walk(ta) if isinstance(v, float) and not math.isfinite(v)]
    assert not bad, bad
    json.dumps(ta, allow_nan=False)          # exactly what the API does


def test_rising_only_series_gives_rsi_100_not_nan():
    ta = compute_ta(_candles([100 + i for i in range(80)]), "1h")
    _assert_finite(ta)
    assert ta["rsi"] == 100.0
    assert ta["signal_label"] in ("neutral", "sell", "strong_sell", "buy", "strong_buy")


def test_flat_series_is_finite_and_neutral():
    flat = [{"open_time": 1_700_000_000_000 + i * 900_000, "open": 50.0, "high": 50.0, "low": 50.0,
             "close": 50.0, "volume": 10.0} for i in range(80)]
    ta = compute_ta(flat, "15m")
    _assert_finite(ta)
    assert ta["bollinger_bands"]["pct_b"] == 0.5
    assert ta["stochastic"]["k"] == 50.0
    assert abs(ta["signal_score"]) <= 1


def test_multi_timeframe_on_degenerate_inputs_serialises():
    out = compute_multi_timeframe_ta({"1h": _candles([100 + i for i in range(80)]),
                                      "4h": _candles([100.0] * 80)})
    _assert_finite(out)
    assert -1 <= out["consensus_score"] <= 1
