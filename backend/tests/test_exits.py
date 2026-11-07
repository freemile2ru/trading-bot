"""Exit policies: trailing, breakeven-plus-costs, partials, stale, legacy, and the shadow book."""


import pytest

from trading import exits
from trading.paper_trader import MINUTE_MS, PaperWallet


@pytest.fixture
def wallet(tmp_path):
    w = PaperWallet(str(tmp_path / "w.json"), label="test")
    w.reset_portfolio(10_000)
    return w


def _open(wallet, policy="trail", timeframe="1h"):
    # long, entry 100, stop 98 (1R = 2), TP1 103 (1.5R), TP2 105 (2.5R)
    return wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1, timeframe=timeframe,
                             extra={"exit_policy": policy})


def _bars(start_ms, rows):
    return [{"open_time": start_ms + i * MINUTE_MS, "open": o, "high": h, "low": l, "close": c}
            for i, (o, h, l, c) in enumerate(rows)]


def _walk(wallet, t, rows, now=None, skip=0):
    start = (int(t["opened_ts"] * 1000) // MINUTE_MS + 1 + skip) * MINUTE_MS
    bars = _bars(start, rows)
    return wallet.process_paths({t["symbol"]: {"bars": bars, "price": rows[-1][3]}}, now=now)


def test_partial_trail_banks_half_and_moves_stop_past_costs(wallet):
    t = _open(wallet, "partial_trail")
    _walk(wallet, t, [(100, 103.2, 99.9, 102.5)])
    live = wallet.get_portfolio()["open_trades"][0]
    assert live["quantity"] == pytest.approx(t["quantity"] / 2, rel=1e-6)
    assert live["realized_pnl_usd"] > 0
    assert live["stop_loss"] > live["entry_price"]                     # breakeven + costs, not exact entry


def test_breakeven_plus_costs_exit_is_not_a_loss(wallet):
    t = _open(wallet, "trail")
    ev = _walk(wallet, t, [(100, 103.2, 99.9, 102.5), (102.5, 102.6, 99.0, 99.5)])
    closed = ev[-1]["trade"]
    assert closed["close_reason"] in ("breakeven", "trailing_stop")
    assert closed["pnl_usd"] >= -1.0                                   # costs covered (to within rounding)


def test_trailing_stop_ratchets_and_exits(wallet):
    t = _open(wallet, "trail")
    ev = _walk(wallet, t, [
        (100, 103.2, 99.9, 103.0),      # TP1
        (103, 108.0, 102.9, 107.5),     # runs to 4R — trail 1R behind: 106
        (107.5, 107.6, 105.5, 105.8),   # pulls back through 106
    ])
    closed = ev[-1]["trade"]
    assert closed["close_reason"] == "trailing_stop"
    assert closed["exit_trigger_price"] == pytest.approx(108.0 - t["r_unit"])   # 1R behind the peak
    assert closed["pnl_usd"] > 0


def test_no_fixed_target_under_trail(wallet):
    t = _open(wallet)
    ev = _walk(wallet, t, [(100, 103.2, 99.9, 103), (103, 106, 102.9, 105.9)])  # through TP2
    assert not any(e["type"] == "closed" for e in ev)


def test_stop_moves_only_from_the_next_bar(wallet):
    # TP1 and a dip below entry in the same bar: the old stop (98) still applies to that bar,
    # and the live price afterwards (102) is above the newly trailed stop (~101.2).
    t = _open(wallet, "trail")
    ev = _walk(wallet, t, [(100, 103.2, 98.5, 102)])
    assert not any(e["type"] == "closed" for e in ev)


def test_stale_trade_is_cut_but_a_working_one_is_not(wallet):
    t = _open(wallet, "trail_stale", timeframe="15m")               # stale after 3h
    later = t["opened_ts"] + 3 * 3600 + 120
    ev = _walk(wallet, t, [(100, 100.5, 99.5, 100.2)], now=later)  # peak +0.25R < 0.5R
    assert ev and ev[-1]["trade"]["close_reason"] == "stale"


def test_working_trade_survives_the_stale_deadline(wallet):
    t = _open(wallet, "trail_stale", timeframe="15m")
    _walk(wallet, t, [(100, 101.5, 99.8, 101.2)])                   # +0.75R peak
    later = t["opened_ts"] + 3 * 3600 + 120
    ev = wallet.process_paths({"SOLUSDT": {"bars": [], "price": 101.0}}, now=later)
    assert not any(e["type"] == "closed" for e in ev)


def test_legacy_policy_reproduces_old_behaviour(wallet):
    t = _open(wallet, "legacy")
    assert t["expires_at"] - t["opened_ts"] == pytest.approx(exits.LEGACY_EXPIRY["1h"])
    ev = _walk(wallet, t, [(100, 103.2, 99.9, 103), (103, 105.2, 102.9, 105)])
    assert ev[-1]["trade"]["close_reason"] == "take_profit_2"


def test_shadow_book_runs_every_policy_independently(wallet):
    t = _open(wallet)
    assert len(wallet.shadow_starts()) == 1
    # Close the real trade by hand; shadows must keep going on their own.
    wallet.close_trade_manually(t["id"], 100.0)
    _walk(wallet, t, [(100, 100.2, 97.5, 97.8)])                     # everyone's original stop
    report = wallet.shadow_report()
    closed = {r["policy"]: r for r in report["results"]}
    assert set(closed) == set(exits.SHADOW_POLICIES)
    assert all(r["reason"] == "stop_loss" and r["r"] < -0.95 for r in closed.values())
    assert report["open_shadows"] == 0


def test_move_stop_only_tightens(wallet):
    t = _open(wallet)
    assert wallet.move_stop(t["id"], 97.0, "loosen") is None        # looser — refused
    assert wallet.move_stop(t["id"], 100.5, "through price") is None  # above current price — refused
    moved = wallet.move_stop(t["id"], 99.0, "tighten")
    assert moved["stop_loss"] == 99.0


def test_default_policy_has_no_stale_cut(wallet):
    t = _open(wallet, None, timeframe="15m")
    assert t["exit_policy"] == exits.DEFAULT_POLICY == "protect"
    later = t["opened_ts"] + 3 * 3600 + 120
    ev = _walk(wallet, t, [(100, 100.5, 99.5, 100.2)], now=later)
    assert not any(e["type"] == "closed" for e in ev)


def test_declined_entries_are_tracked_but_kept_out_of_the_exit_comparison(wallet):
    t = _open(wallet)
    wallet.annotate(t["id"], claude_verdict="approved")
    v = wallet.add_virtual("ETHUSDT", "long", 100, 98, 103, 105, "1h", 1.0,
                           {"claude_verdict": "declined", "claude_confidence": 62, "exit_policy": exits.DEFAULT_POLICY})
    assert v and wallet.get_portfolio()["open_trades"][0]["symbol"] == "SOLUSDT"   # not a real position
    start = (int(v["opened_ts"] * 1000) // MINUTE_MS + 1) * MINUTE_MS
    wallet.process_paths({"ETHUSDT": {"bars": _bars(start, [(100, 100.2, 97.5, 97.8)]), "price": 97.8}})
    report = wallet.shadow_report()
    cal = report["calibration"]
    assert cal["by_verdict"]["declined"]["trades"] == 1
    assert cal["by_verdict"]["declined"]["avg_r"] < -0.9
    assert next(b for b in cal["by_confidence"] if b["band"] == "60–64%")["trades"] == 1
    assert all(p["trades"] == 0 for p in report["policies"])       # declined never counts as a taken trade


def test_a_tightened_stop_below_entry_is_a_loss_not_breakeven(wallet):
    t = _open(wallet)                                  # long 100, stop 98
    wallet.move_stop(t["id"], 99.0, "left watchlist")  # tightened, still below entry
    t2 = wallet.get_portfolio()["open_trades"][0]
    ev = _walk(wallet, t2, [(99.5, 99.6, 98.9, 99.0)])
    closed = ev[-1]["trade"]
    assert closed["close_reason"] == "tightened_stop" and closed["pnl_usd"] < 0


# ── "protect" — the default since 2026-10-07 ───────────────────────────────────

def test_protect_is_the_default_for_new_trades():
    assert exits.DEFAULT_POLICY == "protect" and "protect" in exits.SHADOW_POLICIES and "trail" in exits.SHADOW_POLICIES


def test_protect_locks_in_profit_from_plus_1r_from_the_next_bar(wallet):
    t = _open(wallet, "protect")                                       # 1R = 2: +1R is 102
    _walk(wallet, t, [(100, 101.9, 99.5, 101.5)])                      # +0.95R: nothing yet
    assert wallet.get_portfolio()["open_trades"][0]["stop_loss"] == 98
    _walk(wallet, t, [(101.5, 102.2, 101.0, 102.0)], skip=1)           # the next minute, +1.05R: breakeven-plus-costs
    live = wallet.get_portfolio()["open_trades"][0]
    # Breakeven and the 0.5R trail both start at +1R; the trail (102.2 − 0.5R) is already above
    # breakeven-plus-costs, so it's the stop — locked in profit, never below breakeven.
    assert live["stop_loss"] == pytest.approx(102.2 - 0.5 * live["r_unit"])
    assert live["stop_loss"] > live["entry_price"] + exits.cost_offset(live["entry_price"])


def test_protect_trails_half_an_r_behind_the_best_price(wallet):
    t = _open(wallet, "protect")
    _walk(wallet, t, [(100, 104.0, 99.8, 103.8)])                      # peak +2R (104)
    live = wallet.get_portfolio()["open_trades"][0]
    assert live["stop_loss"] == pytest.approx(104 - 0.5 * live["r_unit"])   # 0.5R behind the best price
    _walk(wallet, t, [(103.8, 103.9, 102.9, 103.0)], skip=1)            # back through it: out, in profit
    closed = wallet.all_trades()["closed"][0]
    assert closed["close_reason"] == "trailing_stop" and closed["pnl_usd"] > 0


def test_protect_closes_after_24_hours(wallet):
    t = _open(wallet, "protect", timeframe="4h")                       # the 4h max hold would be 7 days
    assert t["expires_at"] - t["opened_ts"] == pytest.approx(24 * 3600)
    _walk(wallet, t, [(100, 100.5, 99.6, 100.2)], now=t["opened_ts"] + 25 * 3600)


def test_a_bar_that_hits_the_old_stop_first_still_stops_out(wallet):
    t = _open(wallet, "protect")
    _walk(wallet, t, [(100, 102.5, 97.9, 101.0)])                      # +1.25R and the stop in one bar
    closed = wallet.all_trades()["closed"][0]
    assert closed["close_reason"] == "stop_loss"                       # pessimistic: the stop wins the bar


def test_manual_trades_never_expire_on_a_timeframe_clock(wallet):
    # Trend books mark their trades "1d" with the manual policy: they must not inherit the 1h 72 h cap.
    t = _open(wallet, "manual", timeframe="1d")
    assert t["expires_at"] - t["opened_ts"] >= 300 * 24 * 3600
    assert exits.WINDOWS["1d"][1] >= 30 * 24 * 3600


def test_shadow_candidates_cut_trades_going_nowhere_after_six_hours():
    from trading import exits
    assert {"protect_quick", "protect_cut6"} <= set(exits.SHADOW_POLICIES)
    t = {"symbol": "SOLUSDT", "direction": "long", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_1": 103.0,
         "take_profit_2": 106.0, "quantity": 1.0, "timeframe": "1h"}
    T0 = 1_000_000.0
    exits.stamp(t, "protect_quick", now=T0)
    assert t["stale_after_s"] == 6 * 3600 and t["stale_min_r"] == 0.25
    assert exits.apply_bar(t, 100, 100.3, 99.5, 100.1, T0 + 3600) is None          # peak +0.15R, 1h in: keep
    assert exits.apply_bar(t, 100.1, 100.2, 99.9, 100.0, T0 + 6 * 3600)[0] == "stale"
    exits.stamp(t2 := {**t}, "protect_quick", now=T0)
    exits.apply_bar(t2, 100, 101.1, 99.9, 101.0, T0 + 600)
    exits.apply_bar(t2, 101, 101.1, 100.9, 101.0, T0 + 900)                        # +0.55R: stop to entry
    assert t2["stop_loss"] >= 100.0


def test_protect_trim_banks_half_at_minus_0_3r_once():
    from trading import exits
    assert "protect_trim" in exits.SHADOW_POLICIES
    t = {"symbol": "SOLUSDT", "direction": "long", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_1": 103.0,
         "take_profit_2": 106.0, "quantity": 2.0, "timeframe": "1h"}
    T0 = 1_000_000.0
    exits.stamp(t, "protect_trim", now=T0)
    assert exits.apply_bar(t, 100.0, 100.1, 99.5, 99.6, T0 + 300) is None     # −0.3R is 99.4: not reached
    assert t["quantity"] == 2.0
    assert exits.apply_bar(t, 99.6, 99.7, 99.3, 99.5, T0 + 600) is None      # touches 99.4: trims half
    assert t["quantity"] == 1.0 and t["trimmed"] and t["realized_pnl_usd"] == pytest.approx(-0.6)
    exits.apply_bar(t, 99.5, 99.6, 99.2, 99.3, T0 + 900)
    assert t["quantity"] == 1.0                                                # once only
    hit = exits.apply_bar(t, 99.0, 99.0, 97.5, 97.8, T0 + 1200)               # the rest stops out
    assert hit[0] == "stop_loss"


def test_protect_rides_the_last_six_hours_and_leaves_when_price_slips():
    from trading import exits
    T0 = 1_000_000.0
    t = exits.stamp({"symbol": "SOLUSDT", "direction": "long", "entry_price": 100.0, "stop_loss": 98.0,
                     "take_profit_1": 103.0, "take_profit_2": 106.0, "quantity": 1.0, "timeframe": "1h"}, "protect", now=T0)
    assert exits.apply_bar(t, 100.0, 100.5, 99.8, 100.4, T0 + 10 * 3600) is None            # 14h left: not watching yet
    assert exits.apply_bar(t, 100.0, 100.6, 99.8, 100.4, T0 + 18.5 * 3600) is None          # window: best so far +0.2R
    assert exits.apply_bar(t, 100.4, 100.9, 100.3, 100.8, T0 + 19 * 3600) is None           # improving: +0.4R, keep riding
    assert exits.apply_bar(t, 100.8, 100.9, 100.4, 100.6, T0 + 19.5 * 3600) is None         # slipped 0.1R: not yet
    assert exits.apply_bar(t, 100.6, 100.7, 100.3, 100.45, T0 + 20 * 3600) == ("window_exit", 100.45)   # slipped 0.175R: out
    t2 = exits.stamp({**t, "stop_loss": 98.0}, "protect", now=T0)
    assert exits.apply_bar(t2, 100.0, 100.5, 99.5, 99.7, T0 + 24 * 3600) == ("expired", 99.7)   # never turned: the bell


# ── Dollar profit lock (2026-11-02) ────────────────────────────────────────────

def test_manual_lock_keeps_half_of_the_peak_once_a_hundred_dollars_up(wallet):
    t = wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1, timeframe="1h", extra={"exit_policy": "manual_lock", "strategy": "x"})
    assert t["exit_policy"] == "manual_lock" and t["quantity"] == pytest.approx(50, rel=0.05)    # $100 at risk over 2 points
    ev = _walk(wallet, t, [
        (100, 101.5, 99.9, 101.4),      # best open P&L ≈ +$70: under the arm, the stop stays at 98
    ])
    assert wallet.get_portfolio()["open_trades"][0]["stop_loss"] == 98
    ev = _walk(wallet, t, [
        (101.4, 104.2, 101.3, 104.0),   # ≈ +$205 at the peak: armed — half of it is kept from the next bar
        (104.0, 104.1, 100.5, 100.8),   # gives most of it back: out on the lock
    ], skip=1)
    closed = ev[-1]["trade"]
    assert closed["close_reason"] == "trailing_stop" and closed["locked_usd"] - 8 < closed["pnl_usd"] <= closed["locked_usd"]   # the exit's own fee and slippage come off the lock
    assert 90 < closed["pnl_usd"] < 110


def test_manual_lock_arms_no_earlier_than_one_r(wallet):
    """A $32k copy leg was $100 up after a 0.3% move (2026-11-04): the lock must wait for a full R, whichever is larger."""
    t = wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=4, timeframe="1h", extra={"exit_policy": "manual_lock", "strategy": "x"})
    assert t["risk_usd"] == pytest.approx(400, rel=0.05)
    _walk(wallet, t, [(100, 101.5, 99.9, 101.4)])                                   # ≈ +$280 at the peak: over $100 but under 1R — not armed
    assert wallet.get_portfolio()["open_trades"][0]["stop_loss"] == 98
    _walk(wallet, t, [(101.4, 102.6, 101.3, 102.5)], skip=1)                        # ≈ +$500: past 1R — armed
    assert wallet.get_portfolio()["open_trades"][0]["stop_loss"] > 98


