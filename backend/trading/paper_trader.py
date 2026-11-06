"""Paper trading simulator for USDT-M perpetual futures — thread-safe, atomic writes.

Each `PaperWallet` is an independent isolated-margin futures account backed by its own
JSON file. The module-level functions operate on the default (main) wallet, which is
what the manual/oracle trading flow uses; the autonomous experiment in `autotrader/`
keeps a second wallet so its results never mix with the user's own.

Accounting (see `trading/futures.py` for the model):
  * `size_pct` is the position's NOTIONAL as a % of balance — with leverage it can exceed
    100. Callers normally size by risk instead (`risk_pct`: % of equity lost at the stop).
  * Each position gets its own leverage (≤ the max) so liquidation sits well beyond the
    stop; `margin_usd = notional / leverage` is locked while the position is open.
  * `pnl_usd` is net: price P&L (open remainder + realized partials) − fees − funding.
  * Liquidation loses exactly the position's isolated margin.
"""

import csv
import io
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from storage.db import SWING_DB, connect
from trading import exits, futures, trade_alerts

logger = logging.getLogger("trading.paper_trader")

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
TRADES_FILE = os.path.join(_DATA_DIR, "paper_trades.json")   # legacy — imported into SQLite once

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (label TEXT PRIMARY KEY, balance REAL NOT NULL,
    initial_balance REAL NOT NULL, updated_at REAL);
CREATE TABLE IF NOT EXISTS trades (label TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL,
    symbol TEXT, opened_at TEXT, closed_at TEXT, pnl_usd REAL, data TEXT NOT NULL,
    PRIMARY KEY (label, id));
CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(label, status, closed_at);
CREATE TABLE IF NOT EXISTS orders (label TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL,
    placed_at REAL, data TEXT NOT NULL, PRIMARY KEY (label, id));
CREATE TABLE IF NOT EXISTS shadows (label TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (label, id));
CREATE TABLE IF NOT EXISTS shadow_results (label TEXT NOT NULL, trade_id TEXT NOT NULL,
    policy TEXT NOT NULL, closed_ts REAL, data TEXT NOT NULL, PRIMARY KEY (label, trade_id, policy));
CREATE TABLE IF NOT EXISTS equity (label TEXT NOT NULL, ts TEXT NOT NULL, equity REAL NOT NULL,
    PRIMARY KEY (label, ts));
