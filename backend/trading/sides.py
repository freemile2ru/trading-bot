"""Side switches — which books take longs (2026-11-17, the history review).

Every lane's long side loses and its short side wins, on both instances: locally the weekly lane's shorts are 10 of 10
won (+$929) against its longs −$1,496, the factor lane +$971 / −$440, the TA lane's 12 longs −$401 with no shorts taken,
the weekly_rs book +$192 / −$330; across the research ledger 351 longs lost −$7,283 at 43% won, and the 11-12 entry
study found long entries random (E-ratio ≈ 1) where shorts carry edge. Copy longs were switched off on production the
same day with a rule for their return (experiments/copy_trading.longs_switch); this is the same switch for the lanes.

Rule: a book in LONGS_OFF opens no long. Each refused long is written down at its price (experiments/entry_cells.refuse_long,
flag "side_off") and scored 24h later, so the switch keeps a record of what it cost or saved. Longs return to a book on
their own once its last BACK_N refused longs, settled, averaged ≥ BACK_PCT over the 24h (net of the round trip) with both
halves positive — the same shape as the copy rule. The reversal lane keeps its own long shadow (entry_cells.shadow_reversal_longs),
the unlock and event books are short by construction, the cascade book retires at 120 trades, and the burst longs stay:
their short mirrors are the controlled comparison. Hedge legs are structure, not a view, and are never switched.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("trading.sides")

LONGS_OFF = tuple(b.strip() for b in os.getenv("LONGS_OFF", "auto:ta,auto:weekly,auto:factor,weekly_rs").split(",") if b.strip())
BACK_N = int(os.getenv("LONGS_BACK_N", "20"))
BACK_PCT = float(os.getenv("LONGS_BACK_PCT", "0.3"))
WHY = "side_off"


def long_record(book: str) -> dict:
    """The refused longs of `book`, scored 24h on: the last BACK_N settled, their mean %, the halves, whether they earned the side back."""
    from experiments import entry_cells
    rows = [r for r in entry_cells.gated_records(gate_only=False) if r.get("switch") == WHY and r.get("book") == book]
    done = sorted((r for r in rows if r.get("result")), key=lambda r: r["opened_ts"])[-BACK_N:]
    pct = [r["result"]["coin_pct"] for r in done]
    half = len(pct) // 2
    mean = sum(pct) / len(pct) if pct else None
    earned = len(pct) >= BACK_N and mean >= BACK_PCT and half > 0 and sum(pct[:half]) > 0 and sum(pct[half:]) > 0
    return {"n": len(rows), "settled": len(pct), "mean_pct": round(mean, 3) if mean is not None else None,
            "halves_pct": [round(sum(pct[:half]) / half, 3) if half else None, round(sum(pct[half:]) / (len(pct) - half), 3) if len(pct) - half else None],
            "wins": sum(1 for x in pct if x > 0), "earned": bool(earned)}


def longs_off(book: str) -> bool:
    """True when `book` takes no longs now: it is in LONGS_OFF and its refused longs have not earned the side back."""
    if book not in LONGS_OFF:
        return False
    try:
        return not long_record(book)["earned"]
    except Exception as e:                                   # a record that cannot be read never switches a side on
        logger.warning("Side switch record unreadable for %s (%s) — longs stay off", book, e)
        return True


async def refuse(book: str, symbol: str, now: float, price: float) -> str:
    """Write the refused long down and say why. The caller opens nothing."""
    from experiments import entry_cells
    try:
        await entry_cells.refuse_long(book, symbol, now, price, WHY)
    except Exception as e:
        logger.warning("Refused long %s %s not recorded: %s", book, symbol, e)
    return f"longs are off for {book} (2026-11-17) until its last {BACK_N} refused longs average ≥ {BACK_PCT:g}% over 24h"


def report() -> dict:
    """Every switched book: off now, and the record its longs must earn."""
    return {"rule": f"a listed book opens no long; longs return once its last {BACK_N} refused longs (scored 24h on, net of the round trip) "
                    f"average ≥ {BACK_PCT:g}% with both halves positive",
            "back_n": BACK_N, "back_pct": BACK_PCT,
            "books": {b: {"longs_off": longs_off(b), "record": long_record(b)} for b in LONGS_OFF}}
