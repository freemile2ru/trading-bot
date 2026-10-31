"""The advisory allocator: written rule, total preserved, floors and caps respected."""
from trading import allocator, capital


def _row(book, planned, avg30, expected=None, days=30):
    return {"book": book, "planned": planned, "avg30": avg30, "expected": expected, "days": days}


def test_losers_give_a_quarter_and_winners_take_pro_rata_total_unchanged():
    rows = [_row("weekly_rs", 4_000, -40.0), _row("carry", 8_000, 8.0, expected=6.0), _row("events", 8_000, 0.0, expected=0.0),
            _row("futures_trend", 5_500, 11.0), _row("auto", 2_000, -2.0, days=5),
            _row("young1", 10_000, 1.0, days=2), _row("young2", 10_000, 1.0, days=2)]       # raise the total so the cap stays clear
    s = allocator.suggest(rows)
    assert s["changed"]
    assert s["suggested"]["weekly_rs"] == 3_000                      # −25%
    assert s["suggested"]["auto"] == 2_000                           # too young to move
    assert s["suggested"]["events"] == 8_000                         # score 0: neither gives nor takes
    gained = {m["book"]: m["delta"] for m in s["moves"] if m["delta"] > 0}
    assert set(gained) == {"carry", "futures_trend"} and gained["futures_trend"] > gained["carry"]   # pro rata to score
    assert round(sum(s["suggested"].values()), 2) == round(sum(s["current"].values()), 2)


def test_floor_and_cap_hold():
    rows = [_row("cascade", 500, -5.0), _row("copy_trading", 2_000, -6.0), _row("carry", 13_000, 50.0)] + \
           [_row(b, 5_000, -1.0, days=3) for b in ("x1", "x2", "x3", "x4", "x5", "x6")]
    s = allocator.suggest(rows)
    total = sum(s["current"].values())                               # 45,500 → cap 13,650
    assert s["suggested"]["cascade"] == 500                          # never below the floor: gives nothing
    assert s["suggested"]["copy_trading"] == 1_500                   # gives 25%
    assert s["suggested"]["carry"] == 13_500 <= total * allocator.CAP_FRAC   # takes it all, still under the cap
    assert round(sum(s["suggested"].values()), 2) == round(total, 2)
    # With the taker already at the cap, the giver keeps what cannot be placed.
    s2 = allocator.suggest([_row("copy_trading", 2_000, -6.0), _row("carry", 14_000, 50.0)] + [_row(b, 5_000, -1.0, days=3) for b in ("x1", "x2", "x3", "x4", "x5", "x6")])
    assert s2["suggested"]["carry"] == 14_000 and s2["suggested"]["copy_trading"] == 2_000 and not s2["changed"]


def test_no_change_when_nothing_is_eligible():
    s = allocator.suggest([_row("carry", 8_000, 5.0, days=3), _row("auto", 2_000, -5.0, days=3)])
    assert not s["changed"] and s["suggested"] == s["current"]


def test_set_plan_keeps_books_and_total():
    from storage import state as kv
    kv.delete_sync(capital._k("plan"), capital._k("plan_note"))
    new = dict(capital.PLAN)
    new["weekly_rs"], new["carry"] = new["weekly_rs"] - 500, new["carry"] + 500
    assert capital.set_plan(new, "test")["carry"] == capital.PLAN["carry"] + 500
    assert capital.plan() == {k: float(v) for k, v in new.items()}
    try:
        capital.set_plan({**new, "carry": new["carry"] + 1}, "bad total")
        assert False, "a changed total must be refused"
    except ValueError:
        pass
    kv.delete_sync(capital._k("plan"), capital._k("plan_note"))
    assert capital.plan() == dict(capital.PLAN)


# ── The verdict ladder (2026-10-31) ─────────────────────────────────────────────

