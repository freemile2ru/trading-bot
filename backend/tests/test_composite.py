"""The one book (experiments/composite.py): shorts only from the listed sources, one risk a leg under the caps, the
floors halt then flatten, the drawdown stops the book, the record is read against the target."""
import asyncio
import time

import pytest

from experiments import books, composite
from storage import state as kv
from trading import book_switches, monitor
from trading.paper_trader import PaperWallet


@pytest.fixture
def book(tmp_path, monkeypatch):
    w = PaperWallet(str(tmp_path / "composite.db"), label="composite", initial_balance=50_000)
    monkeypatch.setitem(books.WALLETS, "composite", w)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    kv.delete_sync(composite.STATE_KEY, composite.LOG_KEY)
    prices = {"ZECUSDT": 100.0, "TAOUSDT": 400.0, "NEARUSDT": 3.0, "PLTRUSDT": 150.0, "SOLUSDT": 80.0}

    async def price(sym):
        return prices.get(sym)

    async def atr(sym, now=None):
        return 4.0
    monkeypatch.setattr(monitor, "live_price", price)
    import autotrader.sizing as sizing
    monkeypatch.setattr(sizing, "daily_atr_pct", atr)
    return w, prices


def test_sizing_is_one_risk_a_leg_under_the_notional_and_gross_caps():
    s = composite.size(50_000, 100.0, 110.0, 0.0)                      # a 10% stop: 1% of the book = $5,000 of notional
    assert s["notional"] == 5_000.0 and s["risk_usd"] == 500.0 and s["bound"] is None
    s = composite.size(50_000, 100.0, 101.0, 0.0)                      # a 1% stop wants $50k: the 25% notional cap gives $12,500 (0.25% of risk)
    assert s["notional"] == 12_500.0 and s["wanted"] == 50_000.0 and s["bound"] == "notional_cap" and s["risk_usd"] == 125.0
    s = composite.size(50_000, 100.0, 110.0, 72_000.0)                 # $72k already on: $3k of the $75k gross cap is left
    assert s["notional"] == 3_000.0 and s["bound"] == "gross_cap"
    assert composite.size(50_000, 100.0, 100.0, 0.0)["notional"] == 0.0


def test_offers_take_shorts_only_from_the_sources_one_per_symbol(book):
    w, _ = book
    now = time.time()
    opened = asyncio.run(composite.mirror_reversal([("ZECUSDT", "short"), ("TAOUSDT", "long")], {"ZECUSDT": -0.3, "TAOUSDT": 0.4}, now))
    assert [t["symbol"] for t in opened] == ["ZECUSDT"]
    t = opened[0]
    assert t["source"] == "reversal" and t["stop_loss"] == pytest.approx(110.0) and t["notional_usd"] == pytest.approx(5_000.0)
    assert t["risk_usd"] == pytest.approx(500.0, abs=5) and t["exit_policy"] == "manual_lock" and t["lock_trail_atr"] == 1.0 and t["atr_pct"] == 4.0
    stored = next(x for x in w.get_portfolio()["open_trades"] if x["id"] == t["id"])
    assert stored["expires_at"] == pytest.approx(now + 72 * 3600, abs=2)                  # the cap is stamped after the open
    # a source's trade as it opened: the copy-stocks short at its tight stop is capped at 25% of equity
    src = {"id": "c1", "symbol": "PLTRUSDT", "direction": "short", "entry_price": 150.0, "stop_loss": 153.0, "opened_at": now, "oracle_reasoning": "leader x"}
    t2 = asyncio.run(composite.mirror("copy_stocks", src, now))
    assert t2 and t2["notional_usd"] == pytest.approx(12_500.0, rel=0.01) and t2["sizing_cap"]["bound"] == "notional_cap" and t2["source_trade_id"] == "c1"
    # the same symbol again, a long, an unknown source, a stale offer: none open
    assert asyncio.run(composite.mirror("copy_stocks", src, now)) is None
    assert asyncio.run(composite.mirror("weekly", {**src, "symbol": "SOLUSDT", "direction": "long", "stop_loss": 147.0}, now)) is None
    assert asyncio.run(composite.offer("cascade", "SOLUSDT", "short", 80.0, 88.0, "x", None, now)) is None
    assert asyncio.run(composite.mirror("weekly", {**src, "symbol": "SOLUSDT", "opened_at": now - 3600}, now)) is None
    s = composite.status()
    assert s["phase"] == "running" and len(s["open"]) == 2 and s["by_source"]["reversal"]["open"] == 1 and s["by_source"]["copy_stocks"]["open"] == 1
    why = [e["why"] for e in s["log"] if e["event"] == "refused"]
    assert any("already open" in x for x in why) and any("not one of the book's sources" in x for x in why)
    assert not any("longs are not taken" in x for x in why)                  # longs are refused quietly


