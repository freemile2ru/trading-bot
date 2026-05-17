"""Paper trading simulator — thread-safe with atomic writes and equity history."""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

TRADES_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "paper_trades.json")

# Module-level lock — all public functions acquire this before touching the file
_lock = threading.Lock()


def _load() -> dict:
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            return json.load(f)
    return {
        "open": [],
        "closed": [],
        "balance": 10000.0,
        "initial_balance": 10000.0,
        "equity_history": [{"ts": _now(), "equity": 10000.0}],
    }


def _save(state: dict) -> None:
    """Atomic write: write to .tmp then rename — never leaves a half-written file."""
    os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
    tmp = TRADES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, TRADES_FILE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_equity(state: dict) -> None:
    """Append current equity to history, keeping max 500 entries."""
    open_pnl = sum(t["pnl_usd"] for t in state["open"])
    equity = round(state["balance"] + open_pnl, 2)
    history = state.setdefault("equity_history", [])
    history.append({"ts": _now(), "equity": equity})
    if len(history) > 500:
        state["equity_history"] = history[-500:]


def open_trade(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit_1: float,
    take_profit_2: float,
    size_pct: float = 2.0,
    timeframe: str = "1h",
    oracle_confidence: int = 50,
    oracle_reasoning: str = "",
) -> dict:
    with _lock:
        state = _load()
        balance = state["balance"]
        position_value = balance * (size_pct / 100)
        quantity = position_value / entry_price if entry_price else 0
        risk_amount = abs(entry_price - stop_loss) * quantity
        reward_1 = abs(take_profit_1 - entry_price) * quantity

        trade = {
            "id": str(uuid.uuid4())[:8],
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "current_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit_1": take_profit_1,
            "take_profit_2": take_profit_2,
            "quantity": round(quantity, 6),
            "position_value_usd": round(position_value, 2),
            "size_pct": size_pct,
            "timeframe": timeframe,
            "oracle_confidence": oracle_confidence,
            "oracle_reasoning": oracle_reasoning,
            "opened_at": _now(),
            "closed_at": None,
            "status": "open",
            "pnl_usd": 0.0,
            "pnl_pct": 0.0,
            "risk_usd": round(risk_amount, 2),
            "reward_1_usd": round(reward_1, 2),
            "risk_reward": round(reward_1 / risk_amount, 2) if risk_amount else 0,
            "close_reason": None,
        }
        state["open"].append(trade)
        _snapshot_equity(state)
        _save(state)
        return trade


def update_prices(price_map: dict[str, float]) -> list[dict]:
    """Update all open trade prices. Returns list of auto-closed trades."""
    with _lock:
        state = _load()
        auto_closed: list[dict] = []
        still_open: list[dict] = []

        for trade in state["open"]:
            price = price_map.get(trade["symbol"], trade["current_price"])
            trade["current_price"] = price

            if trade["direction"] == "long":
                pnl_per_unit = price - trade["entry_price"]
            else:
                pnl_per_unit = trade["entry_price"] - price

            trade["pnl_usd"] = round(pnl_per_unit * trade["quantity"], 2)
            trade["pnl_pct"] = round(pnl_per_unit / trade["entry_price"] * 100, 2)

            closed = False
            if trade["direction"] == "long":
                if price <= trade["stop_loss"]:
                    trade = _close_trade(trade, price, "stop_loss")
                    closed = True
                elif price >= trade["take_profit_2"]:
                    trade = _close_trade(trade, price, "take_profit_2")
                    closed = True
                elif price >= trade["take_profit_1"] and trade.get("close_reason") != "tp1_hit":
                    trade["close_reason"] = "tp1_hit"
            elif trade["direction"] == "short":
                if price >= trade["stop_loss"]:
                    trade = _close_trade(trade, price, "stop_loss")
                    closed = True
                elif price <= trade["take_profit_2"]:
                    trade = _close_trade(trade, price, "take_profit_2")
                    closed = True
                elif price <= trade["take_profit_1"] and trade.get("close_reason") != "tp1_hit":
                    trade["close_reason"] = "tp1_hit"

            if closed:
                state["closed"].append(trade)
                state["balance"] = round(state["balance"] + trade["pnl_usd"], 2)
                auto_closed.append(trade)
            else:
                still_open.append(trade)

        state["open"] = still_open
        if price_map:
            _snapshot_equity(state)
        _save(state)
        return auto_closed


def close_trade_manually(trade_id: str, current_price: float, reason: str = "manual") -> Optional[dict]:
    with _lock:
        state = _load()
        trade = next((t for t in state["open"] if t["id"] == trade_id), None)
        if not trade:
            return None

        trade = _close_trade(trade, current_price, reason)
        state["open"] = [t for t in state["open"] if t["id"] != trade_id]
        state["closed"].append(trade)
        state["balance"] = round(state["balance"] + trade["pnl_usd"], 2)
        _snapshot_equity(state)
        _save(state)
        return trade


def _close_trade(trade: dict, price: float, reason: str) -> dict:
    if trade["direction"] == "long":
        pnl_per_unit = price - trade["entry_price"]
    else:
        pnl_per_unit = trade["entry_price"] - price

    trade["pnl_usd"] = round(pnl_per_unit * trade["quantity"], 2)
    trade["pnl_pct"] = round(pnl_per_unit / trade["entry_price"] * 100, 2)
    trade["current_price"] = price
    trade["closed_at"] = _now()
    trade["status"] = "closed"
    trade["close_reason"] = reason
    return trade


def get_portfolio() -> dict:
    with _lock:
        state = _load()

    open_pnl = sum(t["pnl_usd"] for t in state["open"])
    closed_pnl = sum(t["pnl_usd"] for t in state["closed"])

    wins = [t for t in state["closed"] if t["pnl_usd"] > 0]
    losses = [t for t in state["closed"] if t["pnl_usd"] <= 0]
    win_rate = len(wins) / len(state["closed"]) * 100 if state["closed"] else 0
    avg_win = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss else float("inf")

    total_equity = state["balance"] + open_pnl
    total_return_pct = (total_equity - state["initial_balance"]) / state["initial_balance"] * 100

    return {
        "balance": round(state["balance"], 2),
        "initial_balance": state["initial_balance"],
        "total_equity": round(total_equity, 2),
        "open_pnl": round(open_pnl, 2),
        "realized_pnl": round(closed_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "open_trades": state["open"],
        "closed_trades": state["closed"][-20:],
        "equity_history": state.get("equity_history", [])[-100:],
        "stats": {
            "total_trades": len(state["closed"]),
            "open_trades": len(state["open"]),
            "win_rate_pct": round(win_rate, 1),
            "avg_win_usd": round(avg_win, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999,
            "best_trade": max((t["pnl_usd"] for t in state["closed"]), default=0),
            "worst_trade": min((t["pnl_usd"] for t in state["closed"]), default=0),
        },
    }


def reset_portfolio(starting_balance: float = 10000.0) -> dict:
    with _lock:
        state = {
            "open": [],
            "closed": [],
            "balance": starting_balance,
            "initial_balance": starting_balance,
            "equity_history": [{"ts": _now(), "equity": starting_balance}],
        }
        _save(state)
    return {"message": "Portfolio reset", "balance": starting_balance}
