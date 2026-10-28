"""Impulse scalper — a separate paper experiment that trades only extremely volatile moves.

It runs with its own mainnet feed, its own paper wallet and its own trades database
(impulse_trades.db), and never trades live. Its feed also records the 1-second bars and
liquidations into the shared research market DB. It reuses the
scalper's market state, book accounting (fees, slippage, liquidation, funding) and reporting.

  Event    an extreme move on the coin's 1-second bars — either
             fast: the last 60s moved ≥ IMPULSE_FAST_Z σ of the past hour's volatility AND
                   ≥ IMPULSE_FAST_MIN_BPS, on ≥ IMPULSE_FAST_VOL_MULT × normal traded value, or
             slow: the last 300s moved ≥ IMPULSE_SLOW_MIN_BPS.
           One event per coin per IMPULSE_DEDUPE_S (a burst is one event).
  Entry    at market (a resting maker order in a violent move only fills when price turns
           against it), in the move's direction ("ride") or against it ("fade").
  Exit     a stop sized from the move, trailed from the best price by the same distance so a
           runaway keeps running; a time stop; no fixed target.

Every event is logged with forward returns out to 10 minutes whether or not it was traded,
so riding and fading can be judged from the same record (research/scalper_lab.py --impulse
did the historical version of this study; nothing was positive out of sample at the time).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os

from scalper import config as C
from scalper import costs
from scalper.book import Book, Position, _id
from scalper.engine import ScalperEngine, _brief
from scalper.market import MarketState
from scalper.store import Store
from trading import futures

logger = logging.getLogger("scalper.impulse")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


ENABLED = os.getenv("IMPULSE_ENABLED", "true").lower() != "false"
BALANCE = _f("IMPULSE_BALANCE", 10_000)
MODE = os.getenv("IMPULSE_MODE", "ride").lower()             # ride = with the move · fade = against it
WARMUP_S = int(_f("IMPULSE_WARMUP", 1800))                    # history needed for the volatility baseline
FAST_WINDOW, SLOW_WINDOW = 60, 300
FAST_Z = _f("IMPULSE_FAST_Z", 6.0)
FAST_MIN_BPS = _f("IMPULSE_FAST_MIN_BPS", 50.0)
FAST_VOL_MULT = _f("IMPULSE_FAST_VOL_MULT", 3.0)
SLOW_MIN_BPS = _f("IMPULSE_SLOW_MIN_BPS", 150.0)
DEDUPE_S = _f("IMPULSE_DEDUPE_S", 300.0)
SL_MIN_BPS = _f("IMPULSE_SL_MIN_BPS", 60.0)                   # stop = clamp(move × SL_MOVE_FRAC, min, max)
SL_MAX_BPS = _f("IMPULSE_SL_MAX_BPS", 200.0)
SL_MOVE_FRAC = _f("IMPULSE_SL_MOVE_FRAC", 0.5)
TIME_STOP = _f("IMPULSE_TIME_STOP", 600.0)
RISK_PCT = _f("IMPULSE_RISK_PCT", 0.25)
MAX_POSITIONS = int(_f("IMPULSE_MAX_POSITIONS", 3))
MAX_SPREAD_MULT = _f("IMPULSE_MAX_SPREAD_MULT", 4.0)          # spreads blow out in these moves; cap it
BASELINE_REFRESH_S = 15.0
# Seeded start: until the engine has its own WARMUP_S of 1s history, "normal" comes from the past
# hour of 1-minute candles (one REST call per coin) and detection starts once the longest
# measured window (5 min) is filled. Per-second volatility isn't directly in minute candles:
# σ₁ₛ ≈ SEED_VOL_RATIO × σ₁ₘ / √60, where 0.94 is the median over 488 coin-hours of our
# recorded 1s bars (middle half 0.86–1.01; set IMPULSE_SEED=false for the full warm-up).
SEED = os.getenv("IMPULSE_SEED", "true").lower() != "false"
SEED_VOL_RATIO = _f("IMPULSE_SEED_VOL_RATIO", 0.94)
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
HORIZONS = (5, 15, 30, 60, 120, 300, 600)

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
TRADES_DB = os.getenv("IMPULSE_TRADES_DB", os.path.join(_DATA, "impulse_trades.db"))


# ── Detection ──────────────────────────────────────────────────────────────────

def baseline(m: MarketState) -> dict | None:
    """The past hour's 1s volatility (bps) and traded value per second, excluding the last
    SLOW_WINDOW seconds so the move being measured doesn't inflate its own yardstick."""
    bars = list(m.bars)[:-SLOW_WINDOW]
    if len(bars) < WARMUP_S - SLOW_WINDOW:
        return None
    rets = [math.log(b.c / a.c) * 1e4 for a, b in zip(bars, bars[1:]) if a.c and b.c]
    if len(rets) < 2:
        return None
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
    value = sum(b.pv for b in bars) / len(bars)
    return {"sd1_bps": sd, "value_per_s": value}


