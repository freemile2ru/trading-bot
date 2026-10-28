"""Short-term engine base — wires a live market feed, 1-second market state, a paper book and a
store together. A subclass supplies the strategy; the only one left is the market recorder
(scalper/recorder.py), which has none — it keeps the feed and the research bars.

  feed events ──► MarketState (features)  ──► every EVAL_INTERVAL: the subclass's _evaluate()
             └──► Book (fills, stops, liquidation, funding) ──► events ──► Store

A 4Hz clock closes 1-second bars, persists them to the research market DB, expires orders,
enforces feed-staleness rules, fills in forward-return labels, and snapshots equity and health.
Open positions survive restarts (persisted and restored).

History: this module ran the order-flow scalper (paper and live on Binance) until 2026-09-29 and
the impulse scalper until 2026-10-06; both were retired with no edge after costs — see
experiments/retired.py and the Retired tab. Their trades stay on disk as records.
"""

import asyncio
import logging
import time
from dataclasses import asdict

from scalper import config as C
from scalper import feed
from scalper.book import Book, Order, Position
from scalper.market import BAR_HISTORY, MarketState, SecondBar
from scalper.store import Store

logger = logging.getLogger("scalper.engine")


class ScalperEngine:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.book = self._make_book()
        self._restore_day()
        self.markets = {s: MarketState(s) for s in C.SYMBOLS}
        self.health: dict[str, feed.FeedHealth] = {}
        self.tasks: list[asyncio.Task] = []
        self.labels: list[tuple] = []           # (due_ts, kind, ref_id, symbol, ref_price, sign, horizon)
        self.last_signal_log: dict[str, float] = {}
        self.persisted_ts: dict[str, int] = {}
        self.eval_ms: list[float] = []
        self.last_decision: dict[str, dict] = {}
        self.started = time.time()
        self._restore_positions()
        if self.research_feed:
            self._restore_bars()

    @property
    def mode(self) -> str:
        return "paper"                  # the live order-flow scalper was retired; engines here are paper only

    def _make_book(self) -> Book:
        book = Book(self.store.get_state("balance", C.INITIAL_BALANCE))
        book.initial_balance = self.store.get_state("initial_balance", C.INITIAL_BALANCE)
        book.paused = bool(self.store.get_state("paused", False))
        return book

    def _feed_urls(self) -> tuple[str, str]:
        return C.WS_PUBLIC, C.WS_MARKET

    @property
    def research_feed(self) -> bool:
        """Whether this engine writes its 1s bars (and liquidations) to the research market DB."""
        return True

    async def start(self) -> None:
        feed_tasks, self.health = feed.start(C.SYMBOLS, self.on_message, *self._feed_urls())
        self.tasks = feed_tasks + [asyncio.create_task(self._clock(), name="scalper-clock")]
        self.store.add_event("engine_start", {"symbols": C.SYMBOLS, "balance": self.book.balance})
        logger.info("Scalper started on %s (balance $%.2f)", ",".join(C.SYMBOLS), self.book.balance)

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self._persist_positions()
        self.store.add_event("engine_stop", {"balance": self.book.balance})
        await asyncio.to_thread(self.store.close)

    def _persist_positions(self) -> None:
        self.store.set_state("positions", [asdict(p) for p in self.book.positions.values()])
        self.store.set_state("balance", self.book.balance)
        self.store.set_state("initial_balance", self.book.initial_balance)

    def _restore_day(self) -> None:
        """Rebuild today's P&L from the trades table so the daily loss limit survives restarts.
        (It lived only in memory, so any restart reset it — and re-armed a halted scalper.)"""
        from datetime import datetime, timezone
        from scalper import config as cfg
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        mode_sql = "mode = 'live'" if self.mode == "live" else "COALESCE(mode, 'paper') = 'paper'"
        rows = self.store.query("trades", "SELECT COALESCE(SUM(net_pnl), 0) AS pnl FROM trades "
                                f"WHERE exit_ts >= ? AND {mode_sql}", (midnight,))
        b = self.book
        b.day_pnl = rows[0]["pnl"] if rows else 0.0
        b.day_start_equity = b.balance - b.day_pnl
        if b.day_pnl <= -b.day_start_equity * cfg.DAILY_LOSS_LIMIT_PCT / 100:
            b.halted_reason = f"daily loss limit ({cfg.DAILY_LOSS_LIMIT_PCT}%) hit — resumes next UTC day"

    def _restore_bars(self) -> None:
        """Reload the last hour of 1s bars from the market DB so a restart doesn't cost
        another warm-up. Only used if the history is recent (the gap is filled flat)."""
        cutoff = int(time.time()) - BAR_HISTORY
        for sym, m in self.markets.items():
            rows = self.store.query("market", "SELECT * FROM bars_1s WHERE symbol=? AND ts>=? ORDER BY ts",
                                    (sym, cutoff))
            if not rows or time.time() - rows[-1]["ts"] > 120:
                continue
            for r in rows:
                vol = (r["buy_vol"] or 0) + (r["sell_vol"] or 0)
                m.bars.append(SecondBar(
                    r["ts"], r["o"], r["h"], r["l"], r["c"],
                    spread_sum=r["spread_bps"] or 0.0, obi_sum=r["obi"] or 0.0,
                    book_n=1 if r["spread_bps"] is not None else 0,
                    buy_vol=r["buy_vol"] or 0.0, sell_vol=r["sell_vol"] or 0.0, trades=r["trades"] or 0,
                    pv=(r["vwap"] or 0.0) * vol, vol=vol))
            self.persisted_ts[sym] = rows[-1]["ts"]
        loaded = {s: m.history_seconds() for s, m in self.markets.items() if m.bars}
        if loaded:
            logger.info("Scalper reloaded 1s history from the market DB: %s", loaded)

    def _restore_positions(self) -> None:
        now = time.time()
        restored_tp_ids = set()
        for d in self.store.get_state("positions", []) or []:
            p = Position(**d)
            p.exit_order_id = None                      # a passive exit doesn't survive a restart
            self.book.positions[p.symbol] = p
            # Re-rest the take-profit under its ORIGINAL id so its database row is the one that
            # gets filled or cancelled later — a new id would leave the old row pending forever.
            tp_id = p.tp_order_id or f"tp-{p.id}"
            tp = Order(tp_id, p.symbol, "sell" if p.side == "long" else "buy", "tp", p.tp, p.qty, now,
                       None, 0.0, 0.0, p.entry_price, None, signal_id=p.signal_id, position_id=p.id)
            self.book.orders[tp.id] = tp
            p.tp_order_id = tp.id
            restored_tp_ids.add(tp.id)
        # Everything else that was resting when the engine stopped is gone — say so in the DB.
        self.store.cancel_stale_orders(keep=restored_tp_ids)
        if self.book.positions:
            logger.info("Scalper restored %d open position(s)", len(self.book.positions))

    def on_message(self, kind: str, symbol: str, d: dict) -> None:
        if symbol == "!FORCEORDER":                   # all-market liquidation stream — research only
            o = d.get("o") or {}
            try:
                qty, avg = float(o["z"] or o["q"]), float(o.get("ap") or o["p"])
                self.store.add_liquidation((int(o["T"]) / 1000, o["s"], o["S"], float(o["p"]), avg, qty, qty * avg))
            except (KeyError, TypeError, ValueError):
                pass
            return
        m = self.markets.get(symbol)
        if m is None:
            return
        now = time.time()
        if kind.startswith("depth"):
            m.on_depth(d.get("b", []), d.get("a", []), d.get("E"), now)
            self.book.on_depth(symbol, m.bids, m.asks)
        elif kind == "aggTrade":
            price, qty, maker = float(d["p"]), float(d["q"]), bool(d["m"])
            m.on_trade(price, qty, maker, d.get("E"), now)
            self._handle(self.book.on_trade(symbol, price, qty, maker, now), now)
        elif kind.startswith("markPrice"):
            m.on_mark(int(d.get("T") or 0), d.get("E"), now)
            self._handle(self.book.on_mark(symbol, float(d["p"]), float(d.get("r") or 0), now), now)

    def _handle(self, events: list[dict], now: float) -> None:
        changed = False
        for ev in events:
            kind = ev["type"]
            if kind in ("entry_filled", "tp_filled", "exit_filled", "order_filled", "order_placed"):
                o: Order = ev["order"]
                self.store.upsert_order(o.record())
                if kind == "entry_filled":
                    sign = 1 if o.side == "buy" else -1
                    for h in C.ADVERSE_HORIZONS:
                        self.labels.append((now + h, "adverse", o.id, o.symbol, o.fill_price, sign, h))
                    changed = True
            elif kind == "position_closed":
                for o in ev.get("cancelled", []):
                    self.store.upsert_order(o.record())
                t = ev["trade"]
                m = self.markets.get(t["symbol"])
                if t.get("spread_exit_bps") is None and m and m.bids and m.asks:
                    t["spread_exit_bps"] = (m.asks[0][0] - m.bids[0][0]) / m.mid * 1e4
                self.store.add_trade(t)
                logger.info("Scalp %s %s closed — %s, net $%.2f (%.1f bps, %.2fR, held %.0fs)",
                            t["symbol"], t["side"], t["exit_reason"], t["net_pnl"], t["net_bps"],
                            t["r"] or 0, t["hold_s"])
                changed = True
            elif kind == "funding":
                self.store.add_event("funding", {k: v for k, v in ev.items() if k != "type"})
            elif kind == "order_cancelled":
                self.store.upsert_order(ev["order"].record())
        if changed:
            self._persist_positions()

    async def _clock(self) -> None:
        last_eval = last_equity = last_health = last_prune = 0.0
        while True:
            await asyncio.sleep(0.25)
            now = time.time()
            try:
                for m in self.markets.values():
                    m.roll(now)
                self._persist_bars()
                self._expire_orders(now)
                self._staleness(now)
                self._labels(now)
                if now - last_eval >= C.EVAL_INTERVAL:
                    last_eval = now
                    t0 = time.perf_counter()
                    await self._evaluate(now)
                    self.eval_ms = (self.eval_ms + [(time.perf_counter() - t0) * 1000])[-240:]
                if now - last_equity >= 10:
                    last_equity = now
                    self._snapshot_equity(now)
                if now - last_health >= 60:
                    last_health = now
                    self.store.add_health(now, self.health_report())
                if now - last_prune >= 3600:
                    last_prune = now
                    self.store.prune_market()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Scalper clock error: %s", e)

    def _persist_bars(self) -> None:
        if not self.research_feed:
            return
        rows = []
        for sym, m in self.markets.items():
            last = self.persisted_ts.get(sym, 0)
            for b in m.bars:
                if b.ts > last:
                    rows.append((sym, b.ts, b.o, b.h, b.l, b.c, b.spread_bps, b.obi, b.buy_vol,
                                 b.sell_vol, b.trades, b.pv / b.vol if b.vol else None))
            if m.bars:
                self.persisted_ts[sym] = m.bars[-1].ts
        self.store.add_bars(rows)

    def _expire_orders(self, now: float) -> None:
        if C.PASSIVE_EXIT_REPEG:
            for sym in list(self.book.positions):
                moved = self.book.repeg_exit(sym, now)
                if moved:
                    for o in moved:
                        self.store.upsert_order(o.record())
        for o in list(self.book.orders.values()):
            if not o.ttl or now - o.placed_ts <= o.ttl:
                continue
            if o.purpose == "entry":
                self._cancel(o, "ttl", now)
            elif o.purpose == "exit":
                # The passive exit didn't fill in time — leave at market.
                self._cancel(o, "passive exit timed out", now)
                pos = self.book.positions.get(o.symbol)
                if pos:
                    pos.exit_order_id = None
                    m = self.markets[o.symbol]
                    price = (m.best_bid if pos.side == "long" else m.best_ask) or m.mid
                    ev = self.book.close_market(o.symbol, f"{o.meta.get('reason', 'exit')} (taker)", now, price)
                    if ev:
                        self._handle([ev], now)

    def _cancel(self, o: Order, reason: str, now: float) -> None:
        c = self.book.cancel(o.id, reason, now)
        if c:
            self.store.upsert_order(c.record())

    def feed_age(self, now: float) -> float:
        """Seconds since the stalest feed last delivered — counted from engine start for a
        feed that hasn't delivered yet. (Counting "never" as infinitely stale used to flatten
        every restored position the instant the engine restarted, before the feed connected.)"""
        if not self.health:
            return now - self.started
        return max(now - (h.last_msg or self.started) for h in self.health.values())

    def _staleness(self, now: float) -> None:
        if not self.book.positions or self.feed_age(now) < C.FLATTEN_STALE_SECONDS:
            return
        # Blind with open risk: flatten at the last known mid, with taker slippage.
        for sym in list(self.book.positions):
            m = self.markets[sym]
            ev = self.book.close_market(sym, "feed_stale", now, m.mid or self.book.positions[sym].entry_price)
            if ev:
                self._handle([ev], now)
        self.store.add_event("flatten_stale_feed", {"age_s": self.feed_age(now)})

    def _labels(self, now: float) -> None:
        due = [x for x in self.labels if x[0] <= now]
        if not due:
            return
        self.labels = [x for x in self.labels if x[0] > now]
        for _, kind, ref_id, sym, ref, sign, h in due:
            mid = self.markets[sym].mid
            if not mid or not ref:
                continue
            val = round((mid - ref) / ref * 1e4 * sign, 3)
            if kind == "signal":
                self.store.label_signal(ref_id, h, val)
            else:
                self.store.label_adverse(ref_id, h, val)

    def _snapshot_equity(self, now: float) -> None:
        b = self.book
        eq = b.equity()
        self.store.add_equity(now, round(b.balance, 2), round(eq, 2), round(eq - b.balance, 2),
                              round(b.margin_used(), 2), len(b.positions), round(b.day_pnl, 2), self.mode)
        if b.positions:
            self._persist_positions()      # keeps MFE/MAE/funding current across restarts

    async def _evaluate(self, now: float) -> None:
        """The strategy: called every EVAL_INTERVAL. Implemented by the subclass."""
        raise NotImplementedError

    def _exit(self, sym: str, reason: str, now: float, f: dict) -> None:
        pos = self.book.positions.get(sym)
        if not pos:
            return
        if C.PASSIVE_EXITS:
            o = self.book.place_passive_exit(sym, reason, now, C.PASSIVE_EXIT_TTL)
            if o:
                self.store.upsert_order(o.record())
                return
        # Taker exit: we sell into the bid (long) or buy the ask (short).
        m = self.markets[sym]
        price = (m.best_bid if pos.side == "long" else m.best_ask) or f["mid"]
        ev = self.book.close_market(sym, reason, now, price, f.get("spread_bps"))
        if ev:
            self._handle([ev], now)

    def health_report(self) -> dict:
        lags = sorted(x for m in self.markets.values() for x in m.lag_ms)
        pct = lambda q: round(lags[int(q * (len(lags) - 1))], 1) if lags else None
        ev = sorted(self.eval_ms)
        return {
            "feeds": {k: h.as_dict() for k, h in self.health.items()},
            "lag_ms_p50": pct(0.5), "lag_ms_p95": pct(0.95),
            "eval_ms_avg": round(sum(ev) / len(ev), 2) if ev else None,
            "eval_ms_max": round(ev[-1], 2) if ev else None,
            "store_queue": self.store._q.qsize(), "store_write_errors": self.store.write_errors,
            "db_bytes": self.store.sizes(), "uptime_s": round(time.time() - self.started),
            "history_s": {s: m.history_seconds() for s, m in self.markets.items()},
            "pending_labels": len(self.labels),
        }

    def live(self) -> dict:
        return {
            **self.book.snapshot(),
            "mode": self.mode,
            "symbols": C.SYMBOLS,
            "decisions": self.last_decision,
            "warmup_s": C.WARMUP_SECONDS,
            "health": self.health_report(),
            "config": {},
        }


def _brief(f: dict, full: bool = False) -> dict:
    keys = ("mid", "spread_bps", "obi", "flow_z", "atr_bps", "vwap_dev_atr", "rv_60s_bps", "trades_per_s")
    if full:
        keys += ("flow_10s", "flow_30s", "ema9", "ema21", "median_spread_bps", "micro_skew_bps")
    return {k: (round(f[k], 5) if isinstance(f.get(k), float) else f.get(k)) for k in keys}