def test_the_floors_halt_then_flatten_and_the_drawdown_stops_the_book(book):
    w, prices = book
    now = time.time()
    asyncio.run(composite.tick(now))
    coins = ["ZECUSDT", "TAOUSDT", "NEARUSDT", "SOLUSDT"]
    for c in coins:
        prices[c] = 100.0
    opened = asyncio.run(composite.mirror_reversal([(c, "short") for c in coins], {}, now))
    assert len(opened) == 4                                                    # 4 × $5,000 at a 10% stop: $500 of risk each
    # +1.6% against the shorts: −$320 = 0.64%, under the 0.75% halt
    w.update_prices({c: 101.6 for c in coins})
    out = asyncio.run(composite.tick(now + 60))
    assert out["assess"]["action"] == "ok" and out["acted"] is None
    # +1.9%: −$380 ≈ 0.78% (fees on top): halt, nothing new opens
    w.update_prices({c: 101.9 for c in coins})
    out = asyncio.run(composite.tick(now + 120))
    assert out["assess"]["action"] == "halt" and composite.status()["halted_day"] == composite.day_key(now + 120)
    prices["PLTRUSDT"] = 150.0
    assert asyncio.run(composite.offer("weekly", "PLTRUSDT", "short", 150.0, 165.0, "x", None, now + 121)) is None
    # +2.6%: −$520 ≥ 1%: everything is flattened
    w.update_prices({c: 102.6 for c in coins})
    prices.update({c: 102.6 for c in coins})
    out = asyncio.run(composite.tick(now + 180))
    assert out["acted"] and len(out["acted"]["flattened"]) == 4 and w.get_portfolio()["open_trades"] == []
    s = composite.status()
    assert s["flattens"] == 1 and s["halts"] == 1 and s["phase"] == "running" and s["assess"]["daily_loss_pct"] >= 1.0
    # a new day: the floors re-base on the balance; four more legs stop out in full (−$2,000) → the 5% drawdown stops the book
    st = composite._state(); st["day"] = "2000-01-01"; composite._save(st)
    asyncio.run(composite.tick(now + 86_400))
    assert composite.status()["assess"]["daily_loss_pct"] == 0.0
    for c in coins:
        prices[c] = 100.0
    assert len(asyncio.run(composite.mirror_reversal([(c, "short") for c in coins], {}, now + 86_401))) == 4
    w.update_prices({c: 110.5 for c in coins})
    prices.update({c: 110.5 for c in coins})
    out = asyncio.run(composite.tick(now + 86_460))
    s = composite.status()
    assert s["phase"] == "stopped" and s["stopped"]["why"].startswith("drawdown") and w.get_portfolio()["open_trades"] == []
    assert asyncio.run(composite.offer("weekly", "PLTRUSDT", "short", 150.0, 165.0, "x", None, now + 86_500)) is None
    # the record: two trading days, both negative, the verdict waits for 40 legs and 20 days
    assert s["daily"]["trading_days"] >= 1 and s["daily"]["net"] < -2_000 and s["verdict"]["status"] == "stopped"
    assert s["closed_n"] == 8 and s["by_source"]["reversal"]["n"] == 8
    # reset: running again from the plan's money, the ledger kept
    st = asyncio.run(composite.reset("test"))
    assert st["phase"] == "running" and composite.status()["closed_n"] == 0            # the record restarts with the floors
