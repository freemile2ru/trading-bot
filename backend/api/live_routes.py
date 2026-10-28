"""Live-trading control API — status, per-system paper/live switch, kill switch, logs."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from live import config as LC
from live import control

router = APIRouter(prefix="/live", tags=["live"])


class ModeRequest(BaseModel):
    system: str
    mode: str
    confirm: str = ""


@router.get("/status")
async def status():
    """Master switch, environment, keys and mode per system, and each live account's balance."""
    out = control.status()
    for s, info in out["systems"].items():
        if info["has_keys"] and LC.ALLOWED:
            try:
                info["account_balance"] = await asyncio.to_thread(lambda s=s: control.client(s).equity())
            except Exception as e:
                info["account_error"] = str(e)[:200]
    return out


@router.post("/mode")
async def set_mode(req: ModeRequest):
    """Switch a system between paper and live. Going live needs LIVE_TRADING_ALLOWED=true, keys,
    and the exact confirmation phrase from /live/status."""
    try:
        return await asyncio.to_thread(control.set_mode, req.system, req.mode, req.confirm)
    except control.LiveBlocked as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/kill")
async def kill(reason: str = Query("manual kill switch", max_length=200)):
    """Every system back to paper; cancel and close everything live on Binance."""
    return await asyncio.to_thread(control.kill_switch, reason)


@router.get("/activity")
async def activity(limit: int = Query(50, ge=1, le=500)):
    """Recent live events, orders and exchange requests."""
    def load():
        events = control.rows("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
        orders = control.rows("SELECT * FROM orders ORDER BY updated DESC LIMIT ?", (limit,))
        audit = control.rows("SELECT ts, system, method, path, status, ms, response FROM audit "
                             "ORDER BY ts DESC LIMIT ?", (limit,))
        for e in events:
            e["data"] = json.loads(e["data"] or "{}")
        for a in audit:
            a["response"] = (a["response"] or "")[:300]
        return {"events": events, "orders": orders, "audit": audit}
    return await asyncio.to_thread(load)
