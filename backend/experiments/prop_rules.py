"""Propr's rulebook as code — the Classic 1-Step evaluation the prop book (experiments/prop.py) is judged against.

Read from propr.xyz/rules (v1.0.5, 2026-07-27), /terms and /funded-terms (2026-08-26) and the API's
GET /leverage-limits/effective on 2026-11-09:
  target          10% of the starting balance, on REALISED balance; no time limit, no minimum days, no consistency rule
  daily loss      3% of the START-OF-DAY BALANCE (realised only), reset 00:00 UTC — breached when EQUITY (floating
                  P&L counts) touches start-of-day balance − 3%; one touch, no grace, the account is closed for good
  max drawdown    6% of the starting balance, static (never trails), on equity; a touch is fatal
  positions       6 open at once (the $25K card); leverage BTC/ETH 5×, everything else 2× — what the API enforces
                  (the rulebook's 10×/4× table is not what an account gets); every Hyperliquid perp, HIP-3 stocks included
  funded          the same daily and total limits with no target; 80% of profit in USDC on demand; fees pass through
  allowed         bots and automation, any strategy, weekend and news holding, copy trading; no stop-loss required
  not allowed     hedging the same name across accounts or venues, latency games, off-the-shelf herd bots
  cost            $275 for the $25K account, non-refundable; evaluations are paper on Hyperliquid's prices

Our own rules, pre-registered 2026-11-09 before the book's first trade: the firm's floors end the account, so the book
acts first — no new entries once today's loss reaches 1.5% or the drawdown 4%; everything closed at 2.25% today or 5%
total (three quarters of each limit, room for the minute between ticks). A reversal leg risks 0.4% of the account at
its 10% stop, an auction trade 1% at its level stop; at most 4 reversal legs and 2 auction trades of the firm's 6.
The equity the rules see is the firm's: starting balance + realised P&L since the attempt began + floating P&L.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

PLAN = {"firm": "Propr", "name": "Classic 1-Step", "size": 25_000.0, "fee_usd": 275.0, "target_pct": 10.0, "daily_loss_pct": 3.0,
        "max_dd_pct": 6.0, "dd_mode": "static", "max_open": 6, "profit_split": 0.8, "reset_utc": "00:00",
        "source": "https://propr.xyz/rules"}
LEVERAGE_DEFAULT = 2                                  # GET /leverage-limits/effective: {"defaultMax": 2, "overrides": {"BTC": 5, "ETH": 5}}
LEVERAGE = {"BTC": 5, "ETH": 5}
HALT_DAILY_PCT, FLATTEN_DAILY_PCT = 1.5, 2.25         # ours, of the firm's 3%
HALT_DD_PCT, FLATTEN_DD_PCT = 4.0, 5.0                # ours, of the firm's 6%
RISK_PCT = {"reversal": 0.4, "auction": 1.0}          # % of the account at the stop, per source
MAX_OPEN_BY_SOURCE = {"reversal": 4, "auction": 2}
# Binance's "1000X" perps are Hyperliquid's "kX".
THOUSANDS = {"1000PEPE": "kPEPE", "1000SHIB": "kSHIB", "1000BONK": "kBONK", "1000FLOKI": "kFLOKI", "1000LUNC": "kLUNC",
             "1000SATS": "kSATS", "1000RATS": "kRATS", "1000CAT": "kCAT", "1000CHEEMS": "kCHEEMS", "1000WHY": "kWHY", "1000MOG": "kMOG",
             "1000000MOG": "kMOG", "1000NEIRO": "kNEIRO", "1000DOGS": "kDOGS", "1000X": "kX"}


def day_key(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def resolve(symbol: str, universe: dict[str, dict]) -> str | None:
    """The Hyperliquid coin for a Binance symbol, or None when the firm cannot trade it. `universe` is
    {coin: {"szDecimals", "maxLeverage"}} over both dexes (native perps as "BTC", HIP-3 as "xyz:NVDA")."""
    from data.hyperliquid_client import XYZ_TO_BINANCE
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    for xyz, b in XYZ_TO_BINANCE.items():
        if b == symbol and f"xyz:{xyz}" in universe:
            return f"xyz:{xyz}"
    for coin in (base, THOUSANDS.get(base), f"xyz:{base}"):
        if coin and coin in universe:
            return coin
    return None


def leverage_cap(coin: str) -> int:
    return LEVERAGE.get(coin, LEVERAGE_DEFAULT)


def floors(start: float, sod_balance: float) -> dict[str, float]:
    """The firm's two floors and our four, as equity levels."""
    return {"daily": sod_balance * (1 - PLAN["daily_loss_pct"] / 100), "max": start * (1 - PLAN["max_dd_pct"] / 100),
            "halt_daily": sod_balance * (1 - HALT_DAILY_PCT / 100), "flatten_daily": sod_balance * (1 - FLATTEN_DAILY_PCT / 100),
            "halt_dd": start * (1 - HALT_DD_PCT / 100), "flatten_dd": start * (1 - FLATTEN_DD_PCT / 100)}