def test_manual_lock_arms_at_three_percent_of_the_notional_when_one_r_is_further(wallet):
    """Two reversal legs with 10% disaster stops peaked +$330 and +$340 at 0.66R (2026-11-05) and the lock never armed."""
    t = wallet.open_trade("SOLUSDT", "long", 100, 90, 110, 120, risk_pct=5, timeframe="1h", extra={"exit_policy": "manual_lock", "strategy": "x"})
    assert t["risk_usd"] == pytest.approx(500, rel=0.05) and t["notional_usd"] == pytest.approx(5000, rel=0.05)
    assert exits.lock_arm_usd(t, exits.POLICIES["manual_lock"]) == pytest.approx(150, rel=0.05)      # 3% of $5k, not 1R = $500
    _walk(wallet, t, [(100, 103.5, 99.9, 103.4)])                                   # ≈ +$170 at the peak: armed
    live = wallet.get_portfolio()["open_trades"][0]
    assert live["stop_loss"] > 100 and live["locked_usd"] == pytest.approx(85, abs=5)
    ev = _walk(wallet, t, [(103.4, 103.5, 100.2, 100.5)], skip=1)                   # gives it back: out with half the peak
    closed = ev[-1]["trade"]
    assert closed["close_reason"] == "trailing_stop" and 70 < closed["pnl_usd"] < 90


