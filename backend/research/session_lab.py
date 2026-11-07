"""Session asymmetry — do crypto returns accrue in one part of the day, and is that tradeable after costs?

Why (Aalto 2026 thesis on BTC/USDT 2017–2026: a significant overnight premium in the Asian session over the full
sample, and a US-overnight premium that grew after the spot ETFs; the "overnight vs trading hours" split quoted at
+0.09% vs −0.03% a day; Padysak & Vojtko on weekends): Bitcoin's gains cluster outside US equity hours. We tested
only the Monday Asia open (research/monday_lab.py). This measures every session, every day of the week.

Run: venv/bin/python -m research.session_lab   (cached 15m candles, 40 perps, a year; writes
research_cache/session_results.json)

  sessions  UTC: asia 00:00–08:00, europe 08:00–13:30, us 13:30–21:00, late 21:00–24:00; weekday vs weekend
  measure   the average return of each session, per coin and for BTC, with a 95% block bootstrap; the US cash
            session against the rest ("overnight" = 21:00 → 13:30 next day)
  rule      hold BTC (and the equal-weight basket) only through the session the first half picks as best, flat
            otherwise — one round trip a day at 16 bps; judged on the second half. The honest bar is high: 16 bps a
            day is ~6% a year of costs, which a session premium of a few bps a day does not pay.
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from research.swing_lab import CACHE_DIR

KLINES = os.path.join(CACHE_DIR, "klines15m_365d_40c.pkl")
OUT = os.path.join(CACHE_DIR, "session_results.json")
COST = 0.0016
SESSIONS = {"asia": (0, 8), "europe": (8, 13.5), "us": (13.5, 21), "late": (21, 24)}


def session_returns(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (day, session): the close-to-close return over the session's window."""
    c = df["close"]
    hours = c.index.hour + c.index.minute / 60
    out = []
    for day, g in c.groupby(c.index.normalize()):
        h = hours[c.index.normalize() == day]
        for name, (a, b) in SESSIONS.items():
            win = g[(h >= a) & (h < b)]
            if len(win) < 4:
                continue
            start = c.shift(1).get(win.index[0])                      # the close before the window's first bar
            if start is None or np.isnan(start):
                continue
            out.append((day, name, float(win.iloc[-1] / start - 1), day.weekday() >= 5))
    return pd.DataFrame(out, columns=["day", "session", "ret", "weekend"])


def _ci(v: np.ndarray, seed=1, block=5, boot=2000) -> tuple[float, float]:
    rng, n, means = random.Random(seed), len(v), []
    if n == 0:
        return 0.0, 0.0
    for _ in range(boot):
        ix: list[int] = []
        while len(ix) < n:
            st = rng.randrange(0, max(n - block, 1)); ix += range(st, st + block)
        means.append(v[ix[:n]].mean())
    means.sort()
    return means[int(boot * .025)], means[int(boot * .975)]


def table(sr: pd.DataFrame) -> dict:
    out = {}
    for (name, wk), g in sr.groupby(["session", "weekend"]):
        daily = g.groupby("day")["ret"].mean()
        lo, hi = _ci(daily.values)
        out[f"{name}_{'weekend' if wk else 'weekday'}"] = {"days": int(len(daily)), "bps_day": round(float(daily.mean() * 1e4), 2),
                                                             "ci95_bps": [round(lo * 1e4, 2), round(hi * 1e4, 2)], "positive_pct": round(float((daily > 0).mean() * 100), 1)}
    for name, g in sr.groupby("session"):
        daily = g.groupby("day")["ret"].mean()
        lo, hi = _ci(daily.values)
        out[name] = {"days": int(len(daily)), "bps_day": round(float(daily.mean() * 1e4), 2), "ci95_bps": [round(lo * 1e4, 2), round(hi * 1e4, 2)]}
    return out


def rule(sr: pd.DataFrame, session: str) -> pd.Series:
    daily = sr[sr["session"] == session].groupby("day")["ret"].mean() - COST
    return daily


def main() -> dict:
    kl = pd.read_pickle(KLINES)
    per = {s: session_returns(df) for s, df in kl.items()}
    basket = pd.concat(per.values(), ignore_index=True)
    days = sorted(basket["day"].unique())
    split = days[len(days) // 2]
    res = {"coins": len(kl), "period": [str(days[0].date()), str(days[-1].date())], "split": str(split.date()), "cost_bps": COST * 1e4,
           "btc": {"all": table(per["BTCUSDT"])}, "basket": {"all": table(basket)}}
    # the overnight-vs-US split the papers quote
    for key, sr in (("btc", per["BTCUSDT"]), ("basket", basket)):
        us = sr[sr["session"] == "us"].groupby("day")["ret"].mean()
        rest = sr[sr["session"] != "us"].groupby(["day", "session"])["ret"].mean().groupby("day").sum()   # mean across coins, summed over the sessions
        res[key]["us_vs_rest_bps_day"] = {"us_cash_session": round(float(us.mean() * 1e4), 2), "outside_us": round(float(rest.mean() * 1e4), 2)}
        # the rule: best session on the first half, judged on the second
        first, second = sr[sr["day"] < split], sr[sr["day"] >= split]
        scores = {n: float(rule(first, n).mean()) for n in SESSIONS}
        best = max(scores, key=scores.get)
        held = rule(second, best)
        lo, hi = _ci(held.values)
        bh = second.groupby(["day", "session"])["ret"].mean().groupby("day").sum()                   # the equal-weight basket, all four sessions
        res[key]["rule"] = {"chosen_session": best, "first_half_bps_day_after_costs": {n: round(v * 1e4, 2) for n, v in scores.items()},
                            "second_half": {"days": int(len(held)), "bps_day_after_costs": round(float(held.mean() * 1e4), 2), "ci95_bps": [round(lo * 1e4, 2), round(hi * 1e4, 2)],
                                            "buy_and_hold_bps_day": round(float(bh.mean() * 1e4), 2)},
                            "verdict": "passes" if lo > 0 else "no edge"}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = main()
    print("period", r["period"], "split", r["split"])
    for key in ("btc", "basket"):
        t = r[key]["all"]
        print(f"[{key}] bps/day:", {n: (t[n]["bps_day"], t[n]["ci95_bps"]) for n in SESSIONS}, "| weekday/weekend:",
              {n: (t[f'{n}_weekday']['bps_day'], t[f'{n}_weekend']['bps_day']) for n in SESSIONS if f'{n}_weekend' in t})
        print(f"[{key}] us vs rest:", r[key]["us_vs_rest_bps_day"], "| rule:", r[key]["rule"]["chosen_session"], r[key]["rule"]["second_half"], "→", r[key]["rule"]["verdict"])