"""

MINUTE_MS = 60_000

# Exits that rest in the book (maker, no slippage); everything else crosses the spread.
_MAKER_EXITS = {"take_profit_2"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sign(trade: dict) -> int:
    return 1 if trade["direction"] == "long" else -1


def _price_pnl(trade: dict, price: float) -> float:
    return (price - trade["entry_price"]) * _sign(trade) * trade["quantity"] + trade.get("realized_pnl_usd", 0.0)


def _pnl(trade: dict, price: float) -> tuple[float, float]:
    """Net P&L (price P&L − fees − funding) and the % move on the average entry."""
    total = _price_pnl(trade, price) - trade.get("fees_usd", 0.0) - trade.get("funding_usd", 0.0)
    move = (price - trade["entry_price"]) * _sign(trade) / trade["entry_price"] * 100
    return round(total, 2), round(move, 2)


def _refresh(trade: dict, price: float) -> None:
    trade["current_price"] = price
    trade["pnl_usd"], trade["pnl_pct"] = _pnl(trade, price)
    margin = trade.get("margin_usd") or 0
    trade["roe_pct"] = round(trade["pnl_usd"] / margin * 100, 2) if margin else trade["pnl_pct"]


def _set_leverage(trade: dict, leverage: int) -> None:
    trade["leverage"] = leverage
    trade["notional_usd"] = round(trade["quantity"] * trade["entry_price"], 2)
    trade["position_value_usd"] = trade["notional_usd"]
    trade["margin_usd"] = round(trade["notional_usd"] / leverage, 2)
    trade["liquidation_price"] = round(
        futures.liquidation_price(trade["entry_price"], leverage, trade["direction"], trade["symbol"]), 8)


def _stop_pct(entry: float, stop: float) -> float:
    return abs(entry - stop) / entry * 100 if entry else 0.0


def _close_trade(trade: dict, price: float, reason: str) -> dict:
    """Settle the remainder. `price` is the trigger price; the fill adds slippage for takers."""
    if reason == "liquidated":
        fill = price
        # Price P&L at the liquidation price leaves the maintenance margin; the exchange
        # keeps that too, so the whole isolated margin is lost.
        # liquidation_price() is defined on the entry notional, so the remainder is too.
        trade["fees_usd"] = round(
            trade.get("fees_usd", 0.0) + trade["quantity"] * trade["entry_price"] * futures.mmr(trade["symbol"]), 4)
    else:
        maker = reason in _MAKER_EXITS
        fill = price if maker else futures.slipped(price, trade["direction"], opening=False)
        trade["fees_usd"] = round(trade.get("fees_usd", 0.0) + futures.fee(trade["quantity"] * fill, maker), 4)
    _refresh(trade, fill)
    trade["exit_trigger_price"] = price
    trade["closed_at"] = _now()
    trade["status"] = "closed"
    trade["close_reason"] = reason
    return trade


def _new_trade(
    equity: float, balance: float, symbol: str, direction: str, entry_price: float, stop_loss: float,
    take_profit_1: float, take_profit_2: float, size_pct: Optional[float], risk_pct: Optional[float],
    timeframe: str, oracle_confidence: int, oracle_reasoning: str, hold_duration: str,
    extra: Optional[dict], maker: bool, leverage: Optional[int] = None,
) -> dict:
    """Build a position at the actual fill (slippage applied for market entries)."""
    fill = entry_price if maker else futures.slipped(entry_price, direction, opening=True)
    stop_pct = _stop_pct(fill, stop_loss)
    if risk_pct is not None and stop_pct > 0:
        notional = equity * risk_pct / 100 / (stop_pct / 100)
    else:
        notional = balance * (size_pct or 0) / 100
    quantity = notional / fill if fill else 0
    lev = leverage or futures.safe_leverage(stop_pct, symbol)

    trade = {
        "id": str(uuid.uuid4())[:8],
        "symbol": symbol,
        "direction": direction,
        "entry_price": round(fill, 8),
        "requested_entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "quantity": round(quantity, 8),
        "size_pct": round(notional / balance * 100, 2) if balance else 0,
        "timeframe": timeframe,
        "oracle_confidence": oracle_confidence,
        "oracle_reasoning": oracle_reasoning,
        "hold_duration": hold_duration,
        "opened_at": _now(),
        "closed_at": None,
        "status": "open",
        "fees_usd": round(futures.fee(notional, maker), 4),
        "funding_usd": 0.0,
        "funding_through_ms": int(time.time() * 1000),
        "close_reason": None,
        **(extra or {}),
    }
    _set_leverage(trade, lev)
    risk = abs(fill - stop_loss) * trade["quantity"]
    reward_1 = abs(take_profit_1 - fill) * trade["quantity"]
    trade["risk_usd"] = round(risk, 2)
    trade["reward_1_usd"] = round(reward_1, 2)
    trade["risk_reward"] = round(reward_1 / risk, 2) if risk else 0
    exits.stamp(trade, exits.lock_policy_for((extra or {}).get("strategy"), (extra or {}).get("exit_policy")))
    _refresh(trade, fill)
    return trade


def _validate_levels(direction: str, entry: float, stop: float, liq: Optional[float] = None) -> Optional[str]:
    s = 1 if direction == "long" else -1
    if (entry - stop) * s <= 0:
        return f"stop {stop} is on the wrong side of entry {entry} for a {direction}"
    if liq is not None and (stop - liq) * s <= 0:
        return f"stop {stop} is beyond the liquidation price {liq:.6g}"
    return None


# Every exit decision — stops, targets, partials, trailing, stale/max-hold, liquidation —
# lives in trading/exits.py so real trades and shadow trades follow identical rules.
_evaluate_bar = exits.apply_bar


# A post-only (maker) order sits in the queue: price merely touching it doesn't mean it filled.
# It fills only once price trades THROUGH it by this much — the usual queue-position haircut.
MAKER_FILL_THROUGH = float(os.getenv("MAKER_FILL_THROUGH_BPS", "2")) / 1e4


def _order_touched(order: dict, h: float, l: float) -> bool:
    limit = order["limit_price"]
    if order.get("post_only"):
        return l <= limit * (1 - MAKER_FILL_THROUGH) if order["direction"] == "long" else h >= limit * (1 + MAKER_FILL_THROUGH)
    return l <= limit if order["direction"] == "long" else h >= limit


def _order_fill_price(order: dict, o: float) -> float:
    """A limit fills at its price, or better if the bar opened through it — except a resting
    post-only order, which fills at exactly its price."""
    limit = order["limit_price"]
    if order.get("post_only"):
        return limit
    return min(o, limit) if order["direction"] == "long" else max(o, limit)


def _normalize(trade: dict) -> dict:
    """Trades recorded before the futures model are treated as 1x positions."""
    if "leverage" not in trade:
        trade.setdefault("fees_usd", 0.0)
        trade.setdefault("funding_usd", 0.0)
        _set_leverage(trade, 1)
    return trade


class PaperWallet:
    def __init__(
        self,
        path: str,
        *,
        initial_balance: float = 10000.0,
        history_cap: int = 500,
        min_snapshot_seconds: float = 0.0,
        label: str | None = None,
        legacy_json: str | None = None,
    ):
        """`path` is the SQLite database; wallets sharing a file are separated by `label`.
        A `.json` path (older callers, tests) maps to the `.db` beside it and is imported."""
        if path.endswith(".json"):
            legacy_json = legacy_json or path
            path = path[:-5] + ".db"
        self.path = path
        self.label = label or os.path.splitext(os.path.basename(legacy_json or path))[0]
        self.initial_balance = initial_balance
        self.history_cap = history_cap
        # Price updates arrive every minute; a floor keeps a long-running wallet's history
        # covering days rather than hours without growing the file unbounded.
        self.min_snapshot_seconds = min_snapshot_seconds
        # Re-entrant: live trading calls back into the same wallet while it holds this lock
        # (the pre-trade daily-loss check reads its P&L). A plain Lock hung the whole app there.
        self._lock = threading.RLock()
        self._con = connect(path)
        self._con.executescript(SCHEMA)
        self._con.commit()
        # Reads use their own connection and never wait for the write lock: a live wallet holds
        # that through seconds of Binance calls every minute, and a read on the event loop
        # (autotrader, scanner, API) blocked every other task — websocket feeds included.
        self._rcon = connect(path)
        self._read_lock = threading.Lock()
        self._import_legacy_json(legacy_json)

    # ── Storage ──────────────────────────────────────────────────────────────

    def _fresh(self, balance: float) -> dict:
        return {
            "open": [],
            "closed": [],
            "orders": [],
            "shadows": [],
            "shadow_results": [],
            "balance": balance,
            "initial_balance": balance,
            "equity_history": [{"ts": _now(), "equity": balance}],
        }

    def _rows(self, sql: str, params=(), con=None) -> list:
        return (con or self._con).execute(sql, params).fetchall()

    def _snapshot(self) -> dict:
        """The last saved state, read without the write lock — for everything that only reads."""
        with self._read_lock:
            self._rcon.execute("BEGIN")               # one consistent snapshot across the SELECTs
            try:
                return self._load(self._rcon)
            finally:
                self._rcon.execute("COMMIT")

    def _load(self, con=None) -> dict:
        w = self._rows("SELECT balance, initial_balance FROM wallets WHERE label=?", (self.label,), con)
        if not w:
            return self._fresh(self.initial_balance)
        L = self.label
        state = {
            "balance": w[0][0],
            "initial_balance": w[0][1],
            "open": [_normalize(json.loads(r[0])) for r in self._rows(
                "SELECT data FROM trades WHERE label=? AND status='open' ORDER BY opened_at", (L,), con)],
            "closed": [json.loads(r[0]) for r in self._rows(
                "SELECT data FROM trades WHERE label=? AND status='closed' ORDER BY closed_at, id", (L,), con)],
            "orders": [json.loads(r[0]) for r in self._rows(
                "SELECT data FROM orders WHERE label=? AND status='pending' ORDER BY placed_at", (L,), con)],
            "order_history": list(reversed([json.loads(r[0]) for r in self._rows(
                "SELECT data FROM orders WHERE label=? AND status!='pending' ORDER BY placed_at DESC LIMIT 100", (L,), con)])),
            "shadows": [json.loads(r[0]) for r in self._rows("SELECT data FROM shadows WHERE label=?", (L,), con)],
            "shadow_results": [json.loads(r[0]) for r in self._rows(
                "SELECT data FROM shadow_results WHERE label=? ORDER BY closed_ts", (L,), con)],
            "equity_history": list(reversed([{"ts": r[0], "equity": r[1]} for r in self._rows(
                "SELECT ts, equity FROM equity WHERE label=? ORDER BY ts DESC LIMIT ?", (L, self.history_cap), con)])),
        }
        return state

    def _save(self, state: dict, full: bool = False) -> None:
        """Upsert what can have changed since the last save (everything, when `full`).

        Closed trades, order history, shadow results and equity points are append-mostly,
        so only their tails are rewritten; open trades, pending orders and open shadows
        are rewritten in full (they're small).
        """
        L, con = self.label, self._con
        closed = state["closed"] if full else state["closed"][-50:]
        history = state.get("order_history", []) if full else state.get("order_history", [])[-100:]
        results = state.get("shadow_results", []) if full else state.get("shadow_results", [])[-500:]
        equity = state.get("equity_history", []) if full else state.get("equity_history", [])[-50:]
        with con:
            con.execute("INSERT OR REPLACE INTO wallets VALUES (?,?,?,?)",
                        (L, state["balance"], state["initial_balance"], time.time()))
            con.executemany(
                "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?)",
                [(L, t["id"], t.get("status") or ("closed" if t.get("closed_at") else "open"), t.get("symbol"),
                  t.get("opened_at"), t.get("closed_at"), t.get("pnl_usd"), json.dumps(t, default=str))
                 for t in state["open"] + closed])
            con.executemany(
                "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?)",
                [(L, o["id"], o.get("status", "pending"), o.get("placed_at"), json.dumps(o, default=str))
                 for o in state.get("orders", []) + history])
            con.execute("DELETE FROM shadows WHERE label=?", (L,))
            con.executemany("INSERT INTO shadows VALUES (?,?,?)",
                            [(L, sh["id"], json.dumps(sh, default=str)) for sh in state.get("shadows", [])])
            con.executemany("INSERT OR IGNORE INTO shadow_results VALUES (?,?,?,?,?)",
                            [(L, r["trade_id"], r["policy"], r.get("closed_ts"), json.dumps(r, default=str))
                             for r in results])
            con.executemany("INSERT OR IGNORE INTO equity VALUES (?,?,?)",
                            [(L, e["ts"], e["equity"]) for e in equity])

    def _import_legacy_json(self, legacy_json: str | None) -> None:
        """One-time import of the wallet's old JSON file; the file is kept as *.migrated.json."""
        if not legacy_json or not os.path.exists(legacy_json):
            return
        if self._rows("SELECT 1 FROM wallets WHERE label=?", (self.label,)):
            return
        with open(legacy_json) as f:
            state = json.load(f)
        for key in ("orders", "order_history", "shadows", "shadow_results", "equity_history"):
            state.setdefault(key, [])
        for t in state["open"]:
            t["status"] = "open"
        for t in state["closed"]:
            t["status"] = "closed"
        with self._lock:
            self._save(state, full=True)
        os.replace(legacy_json, legacy_json[:-5] + ".migrated.json")

    def _snapshot_equity(self, state: dict, force: bool = False) -> None:
        """Append current equity to history, keeping at most `history_cap` entries."""
        history = state.setdefault("equity_history", [])
        if not force and self.min_snapshot_seconds and history:
            try:
                last = datetime.fromisoformat(history[-1]["ts"]).timestamp()
                if time.time() - last < self.min_snapshot_seconds:
                    return
            except (KeyError, ValueError):
                pass
        history.append({"ts": _now(), "equity": round(self._equity(state), 2)})
        if len(history) > self.history_cap:
            state["equity_history"] = history[-self.history_cap:]

    @staticmethod
    def _equity(state: dict) -> float:
        return state["balance"] + sum(t["pnl_usd"] for t in state["open"])

    @staticmethod
    def _margin_used(state: dict) -> float:
        return (sum(t.get("margin_usd", 0) for t in state["open"])
                + sum(o.get("margin_usd", 0) for o in state.get("orders", [])))

    def _free_margin(self, state: dict) -> float:
        return self._equity(state) - self._margin_used(state)

    # ── Trading ──────────────────────────────────────────────────────────────

    def open_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float,
        size_pct: Optional[float] = 2.0,
        timeframe: str = "1h",
        oracle_confidence: int = 50,
        oracle_reasoning: str = "",
        hold_duration: str = "N/A",
        extra: Optional[dict] = None,
        risk_pct: Optional[float] = None,
        leverage: Optional[int] = None,
        notional_usd: Optional[float] = None,
    ) -> dict:
        """Open at market (slippage + taker fee). Size by `risk_pct` (preferred), by `size_pct`
        notional, or by `notional_usd` (the Trade tab). `leverage` overrides the automatic choice
        but is still refused if liquidation would sit inside the stop. `extra` is merged into the
        record; `exit_policy` picks how the position is managed (see trading/exits.py)."""
        with self._lock:
            state = self._load()
            if notional_usd:
                size_pct, risk_pct = notional_usd / state["balance"] * 100 if state["balance"] else 0, None

            # Reject if an open trade already exists for this symbol
            existing = next((t for t in state["open"] if t["symbol"] == symbol), None)
            if existing:
                return {
                    "error": f"Trade already open for {symbol} (id={existing['id']}). Close it before opening a new one.",
                    "existing_trade": existing,
                }
            error = _validate_levels(direction, entry_price, stop_loss)
            if error:
                return {"error": error}

            trade = _new_trade(
                self._equity(state), state["balance"], symbol, direction, entry_price, stop_loss,
                take_profit_1, take_profit_2, size_pct, risk_pct, timeframe, oracle_confidence,
                oracle_reasoning, hold_duration, {"entry_type": "market", **(extra or {})}, maker=False,
                leverage=leverage,
            )
            error = _validate_levels(direction, trade["entry_price"], stop_loss, trade["liquidation_price"])
            if error:
                return {"error": error}
            if trade["quantity"] <= 0:
                return {"error": "position size is zero — the wallet has no balance to size from"}
            if trade["margin_usd"] > self._free_margin(state):
                return {"error": f"insufficient margin: need ${trade['margin_usd']:.2f}, "
                                 f"free ${self._free_margin(state):.2f}"}
            state["open"].append(trade)
            state.setdefault("shadows", []).extend(exits.make_shadows(trade, self.label))
            trade_alerts.opened(self.label, trade)
            self._snapshot_equity(state, force=True)
            self._save(state)
            return trade

    def update_prices(self, price_map: dict[str, float]) -> list[dict]:
        """Apply a live price to every open position. Returns auto-closed trades.

        Kept for callers without minute bars; `process_paths` is the accurate path.
        """
        with self._lock:
            state = self._load()
            closed: list[dict] = []
            now = time.time()
            for trade in list(state["open"]):
                price = price_map.get(trade["symbol"])
                if not price:
                    continue
                _refresh(trade, price)
                exits.adopt_lock(trade, self.label)
                hit = _evaluate_bar(trade, price, price, price, price, now)
                if hit:
                    closed.append(self._close_open(state, trade, hit[1], hit[0]))
            if price_map:
                self._snapshot_equity(state, force=bool(closed))
            self._save(state)
            return closed

    def close_trade_manually(
        self, trade_id: str, current_price: float, reason: str = "manual"
    ) -> Optional[dict]:
        with self._lock:
            state = self._load()
            trade = next((t for t in state["open"] if t["id"] == trade_id), None)
            if not trade:
                return None
            trade = self._close_open(state, trade, current_price, reason)
            self._snapshot_equity(state, force=True)
            self._save(state)
            return trade

    # ── Shadow book ──────────────────────────────────────────────────────────
    #
    # Each trade spawns one virtual copy per exit policy at entry. Shadows see the same
    # bars, costs and funding as the real trade but ignore everything else (position
    # manager, flips, manual closes) — so their results isolate the exit policy alone.

    def _advance_shadows(self, state: dict, symbol: str, bars: list[dict], live: float | None, now: float) -> None:
        still = []
        for sh in state.get("shadows", []):
            if sh["symbol"] != symbol:
                still.append(sh)
                continue
            hit = None
            for bar in bars:
                t0 = bar["open_time"]
                if t0 < sh["checked_from_ms"]:
                    continue
                hit = _evaluate_bar(sh, bar["open"], bar["high"], bar["low"], bar["close"],
                                    (t0 + MINUTE_MS) / 1000, stop_only=t0 == sh.get("fill_bar_ms"))
                sh["checked_from_ms"] = t0 + MINUTE_MS
                if hit:
                    break
            if not hit and live:
                hit = _evaluate_bar(sh, live, live, live, live, now)
            if hit:
                self._record_shadow(state, _close_trade(sh, hit[1], hit[0]))
            else:
                if live:
                    _refresh(sh, live)
                still.append(sh)
        state["shadows"] = still

    @staticmethod
    def _record_shadow(state: dict, sh: dict) -> None:
        closed_ts = time.time()
        results = state.setdefault("shadow_results", [])
        results.append({
            "trade_id": sh["trade_id"], "policy": sh["exit_policy"], "symbol": sh["symbol"],
            "direction": sh["direction"], "wallet": sh.get("wallet"),
            "r": round(sh["pnl_usd"] / sh["risk_usd"], 3) if sh.get("risk_usd") else 0.0,
            "claude_verdict": sh.get("claude_verdict"), "claude_confidence": sh.get("claude_confidence"),
            "maker_order_id": sh.get("maker_order_id"),
            "pnl_usd": sh["pnl_usd"], "reason": sh["close_reason"],
            "hold_h": round((closed_ts - sh.get("opened_ts", closed_ts)) / 3600, 2),
            "closed_ts": closed_ts,
        })
        state["shadow_results"] = results[-5000:]

    def add_virtual(
        self, symbol: str, direction: str, price: float, stop_loss: float, take_profit_1: float,
        take_profit_2: float, timeframe: str, risk_pct: float, extra: dict,
    ) -> Optional[dict]:
        """Track a trade that was NOT taken (e.g. Claude declined) as a shadow under the
        live exit policy — same fill model and costs as a real one, no margin or balance."""
        if _validate_levels(direction, price, stop_loss):
            return None
        with self._lock:
            state = self._load()
            t = _new_trade(self._equity(state), state["balance"], symbol, direction, price, stop_loss,
                           take_profit_1, take_profit_2, None, risk_pct, timeframe, 0, "", "", extra, maker=False)
            t["opened_ts"] = time.time()
            state.setdefault("shadows", []).extend(
                exits.make_shadows(t, self.label, [t["exit_policy"]]))
            self._save(state)
            return t

    def drop_shadows(self) -> int:
        """Remove every open shadow (books where exit-policy shadows mean nothing). Returns how many."""
        with self._lock:
            state = self._load()
            n = len(state.get("shadows", []))
            if n:
                state["shadows"] = []
                self._save(state)
            return n

    def add_twin(self, trade: dict, verdict: str) -> dict:
        """Keep a closed position running as a shadow from exactly where it was (same entry, stop,
        peak and progress through the bars) — what holding it would have made (see thesis_exit)."""
        with self._lock:
            state = self._load()
            sh = {k: v for k, v in trade.items() if k not in ("current_price", "pnl_usd", "pnl_pct", "roe_pct")}
            sh.update(id=f"{trade['id']}:{verdict}", trade_id=trade["id"], wallet=self.label, claude_verdict=verdict)
            sh.setdefault("realized_pnl_usd", 0.0)
            if sh.get("checked_from_ms") is None:
                sh["checked_from_ms"] = (int(trade["opened_ts"] * 1000) // 60_000 + 1) * 60_000
            state.setdefault("shadows", []).append(sh)
            self._save(state)
            return sh

    def shadow_starts(self) -> dict[str, int]:
        """Earliest unprocessed bar per symbol across open shadows — for the monitor."""
        state = self._snapshot()
        out: dict[str, int] = {}
        for sh in state.get("shadows", []):
            out[sh["symbol"]] = min(out.get(sh["symbol"], 10**15), sh["checked_from_ms"])
        return out

    def shadow_report(self) -> dict:
        state = self._snapshot()
        return {
            "policies": exits.summarize([r for r in state.get("shadow_results", [])
                                         if r.get("claude_verdict") not in exits.NOT_TAKEN]),
            "calibration": exits.claude_calibration(state.get("shadow_results", [])),
            "open_shadows": len(state.get("shadows", [])),
            "results": state.get("shadow_results", []),
        }

    def _close_open(self, state: dict, trade: dict, price: float, reason: str) -> dict:
        state["open"] = [t for t in state["open"] if t["id"] != trade["id"]]
        trade = _close_trade(trade, price, reason)
        state["closed"].append(trade)
        state["balance"] = round(state["balance"] + trade["pnl_usd"], 2)
        trade_alerts.closed(self.label, trade)
        return trade

    # ── Funding ──────────────────────────────────────────────────────────────

    def apply_funding(self, events: dict[str, list[dict]]) -> float:
        """Charge funding settlements that fell inside each open position's life.

        `events[symbol] = [{"time": ms, "rate": r, "mark": price}, ...]`. Positive rate:
        longs pay shorts. Each position remembers `funding_through_ms`, so a settlement is
        charged exactly once. Returns the total charged (negative = received).
        """
        total = 0.0
        with self._lock:
            state = self._load()
            for trade in state["open"] + state.get("shadows", []):
                through = trade.get("funding_through_ms", 0)
                for ev in sorted(events.get(trade["symbol"], []), key=lambda e: e["time"]):
                    if ev["time"] <= through:
                        continue
                    payment = trade["quantity"] * ev["mark"] * ev["rate"] * _sign(trade)
                    trade["funding_usd"] = round(trade.get("funding_usd", 0.0) + payment, 4)
                    trade["funding_through_ms"] = ev["time"]
                    total += payment
                if trade.get("current_price"):
                    _refresh(trade, trade["current_price"])
            if total:
                self._save(state)
        return total

    def funding_windows(self) -> dict[str, int]:
        """Earliest un-charged funding time per open symbol — what to fetch."""
        state = self._snapshot()
        out: dict[str, int] = {}
        for t in state["open"] + state.get("shadows", []):
            out[t["symbol"]] = min(out.get(t["symbol"], 10**15), t.get("funding_through_ms", 0))
        return out

    # ── Limit orders ─────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        direction: str,
        limit_price: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float,
        size_pct: Optional[float],
        expires_at: float,
        trade_fields: Optional[dict] = None,
        extra: Optional[dict] = None,
        risk_pct: Optional[float] = None,
        leverage: Optional[int] = None,
        notional_usd: Optional[float] = None,
    ) -> dict:
        """Rest a limit order; its margin is reserved until it fills or is cancelled.
        `trade_fields` (timeframe, reasoning, extra…) are carried onto the filled trade."""
        with self._lock:
            state = self._load()
            orders = state.setdefault("orders", [])
            if any(t["symbol"] == symbol for t in state["open"]) or any(o["symbol"] == symbol for o in orders):
                return {"error": f"{symbol} already has an open trade or pending order"}
            error = _validate_levels(direction, limit_price, stop_loss)
            if error:
                return {"error": error}

            stop_pct = _stop_pct(limit_price, stop_loss)
            equity = self._equity(state)
            if notional_usd:
                notional = notional_usd
            else:
                notional = (equity * risk_pct / 100 / (stop_pct / 100)) if risk_pct is not None \
                    else state["balance"] * (size_pct or 0) / 100
            lev = leverage or futures.safe_leverage(stop_pct, symbol)
            error = _validate_levels(direction, limit_price, stop_loss,
                                     futures.liquidation_price(limit_price, lev, direction, symbol))
            if error:
                return {"error": error}
            if notional <= 0:
                return {"error": "order size is zero — the wallet has no balance to size from"}
            margin = notional / lev
            if margin > self._free_margin(state):
                return {"error": f"insufficient margin: need ${margin:.2f}, free ${self._free_margin(state):.2f}"}

            now = time.time()
            order = {
                "id": str(uuid.uuid4())[:8],
                "symbol": symbol,
                "direction": direction,
                "limit_price": limit_price,
                "stop_loss": stop_loss,
                "take_profit_1": take_profit_1,
                "take_profit_2": take_profit_2,
                "notional_usd": round(notional, 2),
                "size_pct": round(notional / state["balance"] * 100, 2) if state["balance"] else 0,
                "leverage": lev,
                "margin_usd": round(margin, 2),
                "liquidation_price": round(futures.liquidation_price(limit_price, lev, direction, symbol), 8),
                "placed_at": now,
                "expires_at": expires_at,
                # Only bars that open after placement can fill it — earlier wicks don't count.
                "checked_from_ms": (int(now * 1000) // MINUTE_MS + 1) * MINUTE_MS,
                "status": "pending",
                "current_price": None,
                "trade_fields": trade_fields or {},
                **(extra or {}),
            }
            orders.append(order)
            self._save(state)
            return order

    def _retire_order(self, state: dict, order: dict, status: str, reason: str) -> dict:
        state["orders"] = [o for o in state.get("orders", []) if o["id"] != order["id"]]
        order.update(status=status, close_reason=reason, resolved_at=time.time())
        history = state.setdefault("order_history", [])
        history.append(order)
        state["order_history"] = history[-1000:]            # long enough to measure maker entries
        return order

    def cancel_order(self, order_id: str, reason: str = "manual") -> Optional[dict]:
        with self._lock:
            state = self._load()
            order = next((o for o in state.get("orders", []) if o["id"] == order_id), None)
            if not order:
                return None
            order = self._retire_order(state, order, "cancelled", reason)
            self._save(state)
            return order

    def _fill(self, state: dict, order: dict, price: float, bar_open_ms: int) -> dict:
        f = order.get("trade_fields", {})
        # The order's notional and leverage were fixed at placement; a fill at a better
        # price buys slightly more units for the same notional.
        size_pct = order["notional_usd"] / state["balance"] * 100 if state["balance"] else 0
        trade = _new_trade(
            self._equity(state), state["balance"], order["symbol"], order["direction"], price,
            order["stop_loss"], order["take_profit_1"], order["take_profit_2"], size_pct, None,
            f.get("timeframe", "1h"), f.get("oracle_confidence", 50),
            f.get("oracle_reasoning", ""), f.get("hold_duration", "N/A"),
            {**f.get("extra", {}), "order_id": order["id"], "entry_type": "limit",
             "limit_price": order["limit_price"],
             # The fill bar itself is re-checked for the stop (pessimistic), so start there.
             "checked_from_ms": bar_open_ms, "fill_bar_ms": bar_open_ms},
            maker=True, leverage=order["leverage"],
        )
        state["open"].append(trade)
        state.setdefault("shadows", []).extend(exits.make_shadows(trade, self.label))
        trade_alerts.opened(self.label, trade)
        order["trade_id"] = trade["id"]
        order["fill_price"] = price
        self._retire_order(state, order, "filled", "filled")
        return trade

    def process_paths(self, paths: dict[str, dict], now: Optional[float] = None) -> list[dict]:
        """Walk each symbol's recent 1-minute bars through its pending order and open trade.

        `paths[symbol] = {"bars": [closed 1m klines, oldest first], "price": live price}`.
        Closed bars are applied in time order — a limit can fill and then stop out or hit
        target within the same batch — then the live price covers the still-forming
        minute. Each order/trade remembers `checked_from_ms` so no bar is applied twice.

        Returns events: {"type": "filled"|"order_expired"|"closed", ...}.
        """
        now = now or time.time()
        events: list[dict] = []
        with self._lock:
            state = self._load()
            state.setdefault("orders", [])

            for symbol, path in paths.items():
                bars = sorted(path.get("bars") or [], key=lambda b: b["open_time"])
                live = path.get("price")

                order = next((o for o in state["orders"] if o["symbol"] == symbol), None)
                trade = next((t for t in state["open"] if t["symbol"] == symbol), None)
                if trade:
                    exits.adopt_lock(trade, self.label)

                if trade and "checked_from_ms" not in trade:
                    # Market entries: first full minute after opening.
                    opened = datetime.fromisoformat(trade["opened_at"]).timestamp()
                    trade["checked_from_ms"] = (int(opened * 1000) // MINUTE_MS + 1) * MINUTE_MS

                for bar in bars:
                    t0, t1 = bar["open_time"], bar["open_time"] + MINUTE_MS
                    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

                    if order and t0 >= order["checked_from_ms"]:
                        if t0 / 1000 >= order["expires_at"]:
                            events.append({"type": "order_expired",
                                           "order": self._retire_order(state, order, "cancelled", "expired")})
                            order = None
                        elif _order_touched(order, h, l):
                            trade = self._fill(state, order, _order_fill_price(order, o), t0)
                            events.append({"type": "filled", "trade": dict(trade), "order": order})
                            order = None
                        else:
                            order["checked_from_ms"] = t1

                    if trade and t0 >= trade["checked_from_ms"]:
                        hit = _evaluate_bar(trade, o, h, l, c, t1 / 1000,
                                            stop_only=t0 == trade.get("fill_bar_ms"))
                        trade["checked_from_ms"] = t1
                        if hit:
                            events.append({"type": "closed", "trade": self._close_open(state, trade, hit[1], hit[0])})
                            trade = None

                # The forming minute: live price, same rules.
                if live:
                    if order:
                        order["current_price"] = live
                        if now >= order["expires_at"]:
                            events.append({"type": "order_expired",
                                           "order": self._retire_order(state, order, "cancelled", "expired")})
                            order = None
                        elif _order_touched(order, live, live):
                            trade = self._fill(state, order, _order_fill_price(order, live),
                                               int(now * 1000) // MINUTE_MS * MINUTE_MS)
                            events.append({"type": "filled", "trade": dict(trade), "order": order})
                            order = None
                    if trade:
                        _refresh(trade, live)
                        hit = _evaluate_bar(trade, live, live, live, live, now)
                        if hit:
                            events.append({"type": "closed", "trade": self._close_open(state, trade, hit[1], hit[0])})

                self._advance_shadows(state, symbol, bars, live, now)

            self._snapshot_equity(state, force=any(e["type"] != "order_expired" for e in events))
            self._save(state)
        return events

    def all_trades(self) -> dict:
        """Every trade the wallet has recorded, unabridged — for history and backtests."""
        state = self._snapshot()
        return {"closed": state["closed"], "open": state["open"],
                "initial_balance": state["initial_balance"]}

    # ── Position adjustments (scale in / scale out) ──────────────────────────
    #
    # Partial exits accumulate in `realized_pnl_usd` (gross price P&L) on the trade and
    # settle to the balance only when the trade closes, together with the remainder —
    # so equity is always right and nothing is counted twice. `risk_usd` keeps the risk
    # taken at the original entry, so R-multiples stay comparable across managed and
    # unmanaged trades; `risk_now_usd` tracks the current loss-at-stop.

    @staticmethod
    def _risk_now(trade: dict) -> float:
        per_unit = (trade["entry_price"] - trade["stop_loss"]) * _sign(trade)
        return round(max(per_unit, 0.0) * trade["quantity"] - trade.get("realized_pnl_usd", 0.0), 2)

    def adjust_trade(
        self, trade_id: str, action: str, price: float, quantity: float,
        reason: str, new_stop: Optional[float] = None, kind: Optional[str] = None,
    ) -> Optional[dict]:
        """`action` is "add" (increase at market) or "reduce" (close `quantity` at market).

        Both cross the spread (slippage + taker fee). After the change the position's
        leverage is re-derived so liquidation still sits well beyond the (possibly moved)
        stop — which can mean posting more margin; an add that needs more margin than is
        free is refused.
        """
        with self._lock:
            state = self._load()
            trade = next((t for t in state["open"] if t["id"] == trade_id), None)
            if not trade or quantity <= 0:
                return None
            trade.setdefault("initial_quantity", trade["quantity"])
            trade.setdefault("initial_entry_price", trade["entry_price"])
            s = _sign(trade)

            if action == "add":
                fill = futures.slipped(price, trade["direction"], opening=True)
                total = trade["quantity"] + quantity
                trade["entry_price"] = round((trade["entry_price"] * trade["quantity"] + fill * quantity) / total, 8)
                trade["quantity"] = round(total, 8)
            elif action == "reduce":
                fill = futures.slipped(price, trade["direction"], opening=False)
                quantity = min(quantity, trade["quantity"])
                trade["realized_pnl_usd"] = round(
                    trade.get("realized_pnl_usd", 0.0) + (fill - trade["entry_price"]) * s * quantity, 4)
                trade["quantity"] = round(trade["quantity"] - quantity, 8)
            else:
                raise ValueError(f"unknown adjustment {action!r}")
            trade["fees_usd"] = round(trade.get("fees_usd", 0.0) + futures.fee(fill * quantity, maker=False), 4)

            if new_stop is not None:
                trade.setdefault("original_stop_loss", trade["stop_loss"])
                trade["stop_loss"] = new_stop

            lev = min(trade["leverage"], futures.safe_leverage(_stop_pct(trade["entry_price"], trade["stop_loss"]), trade["symbol"]))
            _set_leverage(trade, lev)
            _refresh(trade, price)
            if action == "add" and self._free_margin(state) < 0:
                return None   # not enough margin for the larger position — nothing was saved
            trade["size_pct"] = round(trade["notional_usd"] / state["balance"] * 100, 2) if state["balance"] else 0
            trade["risk_now_usd"] = self._risk_now(trade)
            trade.setdefault("adjustments", []).append({
                "ts": time.time(), "action": action, "kind": kind, "price": round(fill, 8),
                "quantity": round(quantity, 8), "reason": reason, "new_stop": new_stop,
                "quantity_after": trade["quantity"], "avg_entry_after": trade["entry_price"],
                "leverage_after": trade["leverage"],
            })
            self._snapshot_equity(state, force=True)
            self._save(state)
            return dict(trade)

    def split_close(self, trade_id: str, quantity: float, price: float, reason: str) -> Optional[dict]:
        """Close `quantity` of a position as its own closed row (its share of entry fees, funding and earlier partial
        P&L, plus the exit's costs) and keep the rest open at the same entry — so a position cut to size leaves a
        record of the cut part and a remainder whose P&L reflects only the size it now has. Returns the closed part."""
        with self._lock:
            state = self._load()
            trade = next((t for t in state["open"] if t["id"] == trade_id), None)
            if not trade or quantity <= 0:
                return None
            frac = min(quantity / trade["quantity"], 1.0) if trade["quantity"] else 1.0
            if frac >= 0.999:
                part = self._close_open(state, trade, price, reason)
            else:
                shared = ("fees_usd", "funding_usd", "realized_pnl_usd", "risk_usd", "reward_1_usd")
                part = {**trade, "id": str(uuid.uuid4())[:8], "part_of": trade["id"], "quantity": round(trade["quantity"] * frac, 8),
                        **{k: round(trade.get(k) or 0.0, 4) * frac for k in shared}}
                part.pop("adjustments", None)
                _set_leverage(part, trade["leverage"])
                part = _close_trade(part, price, reason)
                state["closed"].append(part)
                state["balance"] = round(state["balance"] + part["pnl_usd"], 2)
                trade_alerts.closed(self.label, part)
                trade["quantity"] = round(trade["quantity"] * (1 - frac), 8)
                for k in shared:
                    trade[k] = round((trade.get(k) or 0.0) * (1 - frac), 4)
                _set_leverage(trade, trade["leverage"])
                _refresh(trade, price)
                trade["size_pct"] = round(trade["notional_usd"] / state["balance"] * 100, 2) if state["balance"] else 0
                trade["risk_now_usd"] = self._risk_now(trade)
                trade.setdefault("adjustments", []).append({
                    "ts": time.time(), "action": "split", "kind": reason, "price": price, "quantity": part["quantity"],
                    "reason": reason, "part_id": part["id"], "quantity_after": trade["quantity"]})
            self._snapshot_equity(state, force=True)
            self._save(state)
            return dict(part)

    def move_stop(self, trade_id: str, new_stop: float, reason: str, kind: str = "stop_tighten") -> Optional[dict]:
        """Tighten a stop. Refuses to loosen it or to put it through the current price."""
        with self._lock:
            state = self._load()
            trade = next((t for t in state["open"] if t["id"] == trade_id), None)
            if not trade:
                return None
            s = _sign(trade)
            if (new_stop - trade["stop_loss"]) * s <= 0 or (trade["current_price"] - new_stop) * s <= 0:
                return None
            trade.setdefault("original_stop_loss", trade["stop_loss"])
            trade["stop_loss"] = round(new_stop, 8)
            trade["stop_moved"] = True
            trade["risk_now_usd"] = self._risk_now(trade)
            trade.setdefault("adjustments", []).append({
                "ts": time.time(), "action": "stop", "kind": kind, "price": trade["current_price"],
                "quantity": 0, "reason": reason, "new_stop": trade["stop_loss"],
                "quantity_after": trade["quantity"], "avg_entry_after": trade["entry_price"],
            })
            self._save(state)
            return dict(trade)

    def update_levels(self, trade_id: str, stop_loss: Optional[float] = None,
                      take_profit: Optional[float] = None) -> dict:
        """Set a new stop and/or target by hand (Trade tab). Unlike move_stop this may loosen
        the stop, but never past the current price or the liquidation price."""
        with self._lock:
            state = self._load()
            trade = next((t for t in state["open"] if t["id"] == trade_id), None)
            if not trade:
                return {"error": "position not found"}
            s, price = _sign(trade), trade["current_price"]
            if stop_loss is not None:
                if (price - stop_loss) * s <= 0:
                    return {"error": f"a {trade['direction']} stop must be {'below' if s > 0 else 'above'} the price ({price:g})"}
                error = _validate_levels(trade["direction"], trade["entry_price"] if (trade["entry_price"] - stop_loss) * s > 0 else price,
                                         stop_loss, trade.get("liquidation_price"))
                if error:
                    return {"error": error}
            if take_profit is not None and (take_profit - price) * s <= 0:
                return {"error": f"a {trade['direction']} target must be {'above' if s > 0 else 'below'} the price ({price:g})"}
            if stop_loss is not None:
                trade.setdefault("original_stop_loss", trade["stop_loss"])
                trade["stop_loss"] = round(stop_loss, 8)
                trade["stop_moved"] = True
            if take_profit is not None:
                trade["take_profit_1"] = trade["take_profit_2"] = round(take_profit, 8)
                trade["tp_set"] = True
            trade["risk_now_usd"] = self._risk_now(trade)
            trade.setdefault("adjustments", []).append({
                "ts": time.time(), "action": "levels", "kind": "manual", "price": price, "quantity": 0,
                "reason": "set by hand", "new_stop": stop_loss, "new_target": take_profit,
                "quantity_after": trade["quantity"], "avg_entry_after": trade["entry_price"],
            })
            self._save(state)
            return dict(trade)

    def annotate(self, trade_id: str, **fields) -> None:
        """Attach bookkeeping (e.g. the last position review) to an open trade."""
        with self._lock:
            state = self._load()
            trade = next((t for t in state["open"] if t["id"] == trade_id), None)
            if trade:
                trade.update(fields)
                self._save(state)

    def get_orders(self, history_limit: int = 20) -> dict:
        state = self._snapshot()
        return {
            "pending": state.get("orders", []),
            "history": list(reversed(state.get("order_history", [])[-history_limit:])),
        }

    # ── Reporting ────────────────────────────────────────────────────────────

    def get_portfolio(self, closed_limit: int = 20, history_limit: int = 100) -> dict:
        state = self._snapshot()

        open_pnl = sum(t["pnl_usd"] for t in state["open"])
        closed_pnl = sum(t["pnl_usd"] for t in state["closed"])

        wins = [t for t in state["closed"] if t["pnl_usd"] > 0]
        losses = [t for t in state["closed"] if t["pnl_usd"] <= 0]
        win_rate = len(wins) / len(state["closed"]) * 100 if state["closed"] else 0
        avg_win = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0
        profit_factor = abs(avg_win / avg_loss) if avg_loss else float("inf")

        total_equity = state["balance"] + open_pnl
        # A live ledger starts at 0 until its first balance sync; a return on nothing is nothing.
        total_return_pct = (total_equity - state["initial_balance"]) / state["initial_balance"] * 100 if state["initial_balance"] else 0.0
        margin_used = self._margin_used(state)

        return {
            "balance": round(state["balance"], 2),
            "initial_balance": state["initial_balance"],
            "total_equity": round(total_equity, 2),
            "open_pnl": round(open_pnl, 2),
            "realized_pnl": round(closed_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "margin_used": round(margin_used, 2),
            "free_margin": round(total_equity - margin_used, 2),
            "notional_open": round(sum(t.get("notional_usd", 0) for t in state["open"]), 2),
            "open_risk_usd": round(sum(max(self._risk_now(t), 0) for t in state["open"]), 2),
            "fees_paid": round(sum(t.get("fees_usd", 0) for t in state["closed"] + state["open"]), 2),
            "funding_paid": round(sum(t.get("funding_usd", 0) for t in state["closed"] + state["open"]), 2),
            "open_trades": state["open"],
            "closed_trades": state["closed"][-closed_limit:],
            "equity_history": state.get("equity_history", [])[-history_limit:],
            "futures": futures.describe(),
            "stats": {
                "total_trades": len(state["closed"]),
                "open_trades": len(state["open"]),
                "win_rate_pct": round(win_rate, 1),
                "avg_win_usd": round(avg_win, 2),
                "avg_loss_usd": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999,
                "best_trade": max((t["pnl_usd"] for t in state["closed"]), default=0),
                "worst_trade": min((t["pnl_usd"] for t in state["closed"]), default=0),
                "liquidations": sum(1 for t in state["closed"] if t.get("close_reason") == "liquidated"),
            },
        }

    def deposit(self, amount: float, note: str = "deposit") -> float:
        """Add capital: balance and starting balance both rise, so return % keeps meaning. Returns the balance."""
        with self._lock:
            state = self._load()
            state["balance"] += amount
            state["initial_balance"] += amount
            self._snapshot_equity(state, force=True)
            self._save(state)
            logger.info("%s: deposit $%.2f (%s) → balance %.2f", self.label, amount, note, state["balance"])
            return state["balance"]

    def withdraw(self, amount: float, note: str = "withdrawal") -> float:
        """Take capital out: at most the free cash (margin stays with the open positions). Balance and
        starting balance fall together. Returns what was actually taken."""
        with self._lock:
            state = self._load()
            # Balance still carries the margin of open positions; only what is above it is free to leave.
            free = self._equity(state) - self._margin_used(state)
            take = max(0.0, min(float(amount), float(free)))
            if take <= 0:
                return 0.0
            state["balance"] -= take
            state["initial_balance"] -= take
            self._snapshot_equity(state, force=True)
            self._save(state)
            logger.info("%s: withdrawal $%.2f (%s) → balance %.2f", self.label, take, note, state["balance"])
            return take

    def _erase_adjust(self, state: dict, pnl: float) -> None:
        """Undo erased trades' effect on the books: on paper the P&L simply never happened."""
        state["balance"] = round(state["balance"] - pnl, 2)

    def erase_closed_trades(self, ids: set[str], note: str = "erased") -> dict:
        """Remove closed trades from the record and undo their P&L — for ledger artefacts only (positions that
        duplicated another book's, a mis-sized trim), never for trades the book actually took. Their shadow
        results go too. Returns how many were removed and the P&L reversed."""
        with self._lock:
            state = self._load()
            gone = [t for t in state["closed"] if t["id"] in ids]
            if not gone:
                return {"removed": 0, "pnl": 0.0}
            state["closed"] = [t for t in state["closed"] if t["id"] not in ids]
            state["shadow_results"] = [r for r in state.get("shadow_results", []) if r.get("trade_id") not in ids]
            pnl = round(sum(t.get("pnl_usd") or 0.0 for t in gone), 4)
            self._erase_adjust(state, pnl)
            with self._con:
                self._con.executemany("DELETE FROM trades WHERE label=? AND id=?", [(self.label, t["id"]) for t in gone])
                self._con.executemany("DELETE FROM shadow_results WHERE label=? AND trade_id=?", [(self.label, t["id"]) for t in gone])
            self._snapshot_equity(state, force=True)
            self._save(state)
            logger.warning("%s: erased %d closed trades (%s), P&L %+.2f reversed", self.label, len(gone), note, pnl)
            return {"removed": len(gone), "pnl": pnl, "ids": [t["id"] for t in gone]}

    _SCALED = ("quantity", "initial_quantity", "notional_usd", "position_value_usd", "margin_usd", "fees_usd", "funding_usd",
               "realized_pnl_usd", "pnl_usd", "risk_usd", "reward_1_usd", "risk_now_usd", "size_pct")

    def rescale_closed_trades(self, factors: dict[str, float], note: str = "rescaled") -> dict:
        """Restate closed trades as if they had been `factor` × their size (a position opened at the wrong size):
        quantity, notional, margin, costs and P&L scale together; % moves are unchanged. The P&L difference is
        taken out of the books the same way an erase is. Returns how many changed and the P&L shift."""
        with self._lock:
            state = self._load()
            shift, done = 0.0, []
            for t in state["closed"]:
                f = factors.get(t["id"])
                if f is None or f <= 0 or abs(f - 1) < 1e-9:
                    continue
                before = t.get("pnl_usd") or 0.0
                for k in self._SCALED:
                    if isinstance(t.get(k), (int, float)):
                        t[k] = round(t[k] * f, 8 if "quantity" in k else 4 if k != "pnl_usd" else 2)
                t.setdefault("rescaled", []).append({"ts": time.time(), "factor": round(f, 6), "pnl_before": before, "note": note})
                shift += before - t["pnl_usd"]
                done.append(t["id"])
            if not done:
                return {"rescaled": 0, "pnl_shift": 0.0}
            self._erase_adjust(state, round(shift, 4))
            self._snapshot_equity(state, force=True)
            self._save(state, full=True)
            logger.warning("%s: rescaled %d closed trades (%s), P&L %+.2f", self.label, len(done), note, -shift)
            return {"rescaled": len(done), "pnl_shift": round(-shift, 2), "ids": done}

    def reset_portfolio(self, starting_balance: float = 10000.0) -> dict:
        with self._lock:
            with self._con:
                for table in ("trades", "orders", "shadows", "shadow_results", "equity", "wallets"):
                    self._con.execute(f"DELETE FROM {table} WHERE label=?", (self.label,))
            self._save(self._fresh(starting_balance))
        return {"message": "Portfolio reset", "balance": starting_balance}

    def export_trades_csv(self) -> str:
        """Return all trades (open + closed) as a CSV string, newest first."""
        state = self._snapshot()

        all_trades = state["closed"] + state["open"]
        if not all_trades:
            return "No trades recorded yet.\n"

        fieldnames = [
            "id", "symbol", "direction", "status",
            "entry_price", "exit_price",
            "stop_loss", "take_profit_1", "take_profit_2",
            "quantity", "leverage", "notional_usd", "margin_usd", "liquidation_price", "size_pct",
            "pnl_usd", "pnl_pct", "roe_pct", "fees_usd", "funding_usd", "risk_reward",
            "oracle_confidence", "timeframe", "hold_duration",
            "opened_at", "closed_at", "duration_minutes",
            "close_reason", "oracle_reasoning",
        ]

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for trade in sorted(all_trades, key=lambda t: t.get("opened_at", ""), reverse=True):
            row = dict(trade)
            row["exit_price"] = trade.get("current_price", "") if trade.get("closed_at") else ""

            opened_str = trade.get("opened_at", "")
            closed_str = trade.get("closed_at")
            if opened_str:
                try:
                    opened_dt = datetime.fromisoformat(opened_str)
                    end_dt = datetime.fromisoformat(closed_str) if closed_str else datetime.now(timezone.utc)
                    if opened_dt.tzinfo is None:
                        opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    row["duration_minutes"] = round((end_dt - opened_dt).total_seconds() / 60, 1)
                except Exception:
                    row["duration_minutes"] = ""
            else:
                row["duration_minutes"] = ""

            writer.writerow(row)

        return output.getvalue()


# ── Default (main) wallet — the user's own trading ──────────────────────────────

main_wallet = PaperWallet(SWING_DB, label="main", legacy_json=TRADES_FILE)

open_trade = main_wallet.open_trade
update_prices = main_wallet.update_prices
close_trade_manually = main_wallet.close_trade_manually
get_portfolio = main_wallet.get_portfolio
reset_portfolio = main_wallet.reset_portfolio
export_trades_csv = main_wallet.export_trades_csv
