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


def test_bookkeeping_closes_do_not_score():
    from autotrader import lanes
    admin = [{"lane": "weekly", "risk_usd": 10.0, "pnl_usd": -1.0, "close_reason": "resize", "closed_ts": i} for i in range(15)]
    real = [{"lane": "weekly", "risk_usd": 10.0, "pnl_usd": 5.0, "close_reason": "weekly_rebalance", "closed_ts": 100 + i} for i in range(3)]
    card = lanes.scorecard(admin + real)
    assert card["weekly"]["all"]["n"] == 3 and card["weekly"]["all"]["avg_r"] == 0.5
    assert lanes.multiplier("weekly", "down/calm", None, card)[0] == lanes.FULL          # three trades: still equal size, not measuring


# ── Sides and the net-exposure cap (2026-11-02) ────────────────────────────────

def test_a_lanes_losing_side_goes_to_measuring_size_while_the_other_side_stays_full():
    closed = ([_t("reversal", -0.6, regime="down/calm", i=i) for i in range(lanes.MIN_N)]
              + [_t("reversal", 0.9, regime="down/calm", i=100 + i) for i in range(lanes.MIN_N)])
    for i, t in enumerate(closed):
        t["direction"] = "long" if i < lanes.MIN_N else "short"
    card = lanes.scorecard(closed)
    assert card["reversal"]["by_side"]["long"]["avg_r"] == -0.6 and card["reversal"]["by_regime_side"]["down/calm|short"]["n"] == lanes.MIN_N
    m_long, why_long = lanes.multiplier("reversal", "down/calm", "NEARUSDT", card, "long")
    m_short, why_short = lanes.multiplier("reversal", "down/calm", "UNIUSDT", card, "short")
    assert m_long == lanes.MEASURE and "longs in down/calm" in why_long
    assert m_short == 1.0 and "shorts in down/calm" in why_short
    # without a side the regime cell (both sides together, +0.15R) decides as before
    assert lanes.multiplier("reversal", "down/calm", None, card)[0] == 1.0
    # a thin side cell in this regime falls back to the side's record over every regime BEFORE the mixed regime cell:
    # seven winning shorts (under MIN_N) are "equal size", never cut by the longs' losses
    thin = [{**t, "regime_at_entry": {"btc_trend": "up", "btc_vol_30d": 30.0}} for t in closed[:lanes.MIN_N]] + [dict(t) for t in closed[lanes.MIN_N:lanes.MIN_N + 7]]
    for t in thin[lanes.MIN_N:]:
        t["regime_at_entry"] = {"btc_trend": "up", "btc_vol_30d": 30.0}
    m, why = lanes.multiplier("reversal", "up/calm", "UNIUSDT", lanes.scorecard(thin), "short")
    assert m == 1.0 and "equal size" in why
    # a side's record over every regime is the last fallback
    assert lanes.multiplier("reversal", "up/wild", "ZZZUSDT", card, "long")[0] == lanes.MEASURE
    s = lanes.status(closed, [], {"btc_trend": "down", "btc_vol_30d": 30.0, "label": "BTC downtrend · calm"})
    rev = next(l for l in s["lanes"] if l["lane"] == "reversal")
    assert rev["sides"]["long"]["mult"] == lanes.MEASURE and rev["sides"]["short"]["mult"] == 1.0 and s["net_exposure_usd"] == 0


def test_net_exposure_cap_refuses_a_leg_that_widens_the_imbalance_and_allows_one_that_narrows_it():
    held = [{"symbol": "A", "direction": "long", "notional_usd": 9_000.0}, {"symbol": "B", "direction": "long", "notional_usd": 5_000.0},
            {"symbol": "C", "direction": "short", "notional_usd": 3_000.0}]
    assert lanes.net_exposure(held) == 11_000.0
    why = lanes.net_cap_reason("long", 5_000.0, held, 50_000.0)                       # 11k → 16k on a 12.5k cap
    assert why and "net exposure cap" in why and "net long $11,000" in why
    assert lanes.net_cap_reason("long", 1_000.0, held, 50_000.0) is None               # 12k: under the cap
    assert lanes.net_cap_reason("short", 20_000.0, held, 50_000.0) is None             # flips to −9k: narrower than the 11k it was
    assert lanes.net_cap_reason("short", 30_000.0, held, 50_000.0)                     # −19k: past the cap and wider than 11k