def assess(equity: float, start: float, sod_balance: float) -> dict:
    """Where the account stands against every floor: the firm's breach (fatal), our action (ok / halt / flatten),
    today's loss and the drawdown as % of the limit used, and the dollars of room left."""
    f = floors(start, sod_balance)
    daily_loss_pct = max(0.0, (sod_balance - equity) / sod_balance * 100) if sod_balance else 0.0
    dd_pct = max(0.0, (start - equity) / start * 100) if start else 0.0
    breach = "max_daily_loss" if equity <= f["daily"] else "max_drawdown" if equity <= f["max"] else None
    if equity <= f["flatten_daily"] or equity <= f["flatten_dd"]:
        action, why = "flatten", (f"today's loss {daily_loss_pct:.2f}% ≥ {FLATTEN_DAILY_PCT}%" if equity <= f["flatten_daily"]
                                  else f"drawdown {dd_pct:.2f}% ≥ {FLATTEN_DD_PCT}%")
    elif equity <= f["halt_daily"] or equity <= f["halt_dd"]:
        action, why = "halt", (f"today's loss {daily_loss_pct:.2f}% ≥ {HALT_DAILY_PCT}%" if equity <= f["halt_daily"]
                               else f"drawdown {dd_pct:.2f}% ≥ {HALT_DD_PCT}%")
    else:
        action, why = "ok", None
    return {"breach": breach, "action": action, "why": why,
            "daily_loss_pct": round(daily_loss_pct, 3), "dd_pct": round(dd_pct, 3),
            "daily_used_pct": round(daily_loss_pct / PLAN["daily_loss_pct"] * 100, 1), "dd_used_pct": round(dd_pct / PLAN["max_dd_pct"] * 100, 1),
            "room_today_usd": round(equity - f["daily"], 2), "room_total_usd": round(equity - f["max"], 2),
            "floors": {k: round(v, 2) for k, v in f.items()}}


def passed(balance: float, start: float) -> bool:
    return balance >= start * (1 + PLAN["target_pct"] / 100) - 1e-6


def target_progress_pct(balance: float, start: float) -> float:
    return round((balance - start) / (start * PLAN["target_pct"] / 100) * 100, 1) if start else 0.0


def size(equity: float, px: float, stop_px: float, source: str, cap: int) -> tuple[float, int]:
    """Notional and leverage for a leg: RISK_PCT of the account at the stop, never more than half the leverage cap's
    worth of the account (two positions of one class can't outrun the margin), at the asset's cap."""
    stop_frac = abs(px - stop_px) / px if px else 0.0
    if stop_frac <= 0:
        return 0.0, cap
    notional = min(equity * RISK_PCT[source] / 100 / stop_frac, equity * cap / 2)
    return round(notional, 2), cap


def quantity(notional: float, px: float, sz_decimals: int) -> str:
    """A base-asset quantity string rounded DOWN to the coin's size decimals (what the API takes)."""
    q = notional / px if px else 0.0
    step = 10 ** -sz_decimals
    q = math.floor(q / step) * step
    return f"{q:.{sz_decimals}f}"
