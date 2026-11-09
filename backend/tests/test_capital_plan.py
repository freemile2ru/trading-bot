"""The $50k plan: withdrawals, free-cash limits, idempotent apply_plan, and the protect_run shadow exit."""
from storage import state as kv
from trading import capital, exits
from trading.paper_trader import PaperWallet

HOUR = 3600


def _clean(*books):
    kv.delete_sync(capital._k("ledger"), capital._k("paused"), *[capital._k("added", b) for b in books])


def test_withdraw_takes_at_most_free_cash(tmp_path):
    w = PaperWallet(str(tmp_path / "w.db"), label="t", initial_balance=10_000)
    assert w.withdraw(4_000, "test") == 4_000
    p = w.get_portfolio()
    assert p["initial_balance"] == 6_000 and p["total_equity"] == 6_000 and p["total_return_pct"] == 0
    assert w.withdraw(50_000, "test") == 6_000            # never below zero cash
    assert w.withdraw(10, "test") == 0.0


def test_plan_adds_up_to_the_total():
    assert round(sum(capital.PLAN.values())) == round(capital.TOTAL) == 219_500                 # +$10k burst books 2026-11-06, −$1k multi_factor lane budget 2026-11-07, +$25k prop 2026-11-09
    assert capital.PLAN["prop"] == 25_000.0
    assert capital.PLAN["burst_up"] == capital.PLAN["burst_dip"] == 5_000.0
    assert capital.PLAN["copy_trading"] == capital.PLAN["copy_stocks"] == 32_750.0 and "copy_futures" not in capital.PLAN   # $60k + the variants' $5.5k, futures retired
    assert "claude_swing" not in capital.PLAN and not any(b.startswith("copy_crypto_") for b in capital.PLAN)
    assert "multi_factor" not in capital.PLAN and capital.PLAN["main"] == 1_000                     # retired 2026-11-07: a lane budget, not money — nothing to park


def test_apply_plan_moves_pair_books_both_ways(monkeypatch):
    _clean("carry", "carry_q")
    kv.set_sync(capital._k("added", "carry"), 20_000.0)             # held $30k under the old programme
    monkeypatch.setattr(capital, "PLAN", {"carry": 8_000.0, "carry_q": 12_000.0})
    done = capital.apply_plan()
    assert sorted((d["book"], d["amount"]) for d in done) == [("carry", -22_000.0), ("carry_q", 2_000.0)]
    assert capital.added("carry") == -2_000 and capital.initial("carry", 10_000) == 8_000
    assert capital.initial("carry_q", 10_000) == 12_000
    assert capital.apply_plan() == []                                 # idempotent
    assert capital.shortfall("carry") == 0.0
    # While paused only withdrawals run.
    monkeypatch.setattr(capital, "PLAN", {"carry": 9_000.0, "carry_q": 11_000.0})
    done = capital.apply_plan(withdraw_only=True)
    assert [(d["book"], d["amount"]) for d in done] == [("carry_q", -1_000.0)]
    assert capital.shortfall("carry") == 1_000.0
    _clean("carry", "carry_q")


def test_pause_never_raises_sizing_for_a_cut_book():
    _clean("carry", "events")
    kv.set_sync(capital._k("added", "carry"), -2_000.0)
    kv.set_sync(capital._k("added", "events"), 5_000.0)
    capital.pause("test")
    assert capital.sizing_equity("carry", 8_000) == 8_000           # cut books size from what they have
    assert capital.sizing_equity("events", 15_000) == 10_000        # topped-up books fall back to base
    capital.resume()
    _clean("carry", "events")


def _trade(policy: str) -> dict:
    t = {"entry_price": 100.0, "stop_loss": 90.0, "take_profit_1": 110.0, "take_profit_2": 120.0,
         "direction": "long", "timeframe": "1h", "quantity": 1.0}
    return exits.stamp(t, policy, now=0.0)


def test_protect_run_extends_a_profitable_trade_at_the_bell_and_protect_does_not():
    run, base = _trade("protect_run"), _trade("protect")
    for t in (run, base):
        assert exits.apply_bar(t, 100, 108, 100, 108, 23 * HOUR) is None     # +0.8R, no breakeven yet (needs +1R)
    assert exits.apply_bar(base, 108, 108, 108, 108, 24 * HOUR) == ("expired", 108)
    assert exits.apply_bar(run, 108, 108, 108, 108, 24 * HOUR) is None
    assert run["expires_at"] == 48 * HOUR and run["extended"] == 1
    # Flat at the next bell: it leaves like any other trade.
    assert exits.apply_bar(run, 100, 100, 100, 100, 48 * HOUR) == ("expired", 100)


def test_protect_run_stops_extending_after_a_week():
    t = _trade("protect_run")
    for h in range(24, 169, 24):
        out = exits.apply_bar(t, 108, 108, 100, 108, h * HOUR)
        if h < 168:
            assert out is None
    assert out == ("expired", 108)
