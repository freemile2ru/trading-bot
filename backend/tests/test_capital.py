"""The $100/day programme: deposits, pause-aware sizing, daily P&L from equity curves, the breaker."""
import asyncio

from storage import state as kv
from trading import capital, target
from trading.paper_trader import PaperWallet


def _clean():
    kv.delete_sync(capital._k("ledger"), capital._k("paused"), *[capital._k("added", b) for b in ("carry", "carry_q", "events", "weekly_rs", "x")])


def test_paper_wallet_deposit_keeps_return_honest(tmp_path):
    w = PaperWallet(str(tmp_path / "w.db"), label="t", initial_balance=10_000)
    assert w.deposit(20_000, "test") == 30_000
    p = w.get_portfolio()
    assert p["initial_balance"] == 30_000 and p["total_equity"] == 30_000 and p["total_return_pct"] == 0
    assert w.all_trades()["initial_balance"] == 30_000


def test_pair_book_deposit_and_ledger(monkeypatch):
    _clean()
    monkeypatch.setattr(capital, "PLAN", {"carry": 30_000.0, "carry_q": 40_000.0})
    done = capital.apply_plan()
    assert [(d["book"], d["amount"]) for d in done] == [("carry", 20_000.0), ("carry_q", 30_000.0)]
    assert capital.added("carry") == 20_000 and capital.initial("carry", 10_000) == 30_000
    assert capital.apply_plan() == []                        # idempotent
    assert len(capital.deposits()) == 2
    _clean()


def test_pause_shrinks_sizing_only(monkeypatch):
    _clean()
    monkeypatch.setattr(capital, "PLAN", {"carry": 30_000.0})       # the book holds what its plan says
    kv.set_sync(capital._k("added", "carry"), 20_000.0)
    assert capital.sizing_equity("carry", 29_500) == 29_500
    capital.pause("test")
    assert capital.sizing_equity("carry", 29_500) == 9_500
    assert capital.sizing_equity("auto", 9_000) == 9_000       # books without programme capital are untouched
    assert capital.initial("carry", 10_000) == 30_000          # accounting is unchanged by a pause
    capital.resume()
    assert capital.paused() is None and capital.sizing_equity("carry", 29_500) == 29_500
    _clean()


def test_daily_pnl_from_curve_takes_deposits_out():
    d0 = 1_800_000_000.0                                        # 2027-01-15 00:00 UTC
    series = [(d0 + 3600, 10_000), (d0 + 7200, 10_050), (d0 + 86400 + 3600, 10_020),
              (d0 + 2 * 86400 + 3600, 30_120)]                  # day 3: +20k deposit and +100 P&L
    deposits = [{"ts": d0 + 2 * 86400 + 1800, "amount": 20_000, "book": "x"}]
    pnl = target.daily_pnl(series, deposits)
    assert list(pnl.values()) == [50.0, -30.0, 100.0]
    assert target._avg(pnl, 7, d0 + 2 * 86400 + 7200) == round(120 / 7, 2)
    assert target._avg(pnl, 2, d0 + 2 * 86400 + 7200) == 35.0
    # a curve that only starts after the deposit already holds it in its first point
    late = [(d0 + 2 * 86400 + 3600, 30_120), (d0 + 2 * 86400 + 7200, 30_150)]
    assert list(target.daily_pnl(late, deposits).values()) == [30.0]


def test_breaker_trips_once_after_a_week(monkeypatch):
    _clean()
    kv.set_sync(capital._k("added", "carry"), 20_000.0)
    notes = []

    async def fake_notify(title, body):
        notes.append(title)
    import ops.watchdog
    monkeypatch.setattr(ops.watchdog, "_notify", fake_notify)
    monkeypatch.setattr(target, "log_pair_equity", lambda now=None: {})
    rep = {"totals": {"avg7": target.TRIP_AVG7 - 50.0, "today": -10.0}, "books": [{"days": 8}], "paused": None}
    monkeypatch.setattr(target, "report", lambda now=None, max_age_s=60: dict(rep))
    out = asyncio.run(target.tick(1_800_000_000.0))
    assert out["paused"] and notes == ["Daily target: breaker tripped"]
    asyncio.run(target.tick(1_800_000_100.0))
    assert notes == ["Daily target: breaker tripped"]          # not re-notified while paused
    # a short history never trips, however bad
    capital.resume()
    rep["books"] = [{"days": 3}]
    asyncio.run(target.tick(1_800_000_200.0))
    assert capital.paused() is None
    _clean()


