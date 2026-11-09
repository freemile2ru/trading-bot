"""Position sizing for the autotrader's reversal lane (2026-11-09, the user's ask after the first 42 legs).

What the legs showed (prod 18, local 24): one notional for every coin while the coins' daily ranges differ 3.5× (BTC
3.0% a day, ENA 10.6%); a flat 10% "disaster" stop that made R a fiction (the one leg that reached it lost 5× a normal
leg); and the lane allocator's on/off multiplier (1.0 or 0.25 on a 20-trade cell) that cut the whole lane to a quarter
after a single loss. So, pre-registered before any leg is sized by it:

  volatility parity   a leg's expected daily swing is the same dollars for every coin: notional = ledger × RISK_PCT ÷
                      the coin's daily ATR% (14 days). $100 a day of swing on a $50k ledger: ~$1,100 of ZEC, ~$3,300 of BTC;
                      never more than LEG_CAP_PCT of the ledger.
  a real stop         STOP_ATR daily ATRs away (bounded STOP_MIN..STOP_MAX %), so every leg risks the same ledger share
                      at its stop (RISK_PCT × STOP_ATR) and a one-day disaster is about 1.5 normal losses.
  shrunken edge       the side's record in the regime (then the side, then the lane), its average R shrunk toward zero by
                      n/(n+SHRINK_N), mapped to a multiplier in MULT_RANGE: a fresh lane sizes at 1.0 and drifts with
                      evidence instead of jumping between 1.0 and 0.25.
  conviction tilt     the slot's strongest pick ×1.25, its weakest ×0.75 (the stronger-score half beat the weaker in both
                      samples: +0.20R vs +0.09R on prod, +0.05R vs −0.15R locally — small samples, so a tilt, not a filter).
  heat cap            the sum of expected daily swings across the ledger's open legs stays under HEAT_CAP_PCT of the
                      ledger (a new leg is scaled into the room left, or refused), and at most SIDE_CAP legs on one side.

Every input is written on the trade ("sizing") so the Books tab can judge each part on its own record.
"""
from __future__ import annotations

import logging
import os
import time

from storage import state as kv

logger = logging.getLogger("autotrader.sizing")

RISK_PCT = float(os.getenv("AUTOTRADER_SIZE_RISK_PCT", "0.2"))      # % of the ledger as one daily ATR of swing, per leg
STOP_ATR = float(os.getenv("AUTOTRADER_SIZE_STOP_ATR", "1.5"))      # the stop, in daily ATRs
STOP_MIN_PCT, STOP_MAX_PCT = 3.0, 15.0
LEG_CAP_PCT = float(os.getenv("AUTOTRADER_SIZE_LEG_CAP_PCT", "8"))   # % of the ledger, one leg (a 3% ATR coin wants 6.7% for $100 of swing on $50k)
HEAT_CAP_PCT = float(os.getenv("AUTOTRADER_SIZE_HEAT_CAP_PCT", "1"))  # % of the ledger: the open legs' daily swings together
SIDE_CAP = int(os.getenv("AUTOTRADER_SIZE_SIDE_CAP", "6"))
SHRINK_N = 20
MULT_RANGE = (0.5, 1.5)
MULT_GAIN = 2.0                                                      # multiplier = 1 + GAIN × shrunk average R
TILT_TOP, TILT_BOTTOM = 1.25, 0.75
LOCK_ARM_PCT = float(os.getenv("AUTOTRADER_LOCK_ARM_PCT", "1"))      # the reversal legs' profit lock arms at this % of notional (was the global 3%)
ATR_DAYS = 14
ATR_TTL = 6 * 3600
ATR_FALLBACK_PCT = 6.0                                               # when the feed is down: the traded coins' median
SINCE = 1794000000.0                                                 # 2026-11-06: legs sized by this module carry "sizing"


def _k(*parts: str) -> str:
    return kv.key("sizing", *parts)


async def daily_atr_pct(symbol: str, now: float | None = None) -> float | None:
    """The coin's 14-day average true range as % of its last close, from daily perpetual bars; cached six hours."""
    now = now or time.time()
    c = kv.get_sync(_k("atr", symbol)) or {}
    if c.get("at", 0) > now - ATR_TTL and c.get("pct"):
        return float(c["pct"])
    try:
        from data.binance_client import fetch_futures_klines
        bars = await fetch_futures_klines(symbol, "1d", ATR_DAYS + 1)
        if len(bars) < 5:
            return c.get("pct")
        trs = []
        for prev, b in zip(bars[:-1], bars[1:]):
            trs.append(max(b["high"] - b["low"], abs(b["high"] - prev["close"]), abs(b["low"] - prev["close"])))
        pct = sum(trs) / len(trs) / bars[-1]["close"] * 100
        kv.set_sync(_k("atr", symbol), {"at": now, "pct": round(pct, 3)}, ATR_TTL * 4)
        return round(pct, 3)
    except Exception as e:
        logger.warning("ATR for %s unavailable: %s", symbol, e)
        return c.get("pct")


def edge_multiplier(cell: dict | None) -> tuple[float, str]:
    """The record's say on size: average R shrunk toward zero by n/(n+SHRINK_N), mapped into MULT_RANGE."""
    n = int((cell or {}).get("n") or 0)
    avg = (cell or {}).get("avg_r")
    if not n or avg is None:
        return 1.0, "equal size — no record yet"
    shrunk = float(avg) * n / (n + SHRINK_N)
    mult = max(MULT_RANGE[0], min(MULT_RANGE[1], 1.0 + MULT_GAIN * shrunk))
    return round(mult, 3), f"×{mult:.2f} — {n} trades averaging {float(avg):+.2f}R, shrunk to {shrunk:+.3f}R"


