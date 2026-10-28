"""USDT-M perpetual futures model: leverage, isolated margin, liquidation, fees, funding.

Sizing principle — leverage is a ceiling, not a target. Every position is sized by the
risk at its stop (a fixed % of equity); leverage only decides how much margin that
notional locks up. Each position then gets the highest leverage (≤ MAX_LEVERAGE) whose
liquidation price sits at least LIQ_BUFFER × the stop distance beyond the stop. So a
2% stop can use 20x, while a 4% stop is held to ~11x — at 20x a long is liquidated
~4.6% below entry, and a stop that close to liquidation is one wick from never firing.

Setting FUTURES_MAX_LEVERAGE=1 and FUTURES_FUNDING=false models plain spot.
"""

import math
import os

MAX_LEVERAGE = int(os.getenv("FUTURES_MAX_LEVERAGE", "20"))
MARGIN_MODE = "isolated"   # liquidation is computed per position; one position can't drain another

# Binance USDⓈ-M VIP0: maker 0.02%, taker 0.05%.
MAKER_FEE = float(os.getenv("FUTURES_MAKER_FEE", "0.0002"))
TAKER_FEE = float(os.getenv("FUTURES_TAKER_FEE", "0.0005"))
# Market and stop orders cross the spread and can slip; limits and targets rest in the book.
SLIPPAGE = float(os.getenv("FUTURES_SLIPPAGE", "0.0003"))
FUNDING_ENABLED = os.getenv("FUTURES_FUNDING", "true").lower() != "false"

# Maintenance margin rate for the smallest notional bracket. Binance: 0.4% BTC/ETH,
# 0.5–1% for most alts. Using the alt figure for everything but the majors is conservative.
MMR_MAJORS = {"BTCUSDT": 0.004, "ETHUSDT": 0.004}
MMR_DEFAULT = float(os.getenv("FUTURES_MMR", "0.01"))

LIQ_BUFFER = float(os.getenv("FUTURES_LIQ_BUFFER", "2.0"))   # liquidation ≥ this × stop distance away


def mmr(symbol: str) -> float:
    return MMR_MAJORS.get(symbol, MMR_DEFAULT)


def liquidation_price(entry: float, leverage: float, direction: str, symbol: str) -> float:
    """Isolated-margin liquidation for a one-way position (fees and tier amounts ignored).

    Long:  entry × (1 − 1/L + mmr)      Short: entry × (1 + 1/L − mmr)
    """
    m = mmr(symbol)
    if direction == "long":
        return entry * (1 - 1 / leverage + m)
    return entry * (1 + 1 / leverage - m)


def safe_leverage(stop_pct: float, symbol: str) -> int:
    """Highest whole leverage ≤ MAX_LEVERAGE that keeps liquidation LIQ_BUFFER× beyond the stop.

    Distance to liquidation ≈ 1/L − mmr, so we need 1/L − mmr ≥ LIQ_BUFFER × stop.
    """
    needed = LIQ_BUFFER * stop_pct / 100 + mmr(symbol)
    return max(1, min(MAX_LEVERAGE, math.floor(1 / needed)))


def fee(notional: float, maker: bool) -> float:
    return abs(notional) * (MAKER_FEE if maker else TAKER_FEE)


def slipped(price: float, direction: str, opening: bool) -> float:
    """Taker fill price after slippage — always against the trader."""
    worse_up = (direction == "long") == opening     # buying: long entry or short exit
    return price * (1 + SLIPPAGE) if worse_up else price * (1 - SLIPPAGE)


def describe() -> dict:
    return {
        "max_leverage": MAX_LEVERAGE, "margin_mode": MARGIN_MODE,
        "maker_fee": MAKER_FEE, "taker_fee": TAKER_FEE, "slippage": SLIPPAGE,
        "funding": FUNDING_ENABLED, "liq_buffer": LIQ_BUFFER, "mmr_default": MMR_DEFAULT,
    }
