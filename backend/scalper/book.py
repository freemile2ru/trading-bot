"""The scalper's wallet: post-only orders with a queue model, positions, fees, funding,
liquidation, and the risk limits. In memory for speed; every change is emitted as an
event that the engine persists.

Fill realism (the part paper scalpers usually get wrong):
  * A post-only limit joins the BACK of the queue at its price. It fills only when
    trades at that price consume the size ahead of it, or when price trades through it.
    Cancellations ahead of it (the level shrinking below our place) move it up.
  * Stops and every non-target exit are taker fills with slippage.
  * Fees: maker for entries and take-profits, taker for everything else.
"""

import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from scalper import config as C
from scalper import costs
from trading import futures


def _id() -> str:
    return uuid.uuid4().hex[:10]


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Order:
    id: str
    symbol: str
    side: str                  # buy | sell
    purpose: str               # entry | tp | exit (passive exit at the touch)
    price: float
    qty: float
    placed_ts: float
    ttl: float | None
    queue_ahead: float
    queue_ahead_at_place: float
    mid_at_place: float
    spread_at_place: float | None
    status: str = "pending"
    fill_ts: float | None = None
    fill_price: float | None = None
    time_to_fill: float | None = None
    cancel_reason: str | None = None
    signal_id: int | None = None
    position_id: str | None = None
    meta: dict = field(default_factory=dict)

    def record(self) -> dict:
        d = asdict(self)
        d.pop("meta", None)
        d.pop("queue_ahead", None)
        return d


@dataclass
class Position:
    id: str
    symbol: str
    side: str                  # long | short
    qty: float
    entry_price: float
    entry_ts: float
    leverage: int
    margin: float
    liq_price: float
    tp: float
    sl: float
    tp_bps: float
    sl_bps: float
    fees: float
    risk_usd: float
    signal_id: int | None
    features: dict
    time_to_fill: float | None
    queue_ahead: float | None
    spread_entry_bps: float | None
    next_funding_ms: int | None
    funding: float = 0.0
    mfe_bps: float = 0.0
    mae_bps: float = 0.0
    tp_order_id: str | None = None
    exit_order_id: str | None = None

    @property
    def s(self) -> int:
        return 1 if self.side == "long" else -1

    def move_bps(self, price: float) -> float:
        return (price - self.entry_price) / self.entry_price * 1e4 * self.s

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.s * self.qty


