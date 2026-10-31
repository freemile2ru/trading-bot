"""The autotrader's lanes: scorecard from the ledger, and the pre-registered sizing rule."""
from autotrader import lanes


def _t(lane, r, regime="up/calm", symbol="SOLUSDT", i=0, **extra):
    return {"lane": lane, "symbol": symbol, "risk_usd": 100.0, "pnl_usd": 100.0 * r, "closed_ts": 1_000 + i,
            "regime_at_entry": {"btc_trend": regime.split("/")[0], "btc_vol_30d": {"calm": 30, "normal": 55, "wild": 90}[regime.split("/")[1]]},
            **extra}


def test_regime_key_buckets_trend_and_volatility():
    assert lanes.regime_key({"btc_trend": "up", "btc_vol_30d": 35.0}) == "up/calm"
    assert lanes.regime_key({"btc_trend": "down", "btc_vol_30d": 55.0}) == "down/normal"
    assert lanes.regime_key({"btc_trend": "up", "btc_vol_30d": 88.0}) == "up/wild"
    assert lanes.regime_key(None) == "unknown" and lanes.regime_key({"btc_trend": "up"}) == "up/?"


def test_lane_of_places_older_trades_by_what_they_carry():
    assert lanes.lane_of({"event_id": "e1"}) == "events"
    assert lanes.lane_of({"unlock_id": "u1"}) == "unlocks"
    assert lanes.lane_of({"weekly_id": "w1"}) == "weekly"
    assert lanes.lane_of({"strategy": "auto"}) == "ta"
    assert lanes.lane_of({"lane": "weekly", "event_id": "e1"}) == "weekly"


def test_under_min_n_every_lane_is_equal_size():
    closed = [_t("ta", -1.0, i=i) for i in range(lanes.MIN_N - 1)]
    mult, why = lanes.multiplier("ta", "up/calm", "SOLUSDT", lanes.scorecard(closed))
    assert mult == 1.0 and "equal size" in why


def test_negative_cell_drops_to_measuring_and_positive_stays_full():
    closed = [_t("ta", -0.5, i=i) for i in range(lanes.MIN_N)] + [_t("events", 0.8, regime="up/calm", i=100 + i) for i in range(lanes.MIN_N)]
    card = lanes.scorecard(closed)
    assert lanes.multiplier("ta", "up/calm", "SOLUSDT", card)[0] == lanes.MEASURE
    assert lanes.multiplier("events", "up/calm", "ZECUSDT", card)[0] == 1.0
    # A different regime and a different coin have no record yet → equal size, whatever the other regime said.
    assert lanes.multiplier("ta", "down/wild", "LINKUSDT", card)[0] == 1.0
    # …but the coin's own record is the fallback when the regime cell is thin.
    assert lanes.multiplier("ta", "down/wild", "SOLUSDT", card)[0] == lanes.MEASURE


def test_coin_cell_is_the_fallback_when_the_regime_cell_is_thin():
    closed = [_t("ta", -0.4, regime="up/calm" if i % 2 else "down/calm", symbol="PEPEUSDT", i=i) for i in range(2 * lanes.MIN_N)]
    card = lanes.scorecard(closed)
    mult, why = lanes.multiplier("ta", "up/wild", "PEPEUSDT", card)
    assert mult == lanes.MEASURE and "on PEPEUSDT" in why


def test_pair_trades_score_the_coin_leg_with_its_hedge_and_only_the_last_cell_n():
    closed = []
    for i in range(lanes.CELL_N + 5):
        closed.append(_t("events", 1.0, i=2 * i, event_id=f"e{i}", event_leg="coin"))
        closed.append({**_t("events", -0.3, i=2 * i + 1, event_id=f"e{i}", event_leg="hedge", symbol="BTCUSDT"), "risk_usd": 100.0})
    card = lanes.scorecard(closed)
    c = card["events"]["all"]
    assert c["n"] == lanes.CELL_N and c["avg_r"] == 0.7           # +1.0 coin leg, −0.3 hedge → +0.7R a pair; hedges score nothing alone


def test_status_reports_every_lane_with_the_rule():
    s = lanes.status([], [{"lane": "weekly", "symbol": "ETHUSDT"}], {"btc_trend": "up", "btc_vol_30d": 50.0, "label": "BTC uptrend · normal"})
    assert s["regime"] == "up/normal" and [l["lane"] for l in s["lanes"]] == list(lanes.LANES)
    weekly = next(l for l in s["lanes"] if l["lane"] == "weekly")
    assert weekly["open"] == 1 and weekly["mult"] == 1.0 and "equal size" in weekly["why"]
    assert "Set 2026-10-29" in s["rule"]
