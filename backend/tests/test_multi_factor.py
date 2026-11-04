"""The multi-factor basket: family targets, risk-parity combination, and a low-turnover daily rebalance."""
import asyncio
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from experiments import books, multi_factor as mf
from storage import state as kv
from trading import book_switches
from trading.paper_trader import PaperWallet

COINS = [f"C{i}USDT" for i in range(20)]          # the funding family needs 2 × FUND_K coins with data


def _panel(seed=1, days=140):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-05-01", periods=days, freq="1D", tz="UTC")
    rets = rng.normal(0.0, 0.03, size=(days, len(COINS)))
    rets[:, :3] += 0.015                      # three strong risers, three strong fallers: momentum has something to rank
    rets[:, -3:] -= 0.015
    daily = pd.DataFrame(100 * np.cumprod(1 + rets, axis=0), index=idx, columns=COINS)
    funding = pd.DataFrame(rng.normal(0.0001, 0.0002, size=(days, len(COINS))), index=idx, columns=COINS)
    return daily, funding


def test_family_targets_have_the_right_shape():
    daily, funding = _panel()
    fams = mf.family_targets(daily, funding, epoch_day=0, stored={})
    xs, fund, ts = fams["xsmom"], fams["funding"], fams["tsmom"]
    assert sum(1 for v in xs.values() if v > 0) == mf.XS_K and sum(1 for v in xs.values() if v < 0) == mf.XS_K
    assert abs(sum(xs.values())) < 1e-9 and abs(sum(abs(v) for v in xs.values()) - 1) < 1e-9       # dollar-neutral, gross 1
    assert sum(1 for v in fund.values() if v > 0) == mf.FUND_K and sum(1 for v in fund.values() if v < 0) == mf.FUND_K
    assert all(v > 0 for v in ts.values()) and sum(ts.values()) <= 1.0 + 1e-9                     # long-flat, capped
    assert set(["C0USDT", "C1USDT", "C2USDT"]) <= {s for s, v in xs.items() if v > 0}              # the risers lead
    # Off the rebalance grid the ranked families keep their stored targets.
    again = mf.family_targets(daily, funding, epoch_day=1, stored=fams)
    assert again["xsmom"] == xs and again["funding"] == fund


def test_risk_parity_weights_sum_and_shares():
    daily, funding = _panel()
    fams = mf.family_targets(daily, funding, 0, {})
    combined, shares = mf.risk_parity(daily, funding, fams)
    assert abs(sum(shares.values()) - 1) < 0.01 and all(v > 0 for v in shares.values())
    assert sum(abs(v) for v in combined.values()) <= 1.0 + 1e-6                                   # gross at most 1


@pytest.fixture
def book(monkeypatch, tmp_path):
    wallet = PaperWallet(str(tmp_path / "w.db"), label="multi_factor")
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(books, "WALLETS", {**books.WALLETS, "multi_factor": wallet})
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    daily, funding = _panel()

    async def panel(now):
        return daily, funding
    monkeypatch.setattr(mf, "panel", panel)
    last = daily.iloc[-1]

    async def price(sym):
        return float(last[sym])
    monkeypatch.setattr(mf.monitor, "live_price", price)
    monkeypatch.setattr(mf, "universe", lambda: COINS)
    for k in ("family_targets",):
        kv.delete_sync(kv.key("multi_factor", k))
    return wallet


def test_rebalance_opens_both_sides_once_a_day_and_keeps_positions_within_tolerance(book):
    day1 = datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)
    out = asyncio.run(mf.rebalance(now=day1))
    held = {t["symbol"]: t for t in book.get_portfolio()["open_trades"]}
    assert out and out["longs"] > 0 and out["shorts"] > 0 and len(held) == len(out["opened"])
    gross = sum(t["notional_usd"] for t in held.values())
    assert gross <= book.get_portfolio()["total_equity"] * 1.05                                    # gross ≤ 1× equity
    assert all(t["exit_policy"] == "manual" and t["leverage"] == mf.LEVERAGE for t in held.values())
    assert asyncio.run(mf.rebalance(now=day1.replace(hour=9))) is None                            # done today
    assert asyncio.run(mf.rebalance(now=day1.replace(hour=0, minute=5))) is None                  # not before 00:10
    # The next day with the same data: nothing should churn.
    out2 = asyncio.run(mf.rebalance(now=day1.replace(day=30)))
    assert out2 and out2["opened"] == [] and out2["closed"] == [] and len(out2["kept"]) == len(held)
    kv.delete_sync(kv.key("multi_factor", "done", "2026-10-29"), kv.key("multi_factor", "done", "2026-10-30"))


def test_a_process_started_after_the_slot_waits_for_tomorrows(book, monkeypatch):
    """A reset on 2026-11-04 restarted the process at 17:20 UTC and the basket went on at its first tick, 17:29, into
    the afternoon's tape (TRUMP −5% in the next five minutes). Now it waits for the next 00:10 slot."""
    boot = datetime(2026, 11, 4, 17, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(mf, "_BOOT_TS", boot.timestamp())
    assert asyncio.run(mf.rebalance(now=boot.replace(minute=29))) is None                       # first tick: deferred
    assert book.get_portfolio()["open_trades"] == []
    assert kv.get_sync(kv.key("multi_factor", "done", "2026-11-04"))["deferred"]
    out = asyncio.run(mf.rebalance(now=datetime(2026, 11, 5, 0, 12, tzinfo=timezone.utc)))        # tomorrow's slot
    assert out and out["opened"]
    boot2 = datetime(2026, 11, 6, 0, 4, tzinfo=timezone.utc)                                        # a restart just before the slot
    monkeypatch.setattr(mf, "_BOOT_TS", boot2.timestamp())
    assert asyncio.run(mf.rebalance(now=boot2.replace(minute=15))) is not None                   # runs in its slot
    kv.delete_sync(*[kv.key("multi_factor", "done", d) for d in ("2026-11-04", "2026-11-05", "2026-11-06")])
