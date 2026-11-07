"""The 2026-11-07 candidate labs (basis ranking, stock-perp open, quarter-hour clock, sessions, ETF flows, reversal): their pure parts."""
import numpy as np
import pandas as pd

from research import basis_xs_lab as bx
from research import quarter_hour_lab as qh
from research import reversal_lab as rv
from research import session_lab as se
from research import stock_open_lab as so


def _days(n, start="2025-01-01"):
    return pd.date_range(start, periods=n, freq="D", tz="UTC")


def test_reversal_rebalances_rank_the_past_and_charge_both_legs():
    days = _days(20)
    # ten coins: coin i rises i% a day, so the past-7-day winners keep winning (momentum pays, reversal loses)
    closes = pd.DataFrame({f"C{i}": 100 * (1 + i / 100.0) ** np.arange(20) for i in range(20)}, index=days)
    rv_ = rv.rebalances(closes, rv.SIGNS["reversal"], 7, 1)
    mo_ = rv.rebalances(closes, rv.SIGNS["momentum"], 7, 1)
    assert len(rv_) == len(mo_) == 12 and (mo_["ret"] > 0).all() and (rv_["ret"] < 0).all()
    assert abs(mo_["ret"].iloc[0] - (-rv_["ret"].iloc[0] - 2 * rv.COST)) < 1e-12     # the same legs, opposite sign, costs both ways


def test_basis_rebalances_net_the_funding_paid_over_the_hold():
    days = _days(12)
    data = {}
    for i in range(20):
        prem = 0.0005 * (i - 10)                                                     # coin 19 has the richest premium, coin 0 the cheapest
        data[f"C{i}"] = pd.DataFrame({"close": [100.0] * 12, "prem": [prem] * 12, "fund": [prem * 3] * 12, "qv": [1.0] * 12}, index=days)
    tr = bx.rebalances(data, bx.SIGNS["long_low_basis"], 1, 1)
    # flat prices: the long-low basket is paid the (negative) funding it receives and the short-high basket is paid the funding the longs pay
    assert len(tr) == 10 and tr["legs"].iloc[0] == 2 * bx.K
    expected = (np.mean([-(0.0005 * (i - 10)) * 3 for i in range(bx.K)] + [(0.0005 * (i - 10)) * 3 for i in range(20 - bx.K, 20)]) - bx.COST)
    assert tr["ret"].iloc[0] == np.float64(expected) or abs(tr["ret"].iloc[0] - expected) < 1e-12


def test_stock_sessions_take_the_gap_from_the_prior_close_and_fade_or_follow_it():
    idx = pd.DatetimeIndex([t for d in ("2026-06-01", "2026-06-02", "2026-06-03")                       # three cash days of 15m bars, 09:30–15:45 ET
                            for t in pd.date_range(f"{d} 13:30", periods=26, freq="15min", tz="UTC")])
    px = np.full(len(idx), 100.0)
    day2 = (idx.date == idx[0].date() + pd.Timedelta(days=1))
    px[day2] = 102.0                                                                  # day 2 gaps up 2% and sits there
    px[day2 & (idx.hour * 60 + idx.minute >= 14 * 60)] = 103.0                        # ... then runs on after 10:00 ET
    px[idx.date == idx[0].date() + pd.Timedelta(days=2)] = 103.0                     # day 3 opens where day 2 closed: no gap
    df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 1.0}, index=idx)
    ss = so.sessions(df)
    assert len(ss) == 2 and abs(ss["open"].iloc[0] / ss["prev_close"].iloc[0] - 1.02) < 1e-9
    fol = so.trades(ss, "follow", "10:30", 0.01)
    fad = so.trades(ss, "fade", "10:30", 0.01)
    assert len(fol) == 1 and abs(fol["ret"].iloc[0] - (103 / 102 - 1 - so.COST)) < 1e-9
    assert abs(fad["ret"].iloc[0] - (-(103 / 102 - 1) - so.COST)) < 1e-9
    assert so.trades(ss, "follow", "10:30", 0.03).empty                               # the 2% gap is below a 3% bar


def test_quarter_hour_events_take_the_mark_minutes_and_sign_by_taker_imbalance():
    idx = pd.date_range("2026-10-01", periods=24 * 60 * 2, freq="min", tz="UTC")
    c = 100 + np.cumsum(np.where(idx.minute == 0, 0.1, 0.0))                           # price steps up 0.1 at every hour mark
    df = pd.DataFrame({"o": c, "c": c, "v": 10.0, "buy": np.where(idx.minute.isin((0, 30)), 9.0, 5.0)}, index=idx)
    ev = qh.events(df, qh.MARKS)
    assert set(ev.index.minute) == {0, 15, 30, 45}
    assert ev.loc[ev.index.minute == 0, "imb"].iloc[0] == 0.8 and ev.loc[ev.index.minute == 15, "imb"].iloc[0] == 0.0
    assert (ev.dropna(subset=["fwd_1h"])["fwd_1h"] > 0).all()                        # the price only ever rises
    ctl = qh.events(df, qh.CONTROLS)
    assert set(ctl.index.minute) == {7, 22, 37, 52}
    tr = qh.rule_trades(df, ev, 0.5, 4)
    assert len(tr) >= 1 and (tr["ret"] > -qh.COST).all() and all(t.minute in (0, 30) for t in tr["ts"])


def test_session_returns_split_the_day_into_the_four_windows():
    idx = pd.date_range("2026-10-05", periods=96 * 3, freq="15min", tz="UTC")          # Mon–Wed; the step is on the Tuesday
    c = pd.Series(100.0, index=idx)
    mins = idx.hour * 60 + idx.minute
    tue = idx[0].normalize() + pd.Timedelta(days=1)
    c[(mins >= 13 * 60 + 30) & (mins < 21 * 60) & (idx.normalize() == tue)] = 101.0     # a 1% step through the US session, back after
    df = pd.DataFrame({"close": c, "open": c, "high": c, "low": c, "volume": 1.0})
    sr = se.session_returns(df)
    d1 = sr[sr["day"] == tue].set_index("session")["ret"]
    assert abs(d1["us"] - 0.01) < 1e-6 and abs(d1["asia"]) < 1e-9 and d1["late"] < 0  # up in the US window, back down after it
    assert not sr["weekend"].any()
