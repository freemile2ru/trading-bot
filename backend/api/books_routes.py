"""The Books tab: every trading book with its on/off switch."""

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from trading import book_switches

router = APIRouter(prefix="/books", tags=["books"])


class Switch(BaseModel):
    enabled: bool


@router.get("")
async def books(ledger: str = Query("both", pattern="^(both|paper|live)$")):
    return await asyncio.to_thread(book_switches.catalog, ledger)


@router.post("/{name}")
async def switch(name: str, req: Switch):
    """Turn a book's NEW entries on or off. Open positions are still managed either way."""
    try:
        await book_switches.set_enabled(name, req.enabled)
    except KeyError:
        raise HTTPException(404, f"no book called {name}")
    except PermissionError as e:
        raise HTTPException(409, f"{name} can't be switched here: {e}")
    return next(b for b in (await asyncio.to_thread(book_switches.catalog))["books"] if b["name"] == name)
