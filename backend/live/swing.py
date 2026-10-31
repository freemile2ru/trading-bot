"""Live ledgers for the swing systems — main wallet and autotrader on real Binance orders.

A LiveWallet is a PaperWallet whose actions go to the exchange first and whose book is then
written from what the exchange reports. It lives in swing_trading.db under its own label
("main_live" / "auto_live"), so live and paper trades are never mixed and every existing view
(history, backtests, shadows, Trades page) works on live trades unchanged.

Rules that differ from paper:
  * Every position gets a real close-position STOP_MARKET on Binance the moment it opens
    (TAKE_PROFIT_MARKET too if its exit policy uses a fixed target). If the stop can't be
    placed the position is closed at once — a live position is never unprotected.
  * The exit policy still runs on the minute bars, but only to MOVE the exchange stop
    (breakeven, trailing, tightening) and for time exits (stale / max hold), which the app
    closes at market. Stop and target hits happen on the exchange; the reconciler reads them
    back (real fill price, commission, funding) and closes the ledger trade.
  * Partial take-profits aren't used live: a partial_trail policy runs as `trail`.
  * The ledger balance mirrors the Binance wallet balance, so risk-based sizing uses real equity.

Switching a system between paper and live only changes where NEW entries go; open positions
finish in the ledger they started in. The paper wallet and the live ledger are both monitored.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from live import control
from live import config as LC
from live.binance import BinanceError, client_id, summarize_fills
from storage.db import SWING_DB
from trading import exits
from trading.paper_trader import PaperWallet, _new_trade, _refresh, _set_leverage, _sign, _validate_levels, main_wallet

logger = logging.getLogger("live.swing")

EXCHANGE_EXITS = {"stop_loss", "tightened_stop", "breakeven", "trailing_stop", "take_profit_2", "liquidated"}


def _side(direction: str, opening: bool) -> str:
    return "BUY" if (direction == "long") == opening else "SELL"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveWallet(PaperWallet):
    def __init__(self, system: str, path: str = SWING_DB):
        super().__init__(path, label=f"{system}_live")
        self.system = system
        # Symbols this ledger is active on, refreshed on every save. Other systems read it
        # WITHOUT taking this wallet's lock: two live wallets checking each other while each
        # held its own lock deadlocked the whole app.
        state = self._load()
        self._held: frozenset[str] = frozenset(
            [t["symbol"] for t in state["open"]] + [o["symbol"] for o in state.get("orders", [])])
        self._pnl_today = self._day_pnl(state)

    def _save(self, state: dict, full: bool = False) -> None:
        super()._save(state, full)
        self._held = frozenset([t["symbol"] for t in state["open"]] + [o["symbol"] for o in state.get("orders", [])])
        self._pnl_today = self._day_pnl(state)

    @staticmethod
    def _day_pnl(state: dict) -> float:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        closed = sum(t["pnl_usd"] for t in state["closed"]
                     if t.get("closed_at") and datetime.fromisoformat(t["closed_at"]).timestamp() >= start)
        return closed + sum(t["pnl_usd"] for t in state["open"])

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _c(self):
        return control.client(self.system)

    @staticmethod
    def _fills(c, symbol: str, start_ms: int, order_id=None, side: str | None = None) -> dict:
        """Fills for an order — never raises: a failed lookup AFTER an order went through must not
        abort the booking (that left a position open on Binance, unbooked and without a stop)."""
        try:
            rows = c.fills(symbol, start_ms)
        except BinanceError as e:
            logger.warning("fills lookup for %s failed (%s) — using the order response", symbol, e)
            rows = []
        if side:
            rows = [x for x in rows if x["side"] == side]
        return summarize_fills(rows, order_id)

    def holds(self, symbol: str) -> bool:
        """Lock-free on purpose (see _held)."""
        return symbol in self._held

    def _erase_adjust(self, state: dict, pnl: float) -> None:
        """The balance mirrors the exchange, where the erased trades' P&L is real: take it out of the starting
        balance instead, so the return since start no longer carries them."""
        state["initial_balance"] = round(state["initial_balance"] + pnl, 2)

    def _sync_balance(self, state: dict) -> None:
        """Mirror the exchange wallet balance (realized P&L, fees and funding all included)."""
        try:
            eq = self._c().equity()
        except (BinanceError, control.LiveBlocked) as e:
            logger.warning("%s: can't read the Binance balance (%s)", self.label, e)
            return
        if not state.get("synced_once"):
            state["initial_balance"] = eq["wallet"]
            state["synced_once"] = True
        state["balance"] = round(eq["wallet"], 2)
        state["exchange_equity"] = eq

    def _protect(self, trade: dict) -> str | None:
        """Place whatever protection is missing: the exchange stop (and a fixed target).

        Returns None when the position is protected. If the STOP can't be placed the position is
        closed at market and "closed" is returned (trade["emergency_close"] holds the fill); if
        even that fails, the trade is flagged unprotected and "unprotected" is returned —
        process_paths retries the stop every minute. A failed target alone is not fatal."""
        c, sym, d = self._c(), trade["symbol"], trade["direction"]
        if not trade.get("exchange_stop"):
            try:
                stp = c.stop_market(sym, _side(d, opening=False), trade["stop_loss"], cid=client_id("stp"))
            except BinanceError as e:
                logger.error("%s %s: protective stop failed (%s) — closing the position", self.label, sym, e)
                control.event(self.system, "stop_failed_closed", {"symbol": sym, "error": str(e)})
                try:
                    cid = client_id("cls")
                    r = c.market_confirmed(sym, _side(d, opening=False), trade["quantity"], reduce_only=True, cid=cid)
                except BinanceError as e2:
                    logger.critical("%s %s: UNPROTECTED — stop and emergency close both failed (%s)", self.label, sym, e2)
                    control.event(self.system, "unprotected", {"symbol": sym, "error": str(e2)})
                    trade["unprotected"] = True
                    return "unprotected"
                f = self._fills(c, sym, int(time.time() * 1000) - 60_000, r.get("orderId"))
                trade["emergency_close"] = {"price": f["price"] or float(r.get("avgPrice") or trade["current_price"]),
                                            "commission": f["commission"]}
                control.record_order(cid, self.system, sym, "close", _side(d, False), "MARKET", trade["quantity"],
                                     trade["emergency_close"]["price"], "filled", r.get("orderId"), ref=trade["id"])
                return "closed"
            trade["exchange_stop"] = {"cid": stp["cid"], "algo": stp["algo"], "price": trade["stop_loss"]}
            trade.pop("unprotected", None)
            control.record_order(stp["cid"], self.system, sym, "stop", _side(d, False), "STOP_MARKET",
                                 trade["quantity"], trade["stop_loss"], "open", stp.get("orderId") or stp.get("algoId"),
                                 stp["algo"], trade["id"])
        if exits.policy_of(trade).fixed_tp2 and trade.get("tp_set", True) and not trade.get("exchange_tp"):
            try:
                tp = c.stop_market(sym, _side(d, opening=False), trade["take_profit_2"], cid=client_id("tp"),
                                   kind="TAKE_PROFIT_MARKET")
            except BinanceError as e:
                logger.warning("%s %s: target order failed (%s) — the stop still protects it", self.label, sym, e)
                return None
            trade["exchange_tp"] = {"cid": tp["cid"], "algo": tp["algo"], "price": trade["take_profit_2"]}
            control.record_order(tp["cid"], self.system, sym, "take_profit", _side(d, False),
                                 "TAKE_PROFIT_MARKET", trade["quantity"], trade["take_profit_2"], "open",
                                 tp.get("orderId") or tp.get("algoId"), tp["algo"], trade["id"])
        return None

    def _after_protect(self, state: dict, trade: dict, outcome: str | None) -> dict | None:
        """Book an emergency close from _protect; returns the closed trade, if any."""
        if outcome != "closed":
            return None
        ec = trade.pop("emergency_close")
        return self._finish(state, trade, ec["price"], ec["commission"], "stop_failed")

    def _replace_stop(self, trade: dict) -> None:
        old = trade.get("exchange_stop") or {}
        if old.get("price") == trade["stop_loss"]:
            return
        c = self._c()
        if old.get("cid"):
            # Binance allows one close-position stop per side, so the old one must go first.
            c.cancel(trade["symbol"], old["cid"], old.get("algo", False))
            control.record_order(old["cid"], self.system, trade["symbol"], "stop", "", "STOP_MARKET", 0, None,
                                 "replaced", ref=trade["id"])
        trade["exchange_stop"] = None               # whatever happens next, the old stop is gone
        # A stop already past the market (-2021) or any other refusal: _protect closes at market,
        # and if even that fails the trade is flagged and the stop retried next minute. The
        # reconciler books the close once the exchange shows the position gone.
        if self._protect(trade) == "closed":
            trade["pending_exchange_close"] = "stop replace failed"

    def _cancel_protection(self, trade: dict) -> None:
        for key in ("exchange_stop", "exchange_tp"):
            o = trade.get(key)
            if o and o.get("cid"):
                self._c().cancel(trade["symbol"], o["cid"], o.get("algo", False))
                control.record_order(o["cid"], self.system, trade["symbol"], key.split("_")[1], "", "", 0, None,
                                     "cancelled", ref=trade["id"])

    def _finish(self, state: dict, trade: dict, exit_price: float, commission: float, reason: str) -> dict:
        """Close the ledger trade at the real exit price with the real commission."""
        state["open"] = [t for t in state["open"] if t["id"] != trade["id"]]
        trade["fees_usd"] = round(trade.get("fees_usd", 0.0) + commission, 4)
        _refresh(trade, exit_price)
        trade.update(exit_trigger_price=exit_price, closed_at=_now_iso(), status="closed", close_reason=reason)
        state["closed"].append(trade)
        state["balance"] = round(state["balance"] + trade["pnl_usd"], 2)
        # The exchange balance already holds trims booked earlier; mirror it rather than let the
        # ledger count them twice until the next sync (the daily-loss guard reads this balance).
        self._sync_balance(state)
        return trade

    def _others_holding(self, other: str, symbol: str) -> bool:
        w = LIVE.get(other)
        return bool(w and w.holds(symbol))

    # ── Entries ──────────────────────────────────────────────────────────────

    def open_trade(self, symbol, direction, entry_price, stop_loss, take_profit_1, take_profit_2,
                   size_pct=2.0, timeframe="1h", oracle_confidence=50, oracle_reasoning="", hold_duration="N/A",
                   extra=None, risk_pct=None, leverage=None, notional_usd=None) -> dict:
        extra = dict(extra or {})
        # A live position is closed whole, so a partial-exit policy runs as the default instead.
        if exits.POLICIES.get(extra.get("exit_policy") or exits.DEFAULT_POLICY, exits.POLICIES["protect"]).partial_at_tp1:
            extra["exit_policy"] = "protect"
        with self._lock:
            state = self._load()
            if any(t["symbol"] == symbol for t in state["open"]) or any(o["symbol"] == symbol for o in state.get("orders", [])):
                return {"error": f"{symbol} already has a live position or pending order"}
            error = _validate_levels(direction, entry_price, stop_loss)
            if error:
                return {"error": error}
            self._sync_balance(state)
            if notional_usd:
                size_pct, risk_pct = notional_usd / state["balance"] * 100 if state["balance"] else 0, None
            plan = _new_trade(self._equity(state), state["balance"], symbol, direction, entry_price, stop_loss,
                              take_profit_1, take_profit_2, size_pct, risk_pct, timeframe, oracle_confidence,
                              oracle_reasoning, hold_duration, {"entry_type": "market", **extra}, maker=False,
                              leverage=leverage)
            error = _validate_levels(direction, entry_price, stop_loss, plan["liquidation_price"])
            if error:
                return {"error": error}
            try:
                control.guard_entry(self.system, symbol, plan["leverage"], self._others_holding)
                c = self._c()
                qty = c.qty(symbol, plan["quantity"])
                rules = c.rules(symbol)
                if qty < rules["min_qty"] or qty * entry_price < rules["min_notional"]:
                    return {"error": f"size {qty} {symbol} is below Binance's minimum "
                                     f"(${rules['min_notional']:.0f} notional) for this account size"}
                c.prepare(symbol, plan["leverage"])
                cid = client_id("ent")
                r = c.market_confirmed(symbol, _side(direction, True), qty, cid=cid)
            except (control.LiveBlocked, BinanceError) as e:
                control.event(self.system, "entry_refused", {"symbol": symbol, "reason": str(e)})
                return {"error": str(e)}
            fills = self._fills(c, symbol, int(time.time() * 1000) - 60_000, r.get("orderId"))
            fill = fills["price"] or float(r.get("avgPrice") or entry_price)
            filled_qty = fills["qty"] or float(r.get("executedQty") or qty)
            control.record_order(cid, self.system, symbol, "entry", _side(direction, True), "MARKET", filled_qty,
                                 fill, "filled", r.get("orderId"))
            trade = _new_trade(self._equity(state), state["balance"], symbol, direction, fill, stop_loss,
                               take_profit_1, take_profit_2, None, None, timeframe, oracle_confidence,
                               oracle_reasoning, hold_duration,
                               {"entry_type": "market", **extra, "mode": "live", "env": LC.ENV,
                                "account": LC.account_id(self.system), "entry_cid": cid,
                                "entry_order_id": r.get("orderId")},
                               maker=True, leverage=plan["leverage"])
            # _new_trade priced a maker fill at `fill` — overwrite size and fees with the exchange's numbers.
            trade["entry_price"], trade["quantity"] = round(fill, 8), filled_qty
            _set_leverage(trade, plan["leverage"])          # notional, margin, liquidation from the real size
            trade["fees_usd"] = round(fills["commission"], 4)
            trade["risk_usd"] = round(abs(fill - stop_loss) * filled_qty, 2)
            trade["r_unit"] = abs(fill - stop_loss)
            _refresh(trade, fill)
            outcome = self._protect(trade)
            if outcome == "closed":
                self._after_protect(state, trade, outcome)
                self._save(state)
                return {"error": "opened, but the stop could not be placed, so it was closed at market"}
            state["open"].append(trade)
            state.setdefault("shadows", []).extend(exits.make_shadows(trade, self.label))
            self._snapshot_equity(state, force=True)
            self._save(state)
            logger.info("LIVE %s: opened %s %s qty %s @ %s (stop %s)", self.label, direction, symbol,
                        filled_qty, fill, stop_loss)
            if outcome == "unprotected":
                return {**trade, "warning": "opened, but NO STOP could be placed and closing failed — check Binance"}
            return trade

    def place_order(self, symbol, direction, limit_price, stop_loss, take_profit_1, take_profit_2, size_pct,
                    expires_at, trade_fields=None, extra=None, risk_pct=None, leverage=None, notional_usd=None) -> dict:
        with self._lock:                        # size off the real account balance
            state = self._load()
            self._sync_balance(state)
            self._save(state)
        order = super().place_order(symbol, direction, limit_price, stop_loss, take_profit_1, take_profit_2,
                                    size_pct, expires_at, trade_fields, extra, risk_pct, leverage, notional_usd)
        if order.get("error"):
            return order
        with self._lock:
            state = self._load()
            o = next(x for x in state["orders"] if x["id"] == order["id"])
            try:
                control.guard_entry(self.system, symbol, o["leverage"], self._others_holding)
                c = self._c()
                qty = c.qty(symbol, o["notional_usd"] / limit_price, market=False)
                c.prepare(symbol, o["leverage"])
                cid = client_id("lim")
                # Maker entries go in post-only (GTX): Binance rejects them rather than letting them take.
                r = c.limit(symbol, _side(direction, True), qty, limit_price, post_only=bool(o.get("post_only")), cid=cid)
            except (control.LiveBlocked, BinanceError) as e:
                self._retire_order(state, o, "cancelled", f"exchange refused: {e}")
                self._save(state)
                return {"error": str(e)}
            o.update(mode="live", exchange_cid=cid, exchange_qty=qty)
            control.record_order(cid, self.system, symbol, "entry", _side(direction, True), "LIMIT", qty,
                                 limit_price, "open", r.get("orderId"), ref=o["id"])
            self._save(state)
            return dict(o)

    def cancel_order(self, order_id: str, reason: str = "manual") -> Optional[dict]:
        with self._lock:
            state = self._load()
            o = next((x for x in state.get("orders", []) if x["id"] == order_id), None)
            if not o or not o.get("exchange_cid"):
                return super().cancel_order(order_id, reason)
            ev = self._cancel_live_order(state, o, reason)
            if ev is None:
                return None                      # couldn't confirm the cancel — the order stays tracked
            self._snapshot_equity(state, force=True)
            self._save(state)
            # A partly filled order became a (protected) trade instead of disappearing.
            return ev.get("order") or {**o, "status": "filled", "trade": ev.get("trade")}

    # ── Exits and adjustments ────────────────────────────────────────────────

    def _market_close(self, state: dict, trade: dict, reason: str) -> dict | None:
        c = self._c()
        try:
            cid = client_id("cls")
            r = c.market_confirmed(trade["symbol"], _side(trade["direction"], False), trade["quantity"],
                                   reduce_only=True, cid=cid)
        except BinanceError as e:
            logger.error("%s: closing %s failed (%s) — its exchange stop stays in place", self.label, trade["symbol"], e)
            control.event(self.system, "close_failed", {"symbol": trade["symbol"], "error": str(e)})
            return None
        try:
            self._cancel_protection(trade)          # after the close: a close-position stop on a flat book is inert
        except BinanceError as e:
            logger.warning("%s: leftover protection on %s not cancelled (%s)", self.label, trade["symbol"], e)
        f = self._fills(c, trade["symbol"], int(time.time() * 1000) - 60_000, r.get("orderId"))
        price = f["price"] or float(r.get("avgPrice") or trade["current_price"])
        control.record_order(cid, self.system, trade["symbol"], "close", _side(trade["direction"], False), "MARKET",
                             f["qty"] or trade["quantity"], price, "filled", r.get("orderId"), ref=trade["id"])
        return self._finish(state, trade, price, f["commission"], reason)

    def close_trade_manually(self, trade_id: str, current_price: float, reason: str = "manual") -> Optional[dict]:
        with self._lock:
            state = self._load()
            trade = next((t for t in state["open"] if t["id"] == trade_id), None)
            if not trade:
                return None
            closed = self._market_close(state, trade, reason)
            self._snapshot_equity(state, force=True)
            self._save(state)
            return closed

    def move_stop(self, trade_id: str, new_stop: float, reason: str, kind: str = "stop_tighten") -> Optional[dict]:
        moved = super().move_stop(trade_id, new_stop, reason, kind)
        if moved:
            with self._lock:
                state = self._load()
                trade = next((t for t in state["open"] if t["id"] == trade_id), None)
                if trade:
                    self._replace_stop(trade)
                    self._save(state)
                    return dict(trade)
        return moved

    def update_levels(self, trade_id: str, stop_loss: Optional[float] = None,
                      take_profit: Optional[float] = None) -> dict:
        """Set stop/target by hand, then replace the orders on the exchange to match."""
        trade = super().update_levels(trade_id, stop_loss, take_profit)
        if trade.get("error"):
            return trade
        with self._lock:
            state = self._load()
            t = next((x for x in state["open"] if x["id"] == trade_id), None)
            if not t:
                return trade
            try:
                if stop_loss is not None:
                    self._replace_stop(t)
                if take_profit is not None:
                    c, old = self._c(), t.get("exchange_tp")
                    if old and old.get("cid"):
                        c.cancel(t["symbol"], old["cid"], old.get("algo", False))
                    tp = c.stop_market(t["symbol"], _side(t["direction"], False), take_profit, cid=client_id("tp"),
                                       kind="TAKE_PROFIT_MARKET")
                    t["exchange_tp"] = {"cid": tp["cid"], "algo": tp["algo"], "price": take_profit}
                    control.record_order(tp["cid"], self.system, t["symbol"], "take_profit", _side(t["direction"], False),
                                         "TAKE_PROFIT_MARKET", t["quantity"], take_profit, "open",
                                         tp.get("orderId") or tp.get("algoId"), tp["algo"], t["id"])
            except BinanceError as e:
                self._save(state)
                return {"error": f"Binance refused the new level: {e}"}
            self._save(state)
            return dict(t)

    def split_close(self, trade_id: str, quantity: float, price: float, reason: str) -> Optional[dict]:
        """A paper-only bookkeeping split: on the live ledger a partial close must go through adjust_trade (a real
        reduce order), or the record would show a close the exchange never made."""
        raise NotImplementedError("split_close is paper-only; use adjust_trade('reduce') on the live ledger")

    def adjust_trade(self, trade_id, action, price, quantity, reason, new_stop=None, kind=None) -> Optional[dict]:
        """Add or trim at market on the exchange, then book the real fill."""
        with self._lock:
            state = self._load()
            trade = next((t for t in state["open"] if t["id"] == trade_id), None)
            if not trade or quantity <= 0:
                return None
            c = self._c()
            try:
                if action == "add":
                    control.guard_entry(self.system, trade["symbol"], trade["leverage"], self._others_holding)
                q = c.qty(trade["symbol"], min(quantity, trade["quantity"]) if action == "reduce" else quantity)
                if q <= 0:
                    return None
                r = c.market_confirmed(trade["symbol"], _side(trade["direction"], action == "add"), q,
                                       reduce_only=action == "reduce", cid=client_id(action))
            except (control.LiveBlocked, BinanceError) as e:
                control.event(self.system, "adjust_refused", {"symbol": trade["symbol"], "reason": str(e)})
                return None
            f = self._fills(c, trade["symbol"], int(time.time() * 1000) - 60_000, r.get("orderId"))
            fill, q = f["price"] or float(r.get("avgPrice") or price), f["qty"] or float(r.get("executedQty") or q)
            trade.setdefault("initial_quantity", trade["quantity"])
            trade.setdefault("initial_entry_price", trade["entry_price"])
            s = _sign(trade)
            if action == "add":
                total = trade["quantity"] + q
                trade["entry_price"] = round((trade["entry_price"] * trade["quantity"] + fill * q) / total, 8)
                trade["quantity"] = round(total, 8)
            else:
                trade["realized_pnl_usd"] = round(trade.get("realized_pnl_usd", 0.0) + (fill - trade["entry_price"]) * s * q, 4)
                trade["quantity"] = round(trade["quantity"] - q, 8)
            trade["fees_usd"] = round(trade.get("fees_usd", 0.0) + f["commission"], 4)
            if new_stop is not None:
                trade.setdefault("original_stop_loss", trade["stop_loss"])
                trade["stop_loss"] = new_stop
                self._replace_stop(trade)
            _refresh(trade, fill)
            trade["risk_now_usd"] = self._risk_now(trade)
            trade.setdefault("adjustments", []).append({
                "ts": time.time(), "action": action, "kind": kind, "price": round(fill, 8), "quantity": round(q, 8),
                "reason": reason, "new_stop": new_stop, "quantity_after": trade["quantity"],
                "avg_entry_after": trade["entry_price"], "leverage_after": trade["leverage"], "live": True,
            })
            self._snapshot_equity(state, force=True)
            self._save(state)
            return dict(trade)

    # ── Monitoring: manage stops on bars, reconcile with the exchange ────────

    def apply_funding(self, events: dict[str, list[dict]]) -> float:
        return 0.0          # real funding is read from the exchange's income history

    def funding_windows(self) -> dict[str, int]:
        return {}

    def reset_portfolio(self, starting_balance: float = 10000.0) -> dict:
        return {"error": "a live ledger can't be reset — it mirrors the Binance account"}

    def process_paths(self, paths: dict[str, dict], now: Optional[float] = None) -> list[dict]:
        now = now or time.time()
        events: list[dict] = []
        with self._lock:
            state = self._load()
            if not state["open"] and not state.get("orders"):
                for symbol, path in paths.items():      # shadows keep running after the trade closes
                    self._advance_shadows(state, symbol, sorted(path.get("bars") or [], key=lambda b: b["open_time"]),
                                          path.get("price"), now)
                self._save(state)
                return events
            c = self._c()

            for trade in list(state["open"]):
                if not trade.get("notional_usd") and trade.get("quantity"):
                    _set_leverage(trade, trade["leverage"])   # entries booked before notional was recomputed
                path = paths.get(trade["symbol"]) or {}
                bars = sorted(path.get("bars") or [], key=lambda b: b["open_time"])
                stop_before = trade["stop_loss"]
                app_exit = None
                for bar in bars:
                    if bar["open_time"] < trade.get("checked_from_ms", 0):
                        continue
                    hit = exits.apply_bar(trade, bar["open"], bar["high"], bar["low"], bar["close"],
                                          bar["open_time"] / 1000 + 60)
                    trade["checked_from_ms"] = bar["open_time"] + 60_000
                    if hit and hit[0] not in EXCHANGE_EXITS:
                        app_exit = hit[0]
                        break
                    if hit:
                        break                       # the exchange stop/target handles it
                if path.get("price"):
                    _refresh(trade, path["price"])
                if trade["stop_loss"] != stop_before:
                    self._replace_stop(trade)
                elif not trade.get("exchange_stop") and not trade.get("pending_exchange_close"):
                    closed = self._after_protect(state, trade, self._protect(trade))   # self-heal an unprotected position
                    if closed:
                        events.append({"type": "closed", "trade": closed})
                        continue
                if app_exit:
                    closed = self._market_close(state, trade, app_exit)
                    if closed:
                        events.append({"type": "closed", "trade": closed})
                    continue
                closed = self._reconcile_trade(state, trade, c)
                if closed:
                    events.append({"type": "closed", "trade": closed})

            for o in list(state.get("orders", [])):
                ev = self._reconcile_order(state, o, c, now)
                if ev:
                    events.append(ev)

            for symbol, path in paths.items():
                self._advance_shadows(state, symbol, sorted(path.get("bars") or [], key=lambda b: b["open_time"]),
                                      path.get("price"), now)
            self._sync_balance(state)
            self._snapshot_equity(state, force=bool(events))
            self._save(state)
        return events

    def _reconcile_trade(self, state: dict, trade: dict, c) -> dict | None:
        """If the exchange no longer holds the position, book how it ended; also pull funding."""
        sym = trade["symbol"]
        opened_ms = int(datetime.fromisoformat(trade["opened_at"]).timestamp() * 1000)
        try:
            funding = c.income(sym, opened_ms, "FUNDING_FEE")
            trade["funding_usd"] = round(-sum(float(x["income"]) for x in funding), 4)   # income < 0 = paid
            held = any(float(p["positionAmt"]) != 0 for p in c.positions(sym))
        except BinanceError as e:
            logger.warning("%s: reconcile %s failed (%s)", self.label, sym, e)
            return None
        if held:
            return None
        closing = _side(trade["direction"], False)
        last_adj = max((a["ts"] for a in trade.get("adjustments", [])), default=0)
        since_ms = max(opened_ms, int(last_adj * 1000) + 1)
        f = self._fills(c, sym, since_ms, side=closing)
        price = f["price"] or trade["current_price"]
        # Which protective order fired: whichever level the exit filled nearest to (algo-endpoint
        # orders can't be looked up by client id the classic way, so price is the common test).
        stop, tp = trade.get("exchange_stop"), trade.get("exchange_tp")
        if tp and abs(price - tp["price"]) < abs(price - trade["stop_loss"]):
            reason, fired = "take_profit_2", tp
        elif stop:
            reason, fired = exits.stop_reason(trade, trade["stop_loss"]), stop
        else:
            reason, fired = "exchange_exit", None
        if fired:
            control.record_order(fired["cid"], self.system, sym, "stop" if fired is stop else "take_profit",
                                 closing, "", f["qty"], price, "filled", ref=trade["id"])
        self._cancel_protection(trade)
        closed = self._finish(state, trade, price, f["commission"], reason)
        control.event(self.system, "reconciled_close", {"symbol": sym, "reason": reason, "price": price})
        return closed

    def _reconcile_order(self, state: dict, o: dict, c, now: float) -> dict | None:
        if not o.get("exchange_cid"):
            return None
        try:
            st = c.order(o["symbol"], o["exchange_cid"])
            status = st.get("status")
            if status == "PARTIALLY_FILLED" and not o.get("exchange_stop"):
                # Part of the order is a real position already — protect it now, not when it
                # finishes filling (which may be hours away, or never).
                stp = c.stop_market(o["symbol"], _side(o["direction"], False), o["stop_loss"], cid=client_id("stp"))
                o["exchange_stop"] = {"cid": stp["cid"], "algo": stp["algo"], "price": o["stop_loss"]}
                control.record_order(stp["cid"], self.system, o["symbol"], "stop", _side(o["direction"], False),
                                     "STOP_MARKET", float(st.get("executedQty") or 0), o["stop_loss"], "open",
                                     stp.get("orderId") or stp.get("algoId"), stp["algo"], o["id"])
            if status in ("NEW", "PARTIALLY_FILLED") and now >= o["expires_at"]:
                c.cancel(o["symbol"], o["exchange_cid"])
                st = c.order(o["symbol"], o["exchange_cid"])       # the cancel may have raced a fill
        except BinanceError as e:
            logger.warning("%s: order status %s failed (%s)", self.label, o["symbol"], e)
            return None
        return self._settle_order(state, o, st, c, now, "expired")

    def _settle_order(self, state: dict, o: dict, st: dict, c, now: float, cancel_reason: str) -> dict | None:
        """Book a limit order from its exchange status: filled (fully, or partly before it was
        cancelled/expired) → a protected trade; cancelled with nothing filled → retired."""
        status = st.get("status")
        executed = float(st.get("executedQty") or 0)
        if status == "FILLED" or (status in ("CANCELED", "EXPIRED") and executed > 0):
            price = float(st.get("avgPrice") or o["limit_price"])
            trade = self._fill(state, o, price, int(now * 1000) // 60_000 * 60_000)
            trade.update(mode="live", env=LC.ENV, account=LC.account_id(self.system), entry_cid=o["exchange_cid"])
            trade["quantity"] = executed
            _set_leverage(trade, trade["leverage"])
            trade["risk_usd"] = round(abs(price - trade["stop_loss"]) * executed, 2)
            if o.get("exchange_stop"):
                trade["exchange_stop"] = o["exchange_stop"]      # placed while it was partly filled
            f = self._fills(c, o["symbol"], int(o["placed_at"] * 1000), st.get("orderId"))
            trade["fees_usd"] = round(f["commission"], 4)
            _refresh(trade, price)
            control.record_order(o["exchange_cid"], self.system, o["symbol"], "entry", "", "LIMIT", executed, price,
                                 "filled", st.get("orderId"), ref=trade["id"])
            closed = self._after_protect(state, trade, self._protect(trade))
            if closed:
                return {"type": "closed", "trade": closed}
            return {"type": "filled", "trade": dict(trade), "order": o}
        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            return {"type": "order_expired",
                    "order": self._retire_order(state, o, "cancelled", cancel_reason if status == "CANCELED" else status.lower())}
        return None

    def _cancel_live_order(self, state: dict, o: dict, reason: str) -> dict | None:
        """Cancel on the exchange and settle: whatever filled first becomes a protected trade."""
        c = self._c()
        try:
            c.cancel(o["symbol"], o["exchange_cid"])
            control.record_order(o["exchange_cid"], self.system, o["symbol"], "entry", "", "LIMIT", 0, None,
                                 "cancelled", ref=o["id"])
            st = c.order(o["symbol"], o["exchange_cid"])
        except BinanceError as e:
            logger.warning("%s: cancelling %s failed (%s)", self.label, o["symbol"], e)
            return None
        return self._settle_order(state, o, st, c, time.time(), reason)

    # ── Kill switch ──────────────────────────────────────────────────────────

    def flatten(self, reason: str) -> list[str]:
        out = []
        with self._lock:
            state = self._load()
            for o in list(state.get("orders", [])):
                if not o.get("exchange_cid"):
                    self._retire_order(state, o, "cancelled", reason)
                    out.append(f"cancelled {o['symbol']} order")
                    continue
                ev = self._cancel_live_order(state, o, reason)
                out.append(f"FAILED to cancel {o['symbol']} order — check Binance" if ev is None else
                           f"cancelled {o['symbol']} order" if ev["type"] == "order_expired" else
                           f"{o['symbol']} order had filled — closing it")
            for t in list(state["open"]):
                if self._market_close(state, t, "kill_switch"):
                    out.append(f"closed {t['symbol']}")
                else:
                    out.append(f"FAILED to close {t['symbol']} — check Binance")
            self._save(state)
        return out

    def daily_pnl(self) -> float:
        """Today's P&L from the last save — lock-free, because the pre-trade guard calls this while
        this wallet's own lock is held (that re-entry hung the app on the first live swing entry)."""
        return self._pnl_today


LIVE: dict[str, LiveWallet] = {"main": LiveWallet("main"), "auto": LiveWallet("auto")}

for _s, _w in LIVE.items():
    control.register(_s, daily_pnl=_w.daily_pnl, flatten=_w.flatten)


def paper(system: str) -> PaperWallet:
    if system == "main":
        return main_wallet
    from autotrader.engine import auto_wallet     # the paper autotrader wallet lives with the autotrader
    return auto_wallet


def active(system: str) -> PaperWallet:
    """Where a NEW entry for this system goes: its live ledger when live, else the paper wallet."""
    return LIVE[system] if control.is_live(system) else paper(system)


def wallets(system: str) -> list[PaperWallet]:
    """Both ledgers — positions keep being managed wherever they were opened."""
    return [paper(system), LIVE[system]]


def find_trade(system: str, trade_id: str) -> tuple[PaperWallet, dict] | None:
    for w in wallets(system):
        t = next((t for t in w.get_portfolio()["open_trades"] if t["id"] == trade_id), None)
        if t:
            return w, t
    return None
