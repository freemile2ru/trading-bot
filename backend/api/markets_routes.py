"""The Markets tab: stocks and futures (commodity) perpetual books — trend, rules, Claude-confirmed."""

import asyncio

from fastapi import APIRouter, HTTPException

from markets import engine as markets

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("/{market}")
async def market(market: str):
    if market == "crypto":
        from autotrader import engine
        from markets import crypto
        from storage import state as kv
        states = await kv.hgetall(engine._state_key())
        return await asyncio.to_thread(crypto.summary, {s.get("symbol", k): s for k, s in states.items() if isinstance(s, dict)})
    if market not in markets.MARKETS:
        raise HTTPException(404, f"unknown market {market} (crypto, stocks or futures)")
    return await asyncio.to_thread(markets.summary, market)


@router.post("/{market}/sweep")
async def sweep_now(market: str):
    """Scan the market now (the loop does this every 15 minutes)."""
    if market not in markets.MARKETS:
        raise HTTPException(404, f"unknown market {market}")
    await markets.sweep(market)
    return await asyncio.to_thread(markets.summary, market)