def test_manual_lock_scales_down_to_a_small_leg(wallet):
    """A $1,000 reversal leg at a 10% stop (2026-11-07: ZEC sat +$18 at its 24h low, the old $100 floor needed a 10% move): arms at 3% of the notional."""
    t = wallet.open_trade("ZECUSDT", "short", 100, 110, 90, 80, risk_pct=1, timeframe="1h", extra={"exit_policy": "manual_lock", "strategy": "x"})
    assert t["risk_usd"] == pytest.approx(100, rel=0.05) and t["notional_usd"] == pytest.approx(1000, rel=0.05)
    assert exits.lock_arm_usd(t, exits.POLICIES["manual_lock"]) == pytest.approx(30, rel=0.05)       # 3% of $1k, not 1R = $100, not the $10 floor
    _walk(wallet, t, [(100, 100.1, 96.4, 96.5)])                                    # ≈ +$35 at the peak: armed
    live = wallet.get_portfolio()["open_trades"][0]
    assert live["stop_loss"] < 110 and live["locked_usd"] == pytest.approx(17, abs=3)


def test_a_running_manual_trade_adopts_the_lock_once_it_is_live_on_its_book(wallet, monkeypatch):
    """The policy is stamped at entry: the two legs open when the lock deployed (2026-11-05) kept the old rule."""
    monkeypatch.setattr(exits, "LOCK_MODE", "shadow")
    t = wallet.open_trade("SOLUSDT", "long", 100, 90, 110, 120, risk_pct=5, timeframe="1d",
                          extra={"exit_policy": "manual", "shadows": False, "strategy": "weekly_rs"})
    assert t["exit_policy"] == "manual"
    _walk(wallet, t, [(100, 103.5, 99.9, 103.4)])                                   # peaks +$170 under the old rule
    live = wallet.get_portfolio()["open_trades"][0]
    assert live["exit_policy"] == "manual" and live["stop_loss"] == 90               # a shadow-mode book: nothing changes
    monkeypatch.setattr(exits, "LOCK_MODE", "live")                                  # the deploy lands
    _walk(wallet, t, [(103.4, 103.5, 103.0, 103.2)], skip=1)
    live = wallet.get_portfolio()["open_trades"][0]
    assert live["exit_policy"] == "manual_lock" and live["lock_adopted_ts"] and live["peak_price"] == 103.5
    assert live["stop_loss"] > 100                                                   # the peak it already had is what the lock keeps


