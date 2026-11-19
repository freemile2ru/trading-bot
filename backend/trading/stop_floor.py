"""The stop floor (2026-11-20): no book sets a stop closer than the symbol's noise.

The data review of 2026-11-19 found every loss on production inside the first hour and every one a stop-out: the
auction book's stops averaged 0.9%, the prop book's 1.0%, the crypto copies' 2.8%. A stop under about 1.5% on a stock
perp or 2% on an alt was a coin flip minus fees on both ledgers (sub-hour trades −0.21R over 39 on prod, −0.21R over 66
locally), while the same positions held an hour or more were positive. The floor widens such a stop to the minimum
for its market; the caller sizes at the widened stop, so the risk is unchanged and only the noise is given room.
"""
from __future__ import annotations

import os

MIN_STOP_PCT = {"stocks": float(os.getenv("STOP_FLOOR_STOCKS_PCT", "1.5")),
                "futures": float(os.getenv("STOP_FLOOR_FUTURES_PCT", "1.5")),
                "crypto": float(os.getenv("STOP_FLOOR_CRYPTO_PCT", "2.0"))}
REGISTERED = "2026-11-20"


def min_pct(market: str | None) -> float:
    return MIN_STOP_PCT.get(market or "crypto", MIN_STOP_PCT["crypto"])


def apply(px: float, stop: float, direction: str, market: str | None) -> tuple[float, bool]:
    """(stop, floored): the stop moved out to the market's floor when it sat closer than that to `px`."""
    if not px or not stop:
        return stop, False
    s = 1 if direction == "long" else -1
    floor = min_pct(market) / 100
    if abs(px - stop) / px >= floor - 1e-9:
        return stop, False
    return round(px * (1 - s * floor), 8), True


def describe() -> dict:
    return {"min_stop_pct": MIN_STOP_PCT, "registered": REGISTERED,
            "rule": "a stop closer than the floor is widened to it and the leg sized at the widened stop"}
