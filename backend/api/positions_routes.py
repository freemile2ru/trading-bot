"""Actions on open positions and orders from the Trades page — one entry point for every system.

Each action goes to the system that owns the position, through the same code its own screen
uses, so live positions still act on Binance (real market close, real exchange stop moves):
  main, main_live      close · edit stop/target · cancel limit orders   (the Trade tab's wallet)
  auto, auto_live      close · edit stop/target · cancel limit orders   (autotrader cooldowns apply)
  scalper(_live), impulse   retired — history only
  hold72               read-only (experiment) — its pairs are in their own ledger
  experiment books     read-only — acting by hand would spoil the experiment
"""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from experiments import books as experiment_books
from trading import monitor

router = APIRouter(prefix="/positions", tags=["positions"])

SWING = ("main", "main_live", "auto", "auto_live")
PAIR_BOOKS = ("hold72", "carry", "fade24", "events", "carry_q", "unlocks", "vol_premium")


class Levels(BaseModel):
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)


def _swing_wallet(system: str):
    from live import swing as live_swing
    base = system.removesuffix("_live")
    return live_swing.LIVE[base] if system.endswith("_live") else live_swing.paper(base)


def _refuse_experiment(system: str) -> None:
    from markets import engine as markets
    if system in experiment_books.LABELS or system in PAIR_BOOKS or system in markets.WALLETS:
        raise HTTPException(409, "experiment books are managed by the experiment — acting by hand would spoil the test")


def _row(system: str, trade: dict) -> dict:
    return {k: trade.get(k) for k in ("id", "symbol", "direction", "entry_price", "stop_loss", "take_profit_2",
                                      "current_price", "pnl_usd", "close_reason", "status")} | {"system": system}


@router.post("/{system}/{trade_id}/close")
async def close(system: str, trade_id: str):
    _refuse_experiment(system)
    if system in ("auto", "auto_live"):
        from autotrader import engine as autotrader
        closed = await autotrader.close(trade_id)          # releases the coin, starts the cooldown
    elif system in ("main", "main_live"):
        wallet = _swing_wallet(system)
        trade = next((t for t in wallet.get_portfolio()["open_trades"] if t["id"] == trade_id), None)
        if not trade:
            raise HTTPException(404, "position not found")
        price = trade["current_price"] if system.endswith("_live") else (await monitor.live_price(trade["symbol"]) or trade["current_price"])
        closed = await asyncio.to_thread(wallet.close_trade_manually, trade_id, price, "manual")
    elif system in ("scalper", "scalper_live", "impulse"):
        raise HTTPException(409, "this scalper was retired — its trades are history only")
    else:
        raise HTTPException(404, f"unknown system {system}")
    if not closed:
        raise HTTPException(502, "close failed — for live positions check the Live tab's activity log")
    return _row(system, closed)


@router.post("/{system}/{trade_id}/levels")
async def set_levels(system: str, trade_id: str, req: Levels):
    _refuse_experiment(system)
    if system not in SWING:
        raise HTTPException(409, "this system's stops and targets are managed by its engine")
    if req.stop_loss is None and req.take_profit is None:
        raise HTTPException(400, "give a stop, a target, or both")
    result = await asyncio.to_thread(_swing_wallet(system).update_levels, trade_id, req.stop_loss, req.take_profit)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return _row(system, result)


@router.delete("/orders/{system}/{order_id}")
async def cancel_order(system: str, order_id: str):
    _refuse_experiment(system)
    if system in ("auto", "auto_live"):
        from autotrader import engine as autotrader
        cancelled = await autotrader.cancel(order_id)
    elif system in ("main", "main_live"):
        cancelled = await asyncio.to_thread(_swing_wallet(system).cancel_order, order_id, "manual")
    else:
        raise HTTPException(409, "this system's orders are managed by its engine")
    if not cancelled:
        raise HTTPException(404, "order not found (it may have filled)")
    return cancelled


@router.get("/orders")
async def open_orders():
    """Resting limit orders in every swing book (scalper entries live for seconds and aren't listed)."""
    def load():
        ledgers = [(s, _swing_wallet(s)) for s in SWING] + list(experiment_books.WALLETS.items())
        out = []
        for system, w in ledgers:
            for o in w.get_orders(1)["pending"]:
                out.append({"system": system, "id": o["id"], "symbol": o["symbol"], "direction": o["direction"],
                            "limit_price": o["limit_price"], "notional_usd": o.get("notional_usd"), "leverage": o.get("leverage"),
                            "stop_loss": o["stop_loss"], "take_profit": o.get("take_profit_2"), "placed_at": o.get("placed_at"),
                            "expires_at": o.get("expires_at"), "current_price": o.get("current_price")})
        return sorted(out, key=lambda o: -(o["placed_at"] or 0))
    return await asyncio.to_thread(load)


@router.get("/{system}/{trade_id}")
async def detail(system: str, trade_id: str):
    """Everything recorded about one trade — its full story, for the Trades page drawer."""
    def load():
        from markets import engine as markets
        if system in SWING or system in experiment_books.LABELS or system in markets.WALLETS:
            w = (experiment_books.WALLETS.get(system) or markets.WALLETS.get(system)) if system not in SWING else _swing_wallet(system)
            data = w.all_trades()
            t = next((t for t in data["closed"] + data["open"] if t["id"] == trade_id), None)
            return None if t is None else {**t, "system": system, "kind": "swing"}
        if system in PAIR_BOOKS:
            from experiments import carry, carry_q, events, fade24, hold72, unlocks, vol_premium
            mods = {"hold72": hold72, "carry": carry, "fade24": fade24, "events": events, "carry_q": carry_q,
                    "unlocks": unlocks, "vol_premium": vol_premium}
            row = next((r for r in mods[system].rows() if r["id"] == trade_id), None)
            return None if row is None else {**row, "system": system, "kind": "pair"}
        import json
        import sqlite3
        from scalper import config as SC
        path = SC.IMPULSE_TRADES_DB if system == "impulse" else SC.TRADES_DB
        con = sqlite3.connect(path, timeout=10)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if row is None:                                   # both scalpers are retired: nothing is open
            return None
        t = dict(row)
        t["features"] = json.loads(t.get("features") or "{}")
        return {**t, "system": system, "kind": "scalp"}
    t = await asyncio.to_thread(load)
    if t is None:
        raise HTTPException(404, "trade not found")
    return t
