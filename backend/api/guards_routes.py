"""Guards: the direction gate, open-risk cap and strategy reviews after consistent losses."""

import asyncio

from fastapi import APIRouter, HTTPException

from trading import guards

router = APIRouter(prefix="/guards", tags=["guards"])


@router.get("")
async def status():
    return await asyncio.to_thread(guards.status)


@router.post("/{family}/clear")
async def clear(family: str):
    """Undo a family's adjustments now (they'd expire on their own)."""
    if family not in guards.FAMILIES:
        raise HTTPException(404, f"no family {family}")
    guards.clear(family)
    return await asyncio.to_thread(guards.status)
