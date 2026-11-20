"""Actions on open positions and orders from the Trades page — one entry point for every system.

Each action goes to the system that owns the position, through the same code its own screen
uses, so live positions still act on Binance (real market close, real exchange stop moves):
  main, main_live      close · edit stop/target · cancel limit orders   (the Trade tab's wallet)
  auto, auto_live      close · edit stop/target · cancel limit orders   (autotrader cooldowns apply)
  scalper(_live), impulse   retired — history only
  hold72               read-only (experiment) — its pairs are in their own ledger
  experiment books     close by hand (2026-11-17, the user: on production they are the books, not experiments);
                       stops, targets and orders stay with the engine
Every close is previewed first (GET …/preview): the live mark, the fill with slippage, the fee it costs and the net P&L.
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
    from tenancy import context as tenant, wallets as tenant_wallets
    base = system.removesuffix("_live")
    if not tenant.is_owner():
        if system.endswith("_live"):
            raise HTTPException(403, "not available to subscriber accounts")
        return tenant_wallets.get(tenant.tenant_id(), base)                     # a subscriber acts on their own legs only
    return live_swing.LIVE[base] if system.endswith("_live") else live_swing.paper(base)


def _refuse_experiment(system: str) -> None:
    from markets import engine as markets
    from tenancy import context as tenant
    if not tenant.is_owner():
        return                                                                  # a subscriber's mirror legs are theirs to close
    if system in experiment_books.LABELS or system in PAIR_BOOKS or system in markets.WALLETS:
        raise HTTPException(409, "experiment books are managed by the experiment — acting by hand would spoil the test")


def _book_wallet(system: str):
    """The paper wallet of an experiment or markets book (None for the swing systems, pair books and scalpers)."""
    from markets import engine as markets
    from tenancy import context as tenant, wallets as tenant_wallets
    if not tenant.is_owner():
        return tenant_wallets.get(tenant.tenant_id(), system) if system in tenant_wallets.existing(tenant.tenant_id()) else None
    return experiment_books.WALLETS.get(system) or markets.WALLETS.get(system)


def _open_trade(system: str, trade_id: str) -> tuple[object, dict]:
    w = _swing_wallet(system) if system in SWING else _book_wallet(system)
    if w is None:
        raise HTTPException(409, "pair books and retired scalpers have nothing to close by hand" if system in PAIR_BOOKS or system in ("scalper", "scalper_live", "impulse")
                            else f"unknown system {system}")
    trade = next((t for t in w.get_portfolio()["open_trades"] if t["id"] == trade_id), None)
    if not trade:
        raise HTTPException(404, "position not found")
    return w, trade


async def _mark(system: str, trade: dict) -> float:
    return trade["current_price"] if system.endswith("_live") else (await monitor.live_price(trade["symbol"]) or trade["current_price"])


@router.get("/{system}/{trade_id}/preview")
async def preview(system: str, trade_id: str):
    """What closing now would book: the mark, the fill after slippage, the closing fee, funding so far and the net P&L —
    shown before the Close button asks for confirmation (2026-11-17)."""
    import copy
    from trading import futures
    from trading.paper_trader import _refresh
    _, trade = _open_trade(system, trade_id)
    mark = await _mark(system, trade)
    t = copy.deepcopy(trade)
    fill = futures.slipped(mark, t["direction"], opening=False)
    exit_fee = futures.fee(t["quantity"] * fill, maker=False)
    fees_so_far = float(t.get("fees_usd") or 0.0)
    t["fees_usd"] = round(fees_so_far + exit_fee, 4)
    _refresh(t, fill)
    s = 1 if t["direction"] == "long" else -1
    gross = (fill - t["entry_price"]) * s * t["quantity"] + float(t.get("realized_pnl_usd") or 0.0)
    return {"system": system, "id": trade_id, "symbol": t["symbol"], "direction": t["direction"], "quantity": t["quantity"],
            "notional_usd": t.get("notional_usd"), "entry_price": t["entry_price"], "mark": mark, "fill": fill,
            "gross_pnl": round(gross, 2), "fees_so_far": round(fees_so_far, 2), "exit_fee": round(exit_fee, 2),
            "funding": round(float(t.get("funding_usd") or 0.0), 2), "net_pnl": round(float(t["pnl_usd"]), 2),
            "margin_usd": t.get("margin_usd"), "live": system.endswith("_live"),
            "note": "a live close fills at the exchange's price; the slippage and fee here are estimates" if system.endswith("_live") else None}


def _row(system: str, trade: dict) -> dict:
    return {k: trade.get(k) for k in ("id", "symbol", "direction", "entry_price", "stop_loss", "take_profit_2",
                                      "current_price", "pnl_usd", "close_reason", "status")} | {"system": system}


@router.post("/{system}/{trade_id}/close")
async def close(system: str, trade_id: str):
    if system in ("auto", "auto_live"):
        from autotrader import engine as autotrader
        closed = await autotrader.close(trade_id)          # releases the coin, starts the cooldown
    else:
        # The main wallet and every experiment / markets book: closed at the live mark, as "manual" — the record keeps
        # the hand that closed it, and a copy's leader-bound shadows end with it (paper_trader._close_open).
        wallet, trade = _open_trade(system, trade_id)
        price = await _mark(system, trade)
        closed = await asyncio.to_thread(wallet.close_trade_manually, trade_id, price, "manual")
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
