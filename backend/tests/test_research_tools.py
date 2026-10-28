import random

import numpy as np
import pandas as pd
import pytest

from research import signal_scores, strategy_lab
from storage import signals as signal_log


# ── Strategy lab accounting ────────────────────────────────────────────────────

def _market(days=40, coins=("A", "B", "C", "D")):
    idx = pd.date_range("2025-01-01", periods=days, freq="D", tz="UTC")
    rng = np.random.default_rng(1)
    daily = pd.DataFrame({c: 100 * np.cumprod(1 + rng.normal(0, 0.02, days)) for c in coins}, index=idx)
    return daily, pd.DataFrame(0.0, index=idx, columns=daily.columns)


def test_costs_and_funding_are_charged():
    daily, funding = _market()
    w = pd.DataFrame(0.0, index=daily.index, columns=daily.columns)
    w["A"] = 1.0                                                    # hold A long throughout
    free = strategy_lab.run(w, daily, funding)
    assert free.iloc[1:].round(10).equals(daily["A"].pct_change().shift(-1).iloc[1:-1].dropna().round(10))
    funding["A"] = 0.001                                            # longs pay 0.1% a day
    paid = strategy_lab.run(w, daily, funding)
    assert (free - paid).iloc[1:].round(6).eq(0.001).all()
    assert free.iloc[0] == pytest.approx(daily["A"].pct_change().iloc[1] - strategy_lab.COST_PER_SIDE)   # opening trade pays


def test_rebalance_closes_coins_that_drop_out():
    idx = pd.date_range("2025-01-01", periods=6, freq="D", tz="UTC")
    score = pd.DataFrame({"A": [3, 3, 3, 1, 1, 1], "B": [2, 2, 2, 2, 2, 2], "C": [1, 1, 1, 3, 3, 3], "D": [0] * 6}, index=idx, dtype=float)
    w = strategy_lab._rebalanced(score, 0, 3, 1)
    assert w.loc[idx[2], "A"] == 0.5 and w.loc[idx[3], "A"] == 0.0 and w.loc[idx[3], "C"] == 0.5
    assert w.abs().sum(axis=1).eq(1.0).all()


def test_verdict_needs_the_whole_interval_above_zero():
    assert strategy_lab.verdict({"mean_daily_ci_bps": [0.5, 9]})[0] == "promising"
    assert strategy_lab.verdict({"mean_daily_ci_bps": [-0.5, 9]})[0] == "no_edge"
    assert strategy_lab.verdict({"mean_daily_ci_bps": [-9, -1]})[0] == "fails"


# ── Signal scoring ─────────────────────────────────────────────────────────────

def _seed(days: int, planted: bool):
    rnd = random.Random(5)
    rows, labels = [], []
    t0 = 1_780_000_000
    for d in range(days):
        for i in range(30):
            conv = rnd.uniform(-0.8, 0.8)
            move = (conv * 2 if planted else 0) + rnd.gauss(0, 1)     # % move
            rows.append({"symbol": f"C{i}USDT", "scanned_at": t0 + d * 86400 + i * 60, "price": 1.0, "conviction": conv,
                         "composite_score": conv, "ta_score": conv, "fa": {"score": 0.0, "components": {}, "features": {"noise": rnd.random()}}})
            labels.append(move)
    signal_log.add_scan_features(rows)
    stored = signal_log.scan_features()
    signal_log.save_labels([(r["id"], r["symbol"], r["ts"], 1.0, m, m, m, m, m, m) for r, m in zip(stored, labels)])


def test_planted_signal_is_found_and_noise_is_not():
    _seed(days=20, planted=True)
    s = signal_scores.scores()
    ic = {i["input"]: i for i in s["horizons"]["24h"]["inputs"]}
    assert ic["conviction"]["ic"] > 0.3 and ic["conviction"]["t"] > 5
    assert abs(ic["noise"]["ic"]) < 0.1
    top = s["horizons"]["24h"]["buckets"][-1]
    assert top["bucket"] == "0.45 and up" and top["avg_bps"] > 100


def test_few_days_report_no_t_stat():
    _seed(days=3, planted=True)
    ic = {i["input"]: i for i in signal_scores.scores()["horizons"]["24h"]["inputs"]}
    assert ic["conviction"]["t"] is None                           # "collecting": one swing would dominate


# ── Retired registry & regime ──────────────────────────────────────────────────

def test_retired_registry_is_complete():
    from experiments import retired
    items = retired.summary()["retired"]
    assert {"order_flow_scalper", "fa_signal", "hedge_straddle"} <= {e["key"] for e in items}
    for e in items:
        assert e["hypothesis"] and e["method"] and e["why"] and e["kept"] and e["removed"]


def test_regime_label():
    from trading import regime
    assert regime.label("up", 20) == "BTC uptrend · calm"
    assert regime.label("down", 60) == "BTC downtrend · volatile"
    assert regime.label(None, 20) is None


def test_failure_score_orders_by_severity():
    from experiments import retired
    bad = retired.score({"result_r": -0.5, "t": -8, "n": 300})
    mild = retired.score({"result_r": -0.13, "t": 3, "n": None})
    harmless = retired.score({"result_r": 0.2, "t": None, "n": 78})
    assert "money" not in bad                                     # paper/testnet money isn't scored
    assert bad["score"] == 100 and bad["grade"] == "severe"
    assert bad["score"] > mild["score"] > harmless["score"]
    assert retired.score({"result_r": None, "n": 79})["partial"]


def test_retired_list_is_ranked():
    from experiments import retired
    items = retired.summary()["retired"]
    scores = [e["score"]["score"] for e in items]
    assert scores == sorted(scores, reverse=True) and [e["rank"] for e in items] == list(range(1, len(items) + 1))
