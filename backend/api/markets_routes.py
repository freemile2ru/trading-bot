"""The Markets tab: stocks and futures (commodity) perpetual books — trend, rules, Claude-confirmed."""

import asyncio

from fastapi import APIRouter, HTTPException

from markets import engine as markets

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("/{market}")
async def market(market: str):
    if market not in markets.MARKETS:
        raise HTTPException(404, f"unknown market {market} (stocks or futures)")
    return await asyncio.to_thread(markets.summary, market)


@router.post("/{market}/sweep")
async def sweep_now(market: str):
    """Scan the market now (the loop does this every 15 minutes)."""
    if market not in markets.MARKETS:
        raise HTTPException(404, f"unknown market {market}")
    await markets.sweep(market)
    return await asyncio.to_thread(markets.summary, market)