def test_manual_lock_never_expires_and_keeps_the_original_stop_until_armed(wallet):
    t = wallet.open_trade("SOLUSDT", "short", 100, 102, 97, 95, risk_pct=1, timeframe="1h", extra={"exit_policy": "manual_lock", "strategy": "x"})
    assert t["expires_at"] - t["opened_ts"] >= 300 * 24 * 3600
    ev = _walk(wallet, t, [(100, 100.5, 99.6, 99.8), (99.8, 102.3, 99.7, 102.1)])              # straight to the stop
    assert ev[-1]["trade"]["close_reason"] == "stop_loss"


def test_the_lock_is_live_on_the_copy_books_and_a_shadow_elsewhere(wallet, monkeypatch):
    monkeypatch.setattr(exits, "LOCK_MODE", "shadow")
    assert exits.lock_policy_for("copy_trading", "manual") == exits.lock_policy_for("copy_stocks_wide", "manual") == "manual_lock"
    assert exits.lock_policy_for("weekly_rs", "manual") == "manual" and exits.lock_policy_for("auto", "protect") == "protect"
    # Every autotrader trade (2026-11-05): its lane legs open under "manual" and get the lock, on paper and on the live ledger.
    assert exits.lock_policy_for("auto", "manual") == exits.lock_policy_for("auto_live", "manual") == "manual_lock"
    assert exits.lock_policy_for("auction", "manual") == "manual"
    monkeypatch.setattr(exits, "LOCK_MODE", "live")                                        # production's default
    assert exits.lock_policy_for("weekly_rs", "manual") == exits.lock_policy_for("auction", "manual") == exits.lock_policy_for("manual", "manual") == "manual_lock"
    monkeypatch.setattr(exits, "LOCK_MODE", "shadow")
    t = wallet.open_trade("ETHUSDT", "long", 100, 70, 150, 160, risk_pct=1, timeframe="1d", extra={"exit_policy": "manual", "shadows": False, "strategy": "weekly_rs"})
    assert t["exit_policy"] == "manual"
    sh = wallet._load()["shadows"]
    assert [x["exit_policy"] for x in sh if x["trade_id"] == t["id"]] == ["manual_lock"]      # the lock is scored beside it
    monkeypatch.setattr(exits, "LOCK_MODE", "off")
    t2 = wallet.open_trade("BTCUSDT", "long", 100, 70, 150, 160, risk_pct=1, timeframe="1d", extra={"exit_policy": "manual", "shadows": False, "strategy": "weekly_rs"})
    assert [x for x in wallet._load()["shadows"] if x["trade_id"] == t2["id"]] == []