def tilt_for(rank: int, n_picks: int) -> tuple[float, str]:
    """The slot's strongest pick (rank 0) is tilted up, its weakest down; a lone pick is not tilted."""
    if n_picks < 2:
        return 1.0, "no tilt — one pick in the slot"
    if rank == 0:
        return TILT_TOP, f"×{TILT_TOP} — the slot's strongest score"
    if rank == n_picks - 1:
        return TILT_BOTTOM, f"×{TILT_BOTTOM} — the slot's weakest score"
    return 1.0, "no tilt — mid-ranked"


def swing_usd(t: dict) -> float:
    """A leg's expected daily P&L swing: notional × its daily ATR% (the fallback when the leg was not sized here)."""
    atr = ((t.get("sizing") or {}).get("atr_pct")) or t.get("atr_pct") or ATR_FALLBACK_PCT
    return (t.get("notional_usd") or 0.0) * atr / 100


def heat_usd(open_legs: list[dict]) -> float:
    return sum(swing_usd(t) for t in open_legs)


def plan_leg(equity: float, atr_pct: float | None, cell: dict | None, rank: int, n_picks: int, open_legs: list[dict], direction: str) -> dict | None:
    """Everything a leg needs: notional, stop, and the reason for each part — or None with the refusal logged in it."""
    atr = atr_pct or ATR_FALLBACK_PCT
    same_side = sum(1 for t in open_legs if t.get("direction") == direction)
    if same_side >= SIDE_CAP:
        return {"refused": f"{same_side} {direction}s already on (cap {SIDE_CAP})"}
    base = equity * RISK_PCT / 100 / (atr / 100)
    mult, mwhy = edge_multiplier(cell)
    tilt, twhy = tilt_for(rank, n_picks)
    notional = min(base * mult * tilt, equity * LEG_CAP_PCT / 100)
    capped = notional < base * mult * tilt - 0.01
    used = heat_usd(open_legs)
    cap = equity * HEAT_CAP_PCT / 100
    room = cap - used
    if room <= 0:
        return {"refused": f"heat cap: ${used:,.0f} of daily swing already on against a ${cap:,.0f} cap"}
    swing = notional * atr / 100
    scaled = None
    if swing > room:
        scaled = round(room / swing, 3)
        notional *= scaled
        swing = room
    stop_pct = max(STOP_MIN_PCT, min(STOP_MAX_PCT, STOP_ATR * atr))
    why = (f"vol parity: ${equity * RISK_PCT / 100:,.0f} a day of swing ÷ {atr:.1f}% ATR = ${base:,.0f}; {mwhy}; {twhy}"
           + (f"; capped at {LEG_CAP_PCT:g}% of the ledger" if capped else "") + (f"; scaled ×{scaled} into the heat room" if scaled else "")
           + f"; stop {stop_pct:.1f}% ({STOP_ATR:g} ATR)")
    return {"notional": round(notional, 2), "stop_pct": round(stop_pct, 3), "atr_pct": round(atr, 3), "atr_known": atr_pct is not None,
            "base_notional": round(base, 2), "edge_mult": mult, "edge_why": mwhy, "tilt": tilt, "tilt_why": twhy, "rank": rank, "n_picks": n_picks,
            "heat_used_usd": round(used, 2), "heat_cap_usd": round(cap, 2), "swing_usd": round(swing, 2), "heat_scaled": scaled,
            "leg_capped": capped, "risk_pct": RISK_PCT, "stop_atr": STOP_ATR, "lock_arm_pct": LOCK_ARM_PCT, "why": why}


def cell_for(card: dict, lane: str, regime: str, direction: str) -> dict | None:
    """The first record cell with any trades: the side in this regime, the side over every regime, the lane."""
    c = card.get(lane) or {}
    for cell in ((c.get("by_regime_side") or {}).get(f"{regime}|{direction}"), (c.get("by_side") or {}).get(direction), c.get("all")):
        if cell and cell.get("n"):
            return cell
    return None


def describe() -> dict:
    return {"risk_pct": RISK_PCT, "stop_atr": STOP_ATR, "stop_range_pct": [STOP_MIN_PCT, STOP_MAX_PCT], "leg_cap_pct": LEG_CAP_PCT,
            "heat_cap_pct": HEAT_CAP_PCT, "side_cap": SIDE_CAP, "shrink_n": SHRINK_N, "mult_range": list(MULT_RANGE), "mult_gain": MULT_GAIN,
            "tilt": [TILT_TOP, TILT_BOTTOM], "lock_arm_pct": LOCK_ARM_PCT,
            "rule": f"A reversal leg's notional is {RISK_PCT:g}% of the ledger ÷ the coin's daily ATR% (equal dollars of daily swing), "
                    f"× the side's shrunken record ({MULT_RANGE[0]:g}–{MULT_RANGE[1]:g}), × {TILT_TOP:g}/{TILT_BOTTOM:g} for the slot's strongest/weakest "
                    f"score; stop {STOP_ATR:g} ATR; at most {LEG_CAP_PCT:g}% of the ledger a leg, {HEAT_CAP_PCT:g}% of daily swing across open legs, "
                    f"{SIDE_CAP} legs a side. Its profit lock arms at {LOCK_ARM_PCT:g}% of notional."}
