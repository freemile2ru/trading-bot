"""Scalper execution costs — fees and fill prices.

Two kinds of change live here, and they are different in nature:

  * Real savings (you only get them if you actually trade this way / hold this account):
      - BNB fee payment: Binance USDⓈ-M takes 10% off fees paid in BNB.
      - VIP tiers: set SCALPER_MAKER_FEE / SCALPER_TAKER_FEE to your account's rates.
      - Passive exits (engine): non-urgent exits rest at the touch as maker first.

  * A more accurate estimate (not a saving): taker fills walk the visible order book for
    our size, plus a small latency allowance, instead of a flat 3 bps on top of crossing
    the spread. For a few-thousand-dollar clip in the deepest perpetual books the book
    walk is usually the touch price; where depth is thin, it charges the real levels —
    and beyond visible depth it assumes the worst.
"""

import os

from trading import futures

BNB_DISCOUNT = 0.10 if os.getenv("SCALPER_BNB_FEES", "false").lower() == "true" else 0.0
MAKER_FEE = float(os.getenv("SCALPER_MAKER_FEE", str(futures.MAKER_FEE))) * (1 - BNB_DISCOUNT)
TAKER_FEE = float(os.getenv("SCALPER_TAKER_FEE", str(futures.TAKER_FEE))) * (1 - BNB_DISCOUNT)
# Price drift between deciding to exit and the order reaching the book (~100–300ms).
LATENCY_BPS = float(os.getenv("SCALPER_LATENCY_BPS", "0.5"))
# Beyond the 5 visible levels we don't know the book — assume it's this much worse.
BEYOND_DEPTH_BPS = float(os.getenv("SCALPER_BEYOND_DEPTH_BPS", "5.0"))


def fee(notional: float, maker: bool) -> float:
    return abs(notional) * (MAKER_FEE if maker else TAKER_FEE)


def walk(levels: list[tuple[float, float]], qty: float, worse: int) -> tuple[float | None, float]:
    """VWAP for taking `qty` from `levels` (best first). `worse` = +1 when buying (prices
    rise as we eat asks), −1 when selling. Returns (vwap, share filled from visible depth)."""
    if not levels or qty <= 0:
        return None, 0.0
    remaining, cost = qty, 0.0
    for price, size in levels:
        take = min(remaining, size)
        cost += take * price
        remaining -= take
        if remaining <= 1e-12:
            return cost / qty, 1.0
    visible = qty - remaining
    last = levels[-1][0] * (1 + worse * BEYOND_DEPTH_BPS / 1e4)
    cost += remaining * last
    return cost / qty, visible / qty


def taker_exit_price(side: str, qty: float, depth: tuple[list, list] | None, trigger: float) -> float:
    """Where a market exit of `qty` really fills.

    A long exit sells into the bids; a short exit buys the asks. The fill is the worse of
    the book walk and the trigger price (the book snapshot can be up to 100ms old), then
    a latency allowance against us.
    """
    selling = side == "long"
    bids, asks = depth or ([], [])
    vwap, _ = walk(bids if selling else asks, qty, worse=-1 if selling else 1)
    if vwap is None:
        vwap = trigger
    px = min(vwap, trigger) if selling else max(vwap, trigger)
    return px * (1 - LATENCY_BPS / 1e4) if selling else px * (1 + LATENCY_BPS / 1e4)


def breakeven_win_rate(tp_bps: float, sl_bps: float) -> float:
    """Win rate needed to break even: a win nets the target minus maker in and out; a loss
    costs the stop plus maker in, taker out and latency."""
    win = tp_bps - 2 * MAKER_FEE * 1e4
    loss = sl_bps + (MAKER_FEE + TAKER_FEE) * 1e4 + LATENCY_BPS
    return round(loss / (win + loss) * 100, 1)


def expected_stop_cost_frac() -> float:
    """Planning figure for sizing: maker in, taker out, latency (book walk ≈ 0 for our size)."""
    return MAKER_FEE + TAKER_FEE + LATENCY_BPS / 1e4
