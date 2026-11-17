"""The copies' profit floor (2026-11-17): a copy that has been +2% up never closes at a loss."""
import asyncio

from trading import exits
from trading.paper_trader import PaperWallet

T0 = 1_794_000_000.0
H = 3600


def _copy(**over) -> dict:
    t = {"id": "c1", "symbol": "NEARUSDT", "direction": "short", "entry_price": 3.184, "stop_loss": 3.3124,
         "take_profit_1": 1.6, "take_profit_2": 1.3, "quantity": 7439.0, "notional_usd": 23686.0, "risk_usd": 955.0,
         "opened_ts": T0, "opened_at": "2026-11-16T03:50:00+00:00", "checked_from_ms": None, "timeframe": "copy", "leverage": 7,
         "exit_policy": "manual_lock", "shadows": False, "leader_id": "0xabc", "fees_usd": 11.8, "be_at_pct": 2.0}
    t.update(over)
    return exits.stamp(t, "manual_lock", T0)


def test_a_copy_that_was_two_percent_up_leaves_at_breakeven_not_at_the_stop():
    t = _copy()
    assert exits.apply_bar(t, 3.18, 3.19, 3.15, 3.16, T0 + H) is None            # +0.75%: nothing
    assert t["stop_loss"] == 3.3124
    assert exits.apply_bar(t, 3.16, 3.17, 3.115, 3.13, T0 + 2 * H) is None        # low 3.115 = +2.2% → the floor arms from the next bar
    floor = round(3.184 - exits.cost_offset(3.184), 8)
    assert t["stop_loss"] == floor and t["floored_ts"] == T0 + 2 * H and t["original_stop_loss"] == 3.3124
    assert exits.apply_bar(t, 3.13, 3.17, 3.12, 3.15, T0 + 3 * H) is None         # still under the floor: nothing happens
    assert t["stop_loss"] == floor
    out = exits.apply_bar(t, 3.17, 3.26, 3.16, 3.23, T0 + 4 * H)                  # the bounce that cost −$420 live
    assert out is not None and out[0] == "breakeven" and abs(out[1] - floor) < 1e-9


def test_the_floor_needs_the_full_two_percent_and_a_price_still_past_it():
    t = _copy()
    assert exits.apply_bar(t, 3.18, 3.19, 3.13, 3.14, T0 + H) is None            # +1.7%: not enough
    assert t["stop_loss"] == 3.3124
    # Reached +2.2% inside the bar but closed back above the entry: the floor would be a stop the price has crossed — not set.
    assert exits.apply_bar(t, 3.14, 3.21, 3.11, 3.20, T0 + 2 * H) is None
    assert t["stop_loss"] == 3.3124
    # The next bar back in profit: the floor goes in (the peak is remembered).
    assert exits.apply_bar(t, 3.20, 3.20, 3.16, 3.17, T0 + 3 * H) is None
    assert t["stop_loss"] == round(3.184 - exits.cost_offset(3.184), 8)


def test_a_trade_without_the_stamp_keeps_the_old_rule_and_the_lock_still_ratchets_above_the_floor():
    leg = _copy(be_at_pct=None)
    del leg["be_at_pct"]
    assert exits.apply_bar(leg, 3.18, 3.19, 3.10, 3.12, T0 + H) is None           # +2.6%: no floor on a reversal leg
    assert leg["stop_loss"] == 3.3124
    t = _copy()
    exits.apply_bar(t, 3.18, 3.19, 3.10, 3.12, T0 + H)                            # floor in (3% arm not reached: peak P&L $613 < $711)
    exits.apply_bar(t, 3.12, 3.12, 3.05, 3.06, T0 + 2 * H)                        # +4.2%: the lock arms and keeps 65% — above the floor
    assert (3.184 - t["stop_loss"]) * 7439 > 0.65 * (3.184 - 3.05) * 7439 - 20 and t["locked_usd"] > 0


def test_a_long_copy_and_the_bits_shadow_carry_the_floor():
    t = _copy(direction="long", entry_price=100.0, stop_loss=96.0, take_profit_1=150.0, take_profit_2=160.0, quantity=100.0, notional_usd=10_000.0)
    assert exits.apply_bar(t, 100, 102.5, 99.5, 101.0, T0 + H) is None
    assert t["stop_loss"] == round(100 + exits.cost_offset(100.0), 8)
    out = exits.apply_bar(t, 101, 101, 97, 98, T0 + 2 * H)
    assert out[0] == "breakeven"
    sh = exits.make_shadows(_copy(), "copy_trading")
    assert [s["exit_policy"] for s in sh] == ["manual_lock_bits"] and sh[0]["be_at_pct"] == 2.0


def test_the_floor_is_live_on_production_only_and_a_shadow_elsewhere(tmp_path, monkeypatch):
    """2026-11-17 (the user): live on production, an experiment locally. The stamp follows BE_LIVE on every poll, and a
    copy without the stamp runs the manual_lock_floor shadow beside bits."""
    from experiments import copy_trading as ct
    w = PaperWallet(str(tmp_path / "c.db"), label="copy_trading")
    w.reset_portfolio(50_000)
    t = w.open_trade(symbol="NEARUSDT", direction="short", entry_price=3.184, stop_loss=3.3124, take_profit_1=1.6, take_profit_2=1.3, notional_usd=5000,
                     timeframe="copy", leverage=5, extra={"strategy": "copy_trading", "exit_policy": "manual", "shadows": False, "leader_id": "0xabc", "leader": "x"})
    assert ct.BE_PCT == 2.0 and ct.config()["be_pct"] == 2.0 and ct.BE_LIVE is False          # tests run as a local instance
    assert "be_at_pct" not in t
    assert [s["exit_policy"] for s in w._load()["shadows"]] == ["manual_lock_bits", "manual_lock_floor"]
    floor = next(s for s in w._load()["shadows"] if s["exit_policy"] == "manual_lock_floor")
    assert exits.policy_of(floor).be_at_pct == 2.0
    ct._floor_stamp(w, t)                                                        # local: nothing to stamp
    assert w.get_portfolio()["open_trades"][0].get("be_at_pct") is None
    monkeypatch.setattr(ct, "BE_LIVE", True)                                     # production: the running copy takes the floor
    ct._floor_stamp(w, w.get_portfolio()["open_trades"][0])
    assert w.get_portfolio()["open_trades"][0]["be_at_pct"] == 2.0
    monkeypatch.setattr(ct, "BE_LIVE", False)                                    # …and loses it again where the floor is a shadow
    ct._floor_stamp(w, w.get_portfolio()["open_trades"][0])
    assert w.get_portfolio()["open_trades"][0]["be_at_pct"] is None
    a = ct.floor_audit()
    assert a["policy"] == "manual_lock_floor" and a["live"] is False and a["label"] == "the +2% breakeven floor"