def test_the_lock_never_places_a_stop_the_price_has_already_crossed(wallet):
    # A trade that peaked +$200 BEFORE the lock applied and has since fallen to a loss: switching it to manual_lock must
    # not conjure a stop above the market (that closed HYPE at −$385 on 2026-11-02).
    t = wallet.open_trade("SOLUSDT", "long", 100, 98, 103, 105, risk_pct=1, timeframe="1h", extra={"exit_policy": "manual", "strategy": "x"})
    _walk(wallet, t, [(100, 104.2, 99.9, 104.0), (104.0, 104.1, 98.6, 98.8)])            # peak +$200, now −$60, stop untouched at 98
    wallet.annotate(t["id"], exit_policy="manual_lock")
    ev = _walk(wallet, t, [(98.8, 99.2, 98.5, 99.0), (99.0, 99.5, 98.7, 99.3)], skip=2)
    assert all(e.get("trade", {}).get("close_reason") is None for e in ev) and wallet.get_portfolio()["open_trades"][0]["stop_loss"] == 98
    ev = _walk(wallet, t, [(99.3, 104.5, 99.2, 104.4), (104.4, 104.6, 101.0, 101.2)], skip=4)   # back above the lock level: now it arms and holds
    assert ev[-1]["trade"]["close_reason"] == "trailing_stop" and ev[-1]["trade"]["pnl_usd"] > 80