def seed_baseline(klines: list[list]) -> dict | None:
    """The same baseline from Binance 1-minute klines ([open_time, o, h, l, c, vol, close_time,
    quote_vol, ...]), dropping the still-open candle and the last 5 minutes like baseline() does."""
    closed = klines[:-1][:-(SLOW_WINDOW // 60)]
    closes = [float(k[4]) for k in closed]
    rets = [math.log(b / a) * 1e4 for a, b in zip(closes, closes[1:]) if a and b]
    if len(rets) < 20:
        return None
    mu = sum(rets) / len(rets)
    sd60 = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
    value = sum(float(k[7]) for k in closed) / (len(closed) * 60)
    return {"sd1_bps": SEED_VOL_RATIO * sd60 / math.sqrt(60), "value_per_s": value, "seeded": True}


def measure(m: MarketState, base: dict | None) -> dict | None:
    """The current fast and slow moves; `hit` says whether either counts as extreme."""
    bars = list(m.bars)
    if base is None or len(bars) <= SLOW_WINDOW or not base["sd1_bps"]:
        return None
    c = bars[-1].c
    out = {"hit": None}
    for w in (FAST_WINDOW, SLOW_WINDOW):
        ref = bars[-1 - w].c
        move = (c / ref - 1) * 1e4 if ref else 0.0
        value = sum(b.pv for b in bars[-w:])
        vol_x = value / (base["value_per_s"] * w) if base["value_per_s"] else 0.0
        out[w] = {"move_bps": move, "z": move / (base["sd1_bps"] * math.sqrt(w)), "vol_mult": vol_x}
    fast, slow = out[FAST_WINDOW], out[SLOW_WINDOW]
    if abs(fast["z"]) >= FAST_Z and abs(fast["move_bps"]) >= FAST_MIN_BPS and fast["vol_mult"] >= FAST_VOL_MULT:
        out["hit"] = FAST_WINDOW
    elif abs(slow["move_bps"]) >= SLOW_MIN_BPS:
        out["hit"] = SLOW_WINDOW
    return out


def stop_bps(move_bps: float) -> float:
    return min(SL_MAX_BPS, max(SL_MIN_BPS, abs(move_bps) * SL_MOVE_FRAC))


# ── Book: market entries and a trailing stop ───────────────────────────────────

class ImpulseBook(Book):
    def can_enter(self, symbol: str, now: float) -> tuple[bool, str]:
        ok, why = super().can_enter(symbol, now)
        if ok and len(self.positions) >= MAX_POSITIONS:
            return False, f"max {MAX_POSITIONS} positions"
        return ok, why

    def open_market(self, symbol: str, side: str, ref: float, sl_bps: float, now: float,
                    spread_bps: float | None, signal_id: int | None, features: dict) -> Position | str:
        """Taker entry: walk the book for our size, pay the taker fee. Sized so the stop,
        including a taker exit, loses RISK_PCT of equity."""
        equity = self.equity()
        cost_frac = 2 * costs.TAKER_FEE + 2 * costs.LATENCY_BPS / 1e4
        notional = equity * RISK_PCT / 100 / (sl_bps / 1e4 + cost_frac)
        qty = notional / ref
        # Opening a long buys the asks — the same walk as closing a short.
        fill = costs.taker_exit_price("short" if side == "long" else "long", qty, self.depth.get(symbol), ref)
        lev = min(C.MAX_LEVERAGE, futures.safe_leverage(sl_bps / 100, symbol))
        margin = qty * fill / lev
        if margin > equity - self.margin_used():
            return f"insufficient margin (${margin:.2f} needed)"
        s = 1 if side == "long" else -1
        sl = fill * (1 - s * sl_bps / 1e4)
        pos = Position(
            _id(), symbol, side, qty, fill, now, lev, margin, futures.liquidation_price(fill, lev, side, symbol),
            None, sl, None, sl_bps, costs.fee(qty * fill, maker=False),
            abs(fill - sl) * qty + qty * fill * cost_frac, signal_id,
            {**features, "trail_bps": sl_bps, "entry_slippage_bps": round((fill - ref) / ref * 1e4 * s, 3)},
            0.0, None, spread_bps, None,
        )
        self.positions[symbol] = pos
        self.entries_last_hour.append(now)
        return pos

    def on_trade(self, symbol: str, price: float, qty: float, buyer_is_maker: bool, now: float) -> list[dict]:
        events = super().on_trade(symbol, price, qty, buyer_is_maker, now)
        p = self.positions.get(symbol)
        trail = p and p.features.get("trail_bps")
        if trail:
            peak = p.entry_price * (1 + p.s * p.mfe_bps / 1e4)       # best price since entry
            new_sl = peak * (1 - p.s * trail / 1e4)
            if (new_sl - p.sl) * p.s > 0:
                p.sl = new_sl
        return events

    def _close(self, p: Position, price: float, reason: str, now: float, maker: bool,
               trigger: float, tp_order=None) -> dict:
        if reason == "stop" and (p.sl - p.entry_price) * p.s > 0:
            reason = "trailing_stop"                                  # stopped out in profit
        ev = super()._close(p, price, reason, now, maker, trigger, tp_order)
        ev["trade"]["entry_maker"] = 0
        return ev


# ── Engine ─────────────────────────────────────────────────────────────────────

class ImpulseEngine(ScalperEngine):
    def __init__(self, store: Store | None = None):
        self.last_event: dict[str, float] = {}
        self.baselines: dict[str, tuple[float, dict | None]] = {}
        self.seeds: dict[str, dict] = {}
        # Its bars and liquidations go to the shared research market DB (the archive the research
        # labs read) — it has been the only live feed since the order-flow scalper was retired.
        super().__init__(store or Store(TRADES_DB, C.MARKET_DB, HORIZONS))

    # Always paper, always mainnet data.
    def _make_book(self) -> Book:
        book = ImpulseBook(self.store.get_state("balance", BALANCE))
        book.initial_balance = self.store.get_state("initial_balance", BALANCE)
        book.paused = bool(self.store.get_state("paused", False))
        return book

    def _restore_positions(self) -> None:
        for d in self.store.get_state("positions", []) or []:
            p = Position(**d)
            self.book.positions[p.symbol] = p
        if self.book.positions:
            logger.info("Impulse scalper restored %d open position(s)", len(self.book.positions))

    async def _seed(self) -> None:
        """Fetch the past hour of 1m candles per coin so detection needn't wait 30 minutes."""
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            async def one(sym: str) -> None:
                try:
                    r = await client.get(KLINES_URL, params={"symbol": sym, "interval": "1m", "limit": 61})
                    r.raise_for_status()
                    base = seed_baseline(r.json())
                    if base:
                        self.seeds[sym] = base
                except Exception as e:
                    logger.warning("Impulse: couldn't seed %s from 1m candles (%s) — full warm-up for it", sym, e)
            await asyncio.gather(*(one(s) for s in self.markets))
        logger.info("Impulse: seeded %d/%d coins from 1m candles — detecting after %ds of live data",
                    len(self.seeds), len(self.markets), SLOW_WINDOW + 1)

    async def start(self) -> None:
        if SEED:
            await self._seed()
        await super().start()
        logger.info("Impulse scalper (paper, %s) watching %d coins for extreme moves", MODE, len(self.markets))

    def _exit(self, sym: str, reason: str, now: float, f: dict | None = None) -> None:
        pos = self.book.positions.get(sym)
        if not pos:
            return
        m = self.markets[sym]
        price = (m.best_bid if pos.side == "long" else m.best_ask) or m.mid or pos.entry_price
        ev = self.book.close_market(sym, reason, now, price)
        if ev:
            self._handle([ev], now)

    def _baseline(self, sym: str, m: MarketState, now: float) -> dict | None:
        """The live baseline once there's WARMUP_S of our own history; the seeded one until then."""
        at, base = self.baselines.get(sym, (0.0, None))
        if now - at >= BASELINE_REFRESH_S or base is None:
            base = baseline(m)
            self.baselines[sym] = (now, base)
        return base or self.seeds.get(sym)

    def _ready_after(self, sym: str) -> int:
        return SLOW_WINDOW + 1 if sym in self.seeds else WARMUP_S

    async def _evaluate(self, now: float) -> None:
        stale = self.feed_age(now) > C.STALE_FEED_SECONDS
        for sym, m in self.markets.items():
            pos = self.book.positions.get(sym)
            if pos:
                if now - pos.entry_ts >= TIME_STOP:
                    self._exit(sym, "time_stop", now)
                else:
                    mark = m.mid or pos.entry_price
                    self.last_decision[sym] = {"status": "in position", "open_bps": round(pos.move_bps(mark), 2),
                                               "stop": pos.sl, "mid": mark}
                continue
            hist = m.history_seconds()
            if hist < self._ready_after(sym):
                self.last_decision[sym] = {"status": "warming up", "history_s": hist, "ready_s": self._ready_after(sym)}
                continue
            base = self._baseline(sym, m, now)
            ms = measure(m, base)
            if ms is None:
                self.last_decision[sym] = {"status": "warming up", "history_s": hist}
                continue
            fast = ms[FAST_WINDOW]
            status = {"status": "watching", "mid": m.mid, "baseline": "seeded" if base.get("seeded") else "live", "move_60s_bps": round(fast["move_bps"], 1),
                      "z_60s": round(fast["z"], 2), "vol_mult_60s": round(fast["vol_mult"], 2),
                      "move_300s_bps": round(ms[SLOW_WINDOW]["move_bps"], 1)}
            if stale:
                status["status"] = "feed stale — no entries"
            if not ms["hit"] or stale:
                self.last_decision[sym] = status
                continue
            if now - self.last_event.get(sym, 0) < DEDUPE_S:
                status["status"] = "same burst — already counted"
                self.last_decision[sym] = status
                continue
            self.last_event[sym] = now
            await self._on_event(sym, m, ms, status, now, seeded=bool(base.get("seeded")))

    async def _on_event(self, sym: str, m: MarketState, ms: dict, status: dict, now: float,
                        seeded: bool = False) -> None:
        w = ms["hit"]
        ev = ms[w]
        up = ev["move_bps"] > 0
        side = ("long" if up else "short") if MODE == "ride" else ("short" if up else "long")
        f = m.features() or {"mid": m.mid}
        sl = stop_bps(ev["move_bps"])
        desc = (f"{w}s move {ev['move_bps']:+.0f} bps ({ev['z']:+.1f}σ, {ev['vol_mult']:.1f}× volume) → "
                f"{MODE} {side}, stop {sl:.0f} bps trailed" + (" [baseline seeded from 1m candles]" if seeded else ""))
        ok, why = self.book.can_enter(sym, now)
        spread, med = f.get("spread_bps"), f.get("median_spread_bps")
        if ok and spread and med and spread > MAX_SPREAD_MULT * med:
            ok, why = False, f"spread {spread:.1f} bps ≥ {MAX_SPREAD_MULT:.0f}× normal"
        signal_id = await asyncio.to_thread(self.store.add_signal, {
            "ts": now, "symbol": sym, "side": side, "decision": "entered" if ok else "blocked",
            "reason": desc if ok else f"{desc} — blocked: {why}", **f,
            "move_bps": ev["move_bps"], "move_z": ev["z"], "vol_mult": ev["vol_mult"], "window_s": w})
        if signal_id and f.get("mid"):
            sign = 1 if side == "long" else -1
            for h in HORIZONS:
                self.labels.append((now + h, "signal", signal_id, sym, f["mid"], sign, h))
        if not ok:
            status["status"] = f"event blocked: {why}"
            self.last_decision[sym] = status
            return
        ref = m.best_ask if side == "long" else m.best_bid
        pos = self.book.open_market(sym, side, ref or f["mid"], sl, now, spread, signal_id,
                                    {**_brief(f, full=True), "window_s": w, "move_bps": round(ev["move_bps"], 2),
                                     "move_z": round(ev["z"], 2), "vol_mult": round(ev["vol_mult"], 2),
                                     "baseline": "seeded" if seeded else "live"})
        if isinstance(pos, str):
            self.store.add_event("entry_rejected", {"symbol": sym, "reason": pos})
            status["status"] = f"event blocked: {pos}"
        else:
            pos.next_funding_ms = m.next_funding_ms
            self._persist_positions()
            logger.info("Impulse entry %s %s @ %s — %s", sym, side, pos.entry_price, desc)
            status["status"] = "entered"
        self.last_decision[sym] = status

    def live(self) -> dict:
        out = super().live()
        out["experiment"] = "impulse"
        out["warmup_s"] = min((self._ready_after(s) for s in self.markets), default=WARMUP_S)
        out["seeded"] = sorted(self.seeds)
        out["config"] = {
            "mode": MODE, "fast_window_s": FAST_WINDOW, "fast_z": FAST_Z, "fast_min_bps": FAST_MIN_BPS,
            "fast_vol_mult": FAST_VOL_MULT, "slow_window_s": SLOW_WINDOW, "slow_min_bps": SLOW_MIN_BPS,
            "dedupe_s": DEDUPE_S, "sl_min_bps": SL_MIN_BPS, "sl_max_bps": SL_MAX_BPS, "sl_move_frac": SL_MOVE_FRAC,
            "time_stop_s": TIME_STOP, "risk_pct": RISK_PCT, "max_positions": MAX_POSITIONS,
            "max_spread_mult": MAX_SPREAD_MULT, "taker_fee_bps": round(costs.TAKER_FEE * 1e4, 2),
            "max_leverage": C.MAX_LEVERAGE, "daily_loss_limit_pct": C.DAILY_LOSS_LIMIT_PCT,
        }
        return out


engine: ImpulseEngine | None = None


async def start() -> None:
    global engine
    if not ENABLED or engine is not None:
        return
    engine = ImpulseEngine()
    await engine.start()


async def stop() -> None:
    global engine
    if engine is not None:
        await engine.stop()
        engine = None
