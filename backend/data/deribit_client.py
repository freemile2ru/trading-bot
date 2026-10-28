"""Deribit public market data (no key): option chains with mark price and implied vol, the index, and the daily
delivery prices that settle expiries. Used by the volatility-premium paper book — nothing is ever sent to Deribit.

Prices are quoted in the coin (a BTC option's mark of 0.0105 is 0.0105 BTC); `usd` converts at the underlying.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("data.deribit")

BASE = "https://www.deribit.com/api/v2/public"
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
TAKER_FEE = 0.0003             # 0.03% of the underlying per contract, capped at 12.5% of the option's price
SETTLE_FEE = 0.00015           # 0.015% of the underlying at delivery (only for in-the-money contracts; charged flat here)


def expiry_ts(name: str) -> float:
    """BTC-27NOV26-80000-C → expiry at 08:00 UTC on that day."""
    d = name.split("-")[1]
    day, mon, yr = int(d[:-5]), MONTHS[d[-5:-2]], 2000 + int(d[-2:])
    return datetime(yr, mon, day, 8, 0, tzinfo=timezone.utc).timestamp()


def parse(name: str) -> dict:
    coin, _, strike, kind = name.split("-")
    return {"coin": coin, "expiry_ts": expiry_ts(name), "strike": float(strike), "type": "call" if kind == "C" else "put"}


async def chain(currency: str) -> list[dict]:
    """Every live option with mark/bid/ask (in coin), mark IV (%), open interest and the underlying price."""
    from data.http import get_json
    d = await get_json(f"{BASE}/get_book_summary_by_currency", params={"currency": currency, "kind": "option"})
    out = []
    for x in d.get("result") or []:
        try:
            out.append({**parse(x["instrument_name"]), "name": x["instrument_name"], "mark": x.get("mark_price"),
                        "bid": x.get("bid_price"), "ask": x.get("ask_price"), "iv": x.get("mark_iv"),
                        "oi": x.get("open_interest"), "underlying": x.get("underlying_price")})
        except (KeyError, ValueError):
            continue
    return out


async def index_price(currency: str) -> float | None:
    from data.http import get_json
    d = await get_json(f"{BASE}/get_index_price", params={"index_name": f"{currency.lower()}_usd"})
    return (d.get("result") or {}).get("index_price")


async def delivery_price(currency: str, expiry: float) -> float | None:
    """The settlement price for an expiry (Deribit publishes one per day at 08:00 UTC)."""
    from data.http import get_json
    day = datetime.fromtimestamp(expiry, timezone.utc).strftime("%Y-%m-%d")
    d = await get_json(f"{BASE}/get_delivery_prices", params={"index_name": f"{currency.lower()}_usd", "count": 10})
    for row in ((d.get("result") or {}).get("data") or []):
        if row.get("date") == day:
            return float(row["delivery_price"])
    return None


def weekly_expiries(rows: list[dict], now: float | None = None, min_days: float = 5.0, max_days: float = 9.5) -> list[float]:
    """Expiry timestamps between min and max days out (the next weekly, normally a Friday)."""
    now = now or time.time()
    return sorted({r["expiry_ts"] for r in rows if min_days * 86400 <= r["expiry_ts"] - now <= max_days * 86400})


def fee_usd(option_px_coin: float, underlying: float, qty: float) -> float:
    """Deribit's taker fee for `qty` contracts: 0.03% of underlying, never more than 12.5% of the premium."""
    return round(qty * min(TAKER_FEE * underlying, 0.125 * option_px_coin * underlying), 4)
