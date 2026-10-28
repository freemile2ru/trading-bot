"""Ops: the data watchdog's status (the app's "market data stopped" banner)."""

from fastapi import APIRouter

from ops import watchdog

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/watchdog")
async def status():
    return watchdog.status()


@router.get("/market")
async def market_state():
    """BTC trend, volatility and activity (quiet / normal / volatile) right now — the header's market chip."""
    from trading import regime
    return await regime.current() or {}


@router.get("/calendar")
async def calendar_status():
    """Upcoming FOMC/CPI releases and stock-perp earnings, and whether a blackout is on (the header chip)."""
    from ops import calendar
    return calendar.status()


@router.get("/calendar/page")
async def calendar_page():
    """The Calendar tab: every upcoming release with its rule, BTC's reaction to recent ones, and the near-event trade split."""
    from ops import calendar
    return await calendar.page()