def test_ladder_promotes_a_proven_edge_and_cuts_a_proven_loser_total_unchanged():
    plan = {"carry": 8_000.0, "copy_crypto_fast": 611.11, "cascade": 500.0, "main": 6_000.0, "auto": 1_000.0, "events": 8_000.0}
    v = {"copy_crypto_fast": {"status": "edge", "trades": 42}, "auto": {"status": "no_edge", "trades": 60}, "cascade": {"status": "collecting", "trades": 9}}
    new, moves = allocator.ladder(plan, v)
    total = sum(plan.values())
    assert round(sum(new.values()), 2) == round(total, 2)
    assert new["auto"] == allocator.FLOOR_USD                                  # no edge → floor
    assert new["copy_crypto_fast"] == round(total * allocator.PROMOTE_FRAC, 2)  # edge → 10% of the total
    assert new["cascade"] == 500.0 and new["events"] == 8_000.0                 # untouched: no verdict, and at the floor anyway
    # Funded by the demotion first, the reserve second: the reserve gave only what was still needed.
    need = new["copy_crypto_fast"] - plan["copy_crypto_fast"]
    freed = plan["auto"] - allocator.FLOOR_USD
    assert new["main"] == round(plan["main"] - (need - freed), 2)
    why = {m["book"]: m["why"] for m in moves}
    assert "promoted" in why["copy_crypto_fast"] and "no edge" in why["auto"] and "reserve" in why["main"]


def test_ladder_asks_the_collecting_books_when_the_reserve_is_short():
    plan = {"copy_crypto_fast": 600.0, "main": 500.0, "cascade": 1_500.0, "weekly_rs": 1_500.0, "carry": 10_000.0}
    v = {"copy_crypto_fast": {"status": "edge", "trades": 40}, "carry": {"status": "collecting", "trades": 0}}
    new, moves = allocator.ladder(plan, v)
    total = sum(plan.values())
    assert round(sum(new.values()), 2) == round(total, 2)
    assert new["main"] == 500.0                                                # nothing above its floor to give
    assert new["copy_crypto_fast"] == round(total * allocator.PROMOTE_FRAC, 2)
    donors = {m["book"]: -m["delta"] for m in moves if m["delta"] < 0}
    assert set(donors) == {"cascade", "weekly_rs", "carry"}                    # pro rata above their floors
    assert donors["carry"] > donors["cascade"] and abs(donors["cascade"] - donors["weekly_rs"]) <= 0.01   # the last donor absorbs the cent


def test_verdicts_follow_the_lab_rule():
    good = [{"status": "closed", "net_pnl": 5.0, "r": 0.5}] * 30 + [{"status": "closed", "net_pnl": -2.0, "r": -1.0}] * 12
    bad = [{"status": "closed", "net_pnl": -5.0, "r": -1.0}] * 30 + [{"status": "closed", "net_pnl": 2.0, "r": 0.4}] * 12
    v = allocator.verdicts({"a": good, "b": bad, "c": good[:10]})
    assert v["a"]["status"] == "edge" and v["b"]["status"] == "no_edge" and v["c"]["status"] == "collecting"


def test_plan_for_merges_drift_and_ladder():
    rows = [_row("weekly_rs", 4_000, -40.0), _row("carry", 8_000, 8.0, expected=6.0), _row("copy_crypto_fast", 611, 1.0, days=2),
            _row("main", 6_000, 0.0), _row("x1", 10_000, 1.0, days=2), _row("x2", 10_000, 1.0, days=2)]
    s = allocator.plan_for(rows, {"copy_crypto_fast": {"status": "edge", "trades": 40}})
    assert s["changed"] and s["verdicts"] == {"copy_crypto_fast": "edge"}
    assert round(sum(s["suggested"].values()), 2) == round(sum(s["current"].values()), 2)
    assert s["suggested"]["copy_crypto_fast"] == round(sum(s["current"].values()) * allocator.PROMOTE_FRAC, 2)
    assert s["suggested"]["weekly_rs"] == 3_000                                 # the score rule still drifted it
    assert all(m["delta"] == round(m["to"] - m["from"], 2) for m in s["moves"])
