"""The market recorder's status. (Both scalpers' routes were removed when they were retired;
their trades stay readable through the Trades page and the Retired tab.)"""

from fastapi import APIRouter, HTTPException

from scalper import recorder

recorder_router = APIRouter(prefix="/recorder", tags=["recorder"])


@recorder_router.get("")
async def status():
    """Is the 1-second bar recorder running, and how much history does it hold per coin?"""
    if recorder.engine is None:
        raise HTTPException(503, "the market recorder isn't running (RECORDER_ENABLED=false?)")
    return recorder.engine.status()