def test_expected_uses_the_evidence(monkeypatch):
    from experiments import carry_q, events
    monkeypatch.setattr(carry_q, "open_positions", lambda: [{"basis_ann": 0.05, "notional": 36_500}])
    assert target.expected("carry_q", 40_000, 0)[0] == 5.0
    monkeypatch.setattr(events, "open_positions", lambda: [{"notional": 3_000}, {"notional": 3_000}])
    assert target.expected("events", 25_000, 0)[0] == 40.0
    assert target.expected("weekly_rs", 30_000, 0)[0] == round(0.0136 / 7 * 30_000, 2)
    assert target.expected("auto", 10_000, 0) == (None, "no evidence of an edge")


# ── The programme-wide per-coin cap ────────────────────────────────────────────────────────────────────────

def _books(tmp_path, monkeypatch):
    from autotrader import lanes
    from experiments import books
    from trading import history
    ws = {n: PaperWallet(str(tmp_path / f"{n}.db"), label=n, initial_balance=50_000) for n in ("main", "auto", "copy_trading", "auction", "prop")}
    monkeypatch.setattr(history, "WALLETS", {"main": ws["main"], "auto": ws["auto"]})
    monkeypatch.setattr(books, "WALLETS", {"copy_trading": ws["copy_trading"], "auction": ws["auction"], "prop": ws["prop"]})
    monkeypatch.setattr(capital, "TOTAL", 200_000.0)
    monkeypatch.setattr(capital, "COIN_CAP_PCT", 4.0)                      # $8,000 a coin
    monkeypatch.setattr(lanes, "LANE_BOOK", {"reversal": "auto", "cascade": "cascade"})
    return ws


def _short(w, symbol, notional, **extra):
    return w.open_trade(symbol=symbol, direction="short", entry_price=1.0, stop_loss=1.1, take_profit_1=0.5, take_profit_2=0.4,
                        notional_usd=notional, timeframe="1d", leverage=2, extra={"exit_policy": "manual", "shadows": False, **extra})


def test_coin_exposure_is_signed_by_book_and_skips_mirrors_and_prop(tmp_path, monkeypatch):
    ws = _books(tmp_path, monkeypatch)
    _short(ws["copy_trading"], "ARBUSDT", 6_000)
    _short(ws["auto"], "ARBUSDT", 1_500, lane="reversal")
    _short(ws["auto"], "ARBUSDT", 9_000, lane="cascade")                   # a mirror of the cascade book: counted there, not here
    _short(ws["prop"], "ARBUSDT", 20_000)                                   # a separate account
    ws["main"].open_trade(symbol="ARBUSDT", direction="long", entry_price=1.0, stop_loss=0.9, take_profit_1=1.5, take_profit_2=1.6,
                          notional_usd=1_000, timeframe="1d", leverage=2, extra={"exit_policy": "manual", "shadows": False})
    assert capital.coin_exposure("ARBUSDT") == {"main": 1_000.0, "auto": -1_500.0, "copy_trading": -6_000.0}
    assert capital.coin_exposure("ETHUSDT") == {}


def test_the_first_book_on_a_coin_is_not_capped_a_second_is_cut_or_skipped(tmp_path, monkeypatch):
    ws = _books(tmp_path, monkeypatch)
    assert capital.coin_room("ARBUSDT", "short", 50_000, "auction")["notional"] == 50_000           # alone: its own sizing rules
    _short(ws["copy_trading"], "ARBUSDT", 6_000)
    assert capital.coin_room("ARBUSDT", "short", 7_000, "copy_trading")["notional"] == 7_000        # the same book adding: not a second book
    assert capital.coin_room("ARBUSDT", "long", 7_000, "auto")["notional"] == 7_000                 # the other side nets off
    r = capital.coin_room("ARBUSDT", "short", 3_000, "auto")                                       # $2,000 of room: cut
    assert r["notional"] == 2_000 and "cut $3,000 → $2,000" in r["why"] and "copy_trading $6,000 already short" in r["why"]
    r = capital.coin_room("ARBUSDT", "short", 5_000, "auto")                                       # less than half fits: skipped
    assert r["notional"] == 0 and r["why"].startswith("coin cap:") and r["held"] == {"copy_trading": -6_000.0}
    _short(ws["main"], "ARBUSDT", 1_000)
    assert capital.coin_room("ARBUSDT", "short", 1_500, "auto")["notional"] == 1_000               # net −$7,000: $1,000 left


def test_the_cap_never_blocks_a_leg_it_cannot_measure(monkeypatch):
    monkeypatch.setattr(capital, "coin_exposure", lambda symbol: (_ for _ in ()).throw(RuntimeError("db locked")))
    assert capital.coin_room("ARBUSDT", "short", 4_000, "auto") == {"notional": 4_000, "why": None, "held": {}}