class Book:
    def __init__(self, balance: float = C.INITIAL_BALANCE):
        self.balance = balance
        self.initial_balance = balance
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}          # one per symbol
        self.cooldown_until: dict[str, float] = {}
        self.entries_last_hour: deque = deque()
        self.day = _day(time.time())
        self.day_start_equity = balance
        self.day_pnl = 0.0
        self.halted_reason: str | None = None
        self.paused = False
        self.marks: dict[str, float] = {}
        self.depth: dict[str, tuple[list, list]] = {}

    # ── Accounting ────────────────────────────────────────────────────────────

    def equity(self) -> float:
        return self.balance + sum(p.unrealized(self.marks.get(p.symbol, p.entry_price)) - p.fees - p.funding
                                  for p in self.positions.values())

    def margin_used(self) -> float:
        return (sum(p.margin for p in self.positions.values())
                + sum(o.meta.get("margin", 0) for o in self.orders.values() if o.purpose == "entry"))

    def _roll_day(self, now: float) -> None:
        d = _day(now)
        if d != self.day:
            self.day, self.day_pnl, self.day_start_equity = d, 0.0, self.equity()
            if self.halted_reason and self.halted_reason.startswith("daily loss"):
                self.halted_reason = None

    # ── Risk gate ─────────────────────────────────────────────────────────────

    def can_enter(self, symbol: str, now: float) -> tuple[bool, str]:
        self._roll_day(now)
        if self.paused:
            return False, "paused"
        if self.halted_reason:
            return False, f"halted: {self.halted_reason}"
        if symbol in self.positions or any(o.symbol == symbol for o in self.orders.values()):
            return False, "already active on this symbol"
        if len(self.positions) + sum(1 for o in self.orders.values() if o.purpose == "entry") >= C.MAX_POSITIONS:
            return False, f"max {C.MAX_POSITIONS} positions"
        while self.entries_last_hour and now - self.entries_last_hour[0] > 3600:
            self.entries_last_hour.popleft()
        if len(self.entries_last_hour) >= C.MAX_TRADES_PER_HOUR:
            return False, f"max {C.MAX_TRADES_PER_HOUR} entries/hour"
        if now < self.cooldown_until.get(symbol, 0):
            return False, f"cooldown {self.cooldown_until[symbol] - now:.0f}s"
        return True, "ok"

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_entry(self, symbol: str, side: str, price: float, sl_bps: float, tp_bps: float,
                    level_qty: float, mid: float, spread_bps: float | None, now: float,
                    signal_id: int | None, features: dict) -> Order | str:
        """Post-only entry sized so the stop — INCLUDING fees and slippage — loses RISK_PCT of
        equity. Returns the order or a reason."""
        equity = self.equity()
        stop_frac = sl_bps / 1e4
        cost_frac = costs.expected_stop_cost_frac()   # maker in, taker out at the stop
        notional = equity * C.RISK_PCT / 100 / (stop_frac + cost_frac)
        qty = notional / price
        lev = min(C.MAX_LEVERAGE, futures.safe_leverage(sl_bps / 100, symbol))
        margin = notional / lev
        if margin > equity - self.margin_used():
            return f"insufficient margin (${margin:.2f} needed)"
        o = Order(_id(), symbol, "buy" if side == "long" else "sell", "entry", price, qty, now, C.ENTRY_TTL,
                  level_qty, level_qty, mid, spread_bps, signal_id=signal_id,
                  meta={"side": side, "sl_bps": sl_bps, "tp_bps": tp_bps, "leverage": lev, "margin": margin,
                        "features": features})
        self.orders[o.id] = o
        self.entries_last_hour.append(now)
        return o

    def cancel(self, order_id: str, reason: str, now: float) -> Order | None:
        o = self.orders.pop(order_id, None)
        if o:
            o.status, o.cancel_reason, o.fill_ts = "cancelled", reason, None
            o.meta["resolved_ts"] = now
        return o

    # ── Market events → fills & exits ─────────────────────────────────────────

    def on_depth(self, symbol: str, bids: list, asks: list) -> None:
        """Cancellations ahead of us: our place can't exceed what's resting at the level."""
        self.depth[symbol] = (bids, asks)
        for o in self.orders.values():
            if o.symbol != symbol:
                continue
            levels = bids if o.side == "buy" else asks
            qty = next((q for p, q in levels if p == o.price), None)
            if qty is not None:
                o.queue_ahead = min(o.queue_ahead, qty)

    def on_trade(self, symbol: str, price: float, qty: float, buyer_is_maker: bool, now: float) -> list[dict]:
        events: list[dict] = []
        self.marks[symbol] = price

        # 1. Resting orders (entries and take-profits).
        for o in list(self.orders.values()):
            if o.symbol != symbol:
                continue
            if o.side == "buy":
                through = price < o.price
                at_level = price == o.price and buyer_is_maker      # a seller hit the bid
            else:
                through = price > o.price
                at_level = price == o.price and not buyer_is_maker  # a buyer lifted the offer
            if at_level:
                o.queue_ahead -= qty
            if through or (at_level and o.queue_ahead < 0):
                events += self._fill(o, now)

        # 2. Positions: extremes, liquidation gap, stop.
        p = self.positions.get(symbol)
        if p and p.id not in {e.get("position_id") for e in events if e["type"] == "entry_filled"}:
            mv = p.move_bps(price)
            p.mfe_bps, p.mae_bps = max(p.mfe_bps, mv), min(p.mae_bps, mv)
            if (price <= p.liq_price) if p.side == "long" else (price >= p.liq_price):
                events.append(self._close(p, p.liq_price, "liquidated", now, maker=False, trigger=price))
            elif (price <= p.sl) if p.side == "long" else (price >= p.sl):
                events.append(self._close(p, price, "stop", now, maker=False, trigger=price))
        return events

    def on_mark(self, symbol: str, mark: float, rate: float, now: float) -> list[dict]:
        p = self.positions.get(symbol)
        if not p or not p.next_funding_ms or now * 1000 < p.next_funding_ms:
            return []
        payment = p.qty * mark * rate * p.s          # positive rate: longs pay
        p.funding += payment
        p.next_funding_ms += 8 * 3600 * 1000
        return [{"type": "funding", "symbol": symbol, "position_id": p.id, "amount": payment, "rate": rate}]

    def _fill(self, o: Order, now: float) -> list[dict]:
        self.orders.pop(o.id, None)
        o.status, o.fill_ts, o.fill_price = "filled", now, o.price
        o.time_to_fill = now - o.placed_ts
        if o.purpose == "entry":
            m = o.meta
            side = m["side"]
            s = 1 if side == "long" else -1
            tp = o.price * (1 + s * m["tp_bps"] / 1e4)
            sl = o.price * (1 - s * m["sl_bps"] / 1e4)
            notional = o.qty * o.price
            pos = Position(
                _id(), o.symbol, side, o.qty, o.price, now, m["leverage"], notional / m["leverage"],
                futures.liquidation_price(o.price, m["leverage"], side, o.symbol), tp, sl,
                m["tp_bps"], m["sl_bps"], costs.fee(notional, maker=True),
                # All-in risk: price distance to the stop plus the costs of getting stopped.
                abs(o.price - sl) * o.qty + notional * costs.expected_stop_cost_frac(),
                o.signal_id, m["features"], o.time_to_fill, o.queue_ahead_at_place,
                o.spread_at_place, m.get("next_funding_ms"),
            )
            o.position_id = pos.id
            self.positions[o.symbol] = pos
            # Join the back of whatever already rests at the target price (usually nothing:
            # the target is rarely exactly on a displayed level, so it fills only on a trade through it).
            bids, asks = self.depth.get(o.symbol, ([], []))
            levels = asks if side == "long" else bids
            ahead = next((q for p_, q in levels if p_ == tp), 0.0)
            tp_order = Order(_id(), o.symbol, "sell" if side == "long" else "buy", "tp", tp, o.qty, now,
                             None, ahead, ahead, o.price, None, signal_id=o.signal_id, position_id=pos.id)
            self.orders[tp_order.id] = tp_order
            pos.tp_order_id = tp_order.id
            return [{"type": "entry_filled", "order": o, "position_id": pos.id, "position": pos},
                    {"type": "order_placed", "order": tp_order}]
        pos = self.positions.get(o.symbol)
        if pos and pos.id == o.position_id:
            if o.purpose == "exit":
                return [{"type": "exit_filled", "order": o},
                        self._close(pos, o.price, f"{o.meta.get('reason', 'exit')} (maker)", now,
                                    maker=True, trigger=o.price, tp_order=o)]
            return [{"type": "tp_filled", "order": o},
                    self._close(pos, o.price, "take_profit", now, maker=True, trigger=o.price, tp_order=o)]
        return [{"type": "order_filled", "order": o}]

    def _close(self, p: Position, price: float, reason: str, now: float, maker: bool,
               trigger: float, tp_order: Order | None = None) -> dict:
        self.positions.pop(p.symbol, None)
        cancelled = []
        for oid in (p.tp_order_id, p.exit_order_id):
            if oid and (tp_order is None or tp_order.id != oid):
                o = self.cancel(oid, f"position closed ({reason})", now)
                if o:
                    cancelled.append(o)      # returned so the engine persists the cancellation
        if reason == "liquidated":
            fill = price
            fees_exit = p.qty * p.entry_price * futures.mmr(p.symbol)
        elif maker:
            fill, fees_exit = price, costs.fee(p.qty * price, maker=True)
        else:
            # Market exit: walk the visible book for our size, plus latency.
            fill = costs.taker_exit_price(p.side, p.qty, self.depth.get(p.symbol), price)
            fees_exit = costs.fee(p.qty * fill, maker=False)
        slippage_usd = 0.0 if maker else abs(fill - price) * p.qty
        gross = (fill - p.entry_price) * p.s * p.qty
        fees = p.fees + fees_exit
        net = gross - fees - p.funding
        self.balance += net
        self.day_pnl += net
        loss_limit = self.day_start_equity * C.DAILY_LOSS_LIMIT_PCT / 100
        if self.day_pnl <= -loss_limit and not self.halted_reason:
            self.halted_reason = f"daily loss limit ({C.DAILY_LOSS_LIMIT_PCT}%) hit — resumes next UTC day"
        self.cooldown_until[p.symbol] = now + (C.COOLDOWN_AFTER_WIN if net > 0 else C.COOLDOWN_AFTER_LOSS)
        return {
            "type": "position_closed",
            "cancelled": cancelled,
            "trade": {
                "id": p.id, "symbol": p.symbol, "side": p.side, "entry_ts": p.entry_ts, "exit_ts": now,
                "hold_s": round(now - p.entry_ts, 3), "entry_price": p.entry_price, "exit_price": fill,
                "exit_trigger": trigger, "qty": p.qty, "notional": p.qty * p.entry_price,
                "leverage": p.leverage, "margin": p.margin, "liq_price": p.liq_price, "tp": p.tp, "sl": p.sl,
                "tp_bps": p.tp_bps, "sl_bps": p.sl_bps, "exit_reason": reason, "entry_maker": 1,
                "exit_maker": int(maker), "gross_pnl": gross, "fees": fees, "funding": p.funding,
                "net_pnl": net, "net_bps": net / (p.qty * p.entry_price) * 1e4,
                "risk_usd": p.risk_usd, "r": net / p.risk_usd if p.risk_usd else None,
                "mfe_bps": p.mfe_bps, "mae_bps": p.mae_bps, "spread_entry_bps": p.spread_entry_bps,
                "time_to_fill": p.time_to_fill, "queue_ahead": p.queue_ahead, "signal_id": p.signal_id,
                "features": p.features, "entry_fee": p.fees, "exit_fee": fees_exit,
                "slippage_usd": slippage_usd,
                "cost_bps": (fees + slippage_usd + p.funding) / (p.qty * p.entry_price) * 1e4,
            },
        }

    def place_passive_exit(self, symbol: str, reason: str, now: float, ttl: float) -> Order | None:
        """Rest a post-only exit at the touch (a long sells on the ask, a short buys on the
        bid): maker fee and no spread paid. The stop and target stay live meanwhile; the
        engine falls back to a market exit if this hasn't filled within `ttl`."""
        p = self.positions.get(symbol)
        bids, asks = self.depth.get(symbol, ([], []))
        if not p or p.exit_order_id or not bids or not asks:
            return None
        price, ahead = (asks[0] if p.side == "long" else bids[0])
        o = Order(_id(), symbol, "sell" if p.side == "long" else "buy", "exit", price, p.qty, now, ttl,
                  ahead, ahead, (bids[0][0] + asks[0][0]) / 2, None, signal_id=p.signal_id,
                  position_id=p.id, meta={"reason": reason})
        self.orders[o.id] = o
        p.exit_order_id = o.id
        return o

    def repeg_exit(self, symbol: str, now: float) -> tuple[Order, Order] | None:
        """If price has moved away from a resting passive exit, move it to the new touch.

        The replacement keeps the original deadline, so re-pegging never extends how long
        we wait before falling back to a market exit. Returns (cancelled, replacement).
        """
        p = self.positions.get(symbol)
        o = self.orders.get(p.exit_order_id) if p and p.exit_order_id else None
        bids, asks = self.depth.get(symbol, ([], []))
        if not o or not bids or not asks:
            return None
        touch, ahead = (asks[0] if p.side == "long" else bids[0])
        if touch == o.price:
            return None
        deadline = o.placed_ts + (o.ttl or 0)
        old = self.cancel(o.id, "re-pegged to the new touch", now)
        new = Order(_id(), symbol, o.side, "exit", touch, p.qty, now, max(0.0, deadline - now),
                    ahead, ahead, (bids[0][0] + asks[0][0]) / 2, None, signal_id=p.signal_id,
                    position_id=p.id, meta={**o.meta, "repegs": o.meta.get("repegs", 0) + 1})
        self.orders[new.id] = new
        p.exit_order_id = new.id
        return old, new

    def close_market(self, symbol: str, reason: str, now: float, price: float,
                     spread_exit_bps: float | None = None) -> dict | None:
        p = self.positions.get(symbol)
        if not p:
            return None
        ev = self._close(p, price, reason, now, maker=False, trigger=price)
        ev["trade"]["spread_exit_bps"] = spread_exit_bps
        return ev

    def snapshot(self) -> dict:
        return {
            "balance": round(self.balance, 2), "equity": round(self.equity(), 2),
            "initial_balance": self.initial_balance, "margin_used": round(self.margin_used(), 2),
            "day": self.day, "day_pnl": round(self.day_pnl, 2), "halted": self.halted_reason,
            "paused": self.paused,
            "positions": [
                {**{k: v for k, v in asdict(p).items() if k != "features"},
                 "mark": self.marks.get(p.symbol), "open_bps": round(p.move_bps(self.marks.get(p.symbol, p.entry_price)), 2),
                 "unrealized": round(p.unrealized(self.marks.get(p.symbol, p.entry_price)) - p.fees - p.funding, 2)}
                for p in self.positions.values()
            ],
            "orders": [o.record() | {"queue_ahead": round(o.queue_ahead, 6)} for o in self.orders.values()],
        }
