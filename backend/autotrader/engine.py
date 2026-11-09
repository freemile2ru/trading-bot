"""The autonomous paper-trading experiment.

Runs after every scanner sweep: walks open positions and resting orders through the
minute-by-minute price path, exits positions on stop, target, expiry, a flipped signal or
the coin leaving the watchlist, and decides — per watched symbol — whether now is the
moment to enter. It trades its own wallet (`auto_trades.json`), fully separate from the
user's main paper wallet, so the two strategies can be compared side by side.

Entry is two-stage. The deterministic rules in `strategy.py` pick the moment; Claude then
gets the final say before any money moves. Claude is asked at most once per asset per
position: the confirmation key is held while the order rests and the trade is open, and
released only when it closes (or the order is cancelled). A declined entry vetoes the
asset for VETO_SECONDS so the rules don't re-ask every minute.

Once confirmed, the entry is a market order when Claude's suggested entry is at or near
the current price, and otherwise a limit order resting just short of Claude's level
(see `strategy.plan_entry`).
"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import os
import time
from datetime import datetime, timezone

from autotrader import lanes, strategy
# Imported as `kv`: several functions here keep a local dict called `state`.
from storage import state as kv
from oracle.claude_oracle import synthesize_trade
from scanner import bus, escalation
from trading import exits, monitor
from storage.db import SWING_DB
from trading import book_switches, coin_lock, guards, regime
from experiments import books as experiment_books
from trading.paper_trader import PaperWallet, main_wallet
from live import swing as live_swing

logger = logging.getLogger("autotrader")

ENABLED = os.getenv("AUTOTRADER_ENABLED", "true").lower() != "false"
COOLDOWN_SECONDS = int(os.getenv("AUTOTRADER_COOLDOWN", "1800"))   # after any exit, per symbol
LLM_DAILY_BUDGET = int(os.getenv("AUTOTRADER_LLM_DAILY_BUDGET", "50"))
LLM_BUDGET_WARN = int(os.getenv("AUTOTRADER_LLM_BUDGET_WARN", "20"))   # UI turns red past this
MIN_CLAUDE_CONFIDENCE = int(os.getenv("AUTOTRADER_MIN_CLAUDE_CONFIDENCE", "65"))
VETO_SECONDS = int(os.getenv("AUTOTRADER_VETO_SECONDS", "3600"))

_btc_vol: dict = {"at": 0.0, "pct": None}


async def btc_vol_pct() -> float | None:
    """BTC's realized volatility over the last 30 days of hourly closes, annualized %. Cached 30 min."""
    if time.time() - _btc_vol["at"] > 1800:
        import math
        from data.binance_client import fetch_ohlcv
        try:
            k = await fetch_ohlcv("BTCUSDT", "1h", 721)
            r = [math.log(b["close"] / a["close"]) for a, b in zip(k, k[1:])]
            m = sum(r) / len(r)
            _btc_vol.update(at=time.time(),
                            pct=math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(24 * 365) * 100)
        except Exception as e:
            logger.warning("BTC volatility unavailable (%s) — regime gate skipped this sweep", e)
    return _btc_vol["pct"]


PREV_MIN_AGE = 50          # both tiers can scan seconds apart; don't compare a tick with itself
HISTORY_POINTS = 300       # equity points sent to the chart
PENDING_TTL = 600          # a confirmation lost to a restart releases itself after this
HELD_TTL = 30 * 86400      # "approved" key lives for the position; deleted explicitly on close
MISSED_TTL = 900           # price ran away while Claude was thinking — brief pause, then re-arm
REUSE_SIGNAL_SECONDS = 3600  # a scanner signal this fresh counts as Claude's confirmation
MAX_DRIFT_OF_STOP = 0.5    # market entries: skip if price moved > half the stop distance

AUTO_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "auto_trades.json")
auto_wallet = PaperWallet(SWING_DB, label="auto", legacy_json=AUTO_FILE,
                          history_cap=3000, min_snapshot_seconds=120)
FLIP_CONFIRM_SECONDS = int(os.getenv("AUTOTRADER_FLIP_CONFIRM", "900"))   # a flip must persist this long
# Retired 2026-10-19: tightening a trade's stop when its coin left the watchlist made no difference —
# research/tighten_lab.py replayed 120 such trades without it: +0.017R, 90% range −0.10…+0.13 — and on the
# live ledger each tightening is another exchange order. Resting orders are still cancelled on leaving.
TIGHTEN_ON_LEAVE = os.getenv("AUTOTRADER_TIGHTEN_ON_LEAVE", "false").lower() == "true"
# At least one trade a day (2026-10-25, _daily_minimum): after DAILY_MIN_HOURS with no entry, take the strongest
# setup at DAILY_MIN_RISK_X of normal risk. Off with AUTOTRADER_DAILY_MIN=false.
DAILY_MIN_ENABLED = os.getenv("AUTOTRADER_DAILY_MIN", "true").lower() != "false"
DAILY_MIN_HOURS = float(os.getenv("AUTOTRADER_DAILY_MIN_HOURS", "24"))
DAILY_MIN_RISK_X = float(os.getenv("AUTOTRADER_DAILY_MIN_RISK_X", "0.25"))
DAILY_MIN_CONVICTION = float(os.getenv("AUTOTRADER_DAILY_MIN_CONVICTION", "0.15"))
DAILY_MIN_FLOOR = float(os.getenv("AUTOTRADER_DAILY_MIN_FLOOR", "0.05"))     # after 6 more idle hours
# Dry-market widening (2026-10-28): after DRY_HOURS without an entry the scanner's universe grows from its usual ten to
# DRY_UNIVERSE coins and the autotrader considers every scanned coin, not just the watchlist — the normal rules,
# Claude review and guards still apply, there are simply more candidates. Narrows back once something is entered.
DRY_HOURS = float(os.getenv("AUTOTRADER_DRY_HOURS", "12"))
DRY_UNIVERSE = int(os.getenv("AUTOTRADER_DRY_UNIVERSE", "30"))
_dry_cache: dict = {"at": 0.0, "idle_h": 0.0}
# Event shorts in the autotrader (2026-10-27): the event book (experiments/events.py) decides — listings and
# delistings fell 15–22% vs BTC over a week on ~200 announcements since 2023 — and the autotrader mirrors each of
# its positions on its own ledger (the live one when live): the coin short plus a BTC long hedge, closed when the
# book closes. They skip Claude, the direction gate and the entry-timing rules: the edge is the event, not the chart.
# Mirrors are OFF by default since 2026-10-29: each signal is traded in exactly one book (the event, unlock and weekly
# books hold their own positions), so a stop is never paid twice. Set AUTOTRADER_EVENTS/UNLOCKS/WEEKLY=true to mirror.
# The executor (2026-10-31): the autotrader is the one account that trades every signal with a record. The evidence books
# keep running their rules on their own wallets, but those are SIGNAL ledgers (trading/overview.SIGNAL_SYSTEMS): every
# position they open is mirrored here, sized by the book's plan share of this ledger and the lane allocator, and closed
# when the book closes it. AUTOTRADER_EXECUTOR=false returns to the per-lane switches below.
EXECUTOR = os.getenv("AUTOTRADER_EXECUTOR", "true").lower() != "false"
EVENTS_IN_AUTO = EXECUTOR or os.getenv("AUTOTRADER_EVENTS", "false").lower() == "true"
EVENT_LEG_PCT = float(os.getenv("AUTOTRADER_EVENT_LEG_PCT", "5"))      # % of equity a leg (half the paper book's)
EVENT_STOP_PCT = 25.0
EVENT_HEDGE_STOP_PCT = 40.0
# Lanes (2026-10-29, autotrader/lanes.py): the scanner path ("ta") runs beside mirrors of the evidence books — event
# shorts, unlock shorts and the weekly basket — and each lane's entries are sized by its own record in the current
# regime. The mirrors use the same per-leg size; the weekly basket mirrors through _mirror_wallet_book like the others.
UNLOCKS_IN_AUTO = EXECUTOR or os.getenv("AUTOTRADER_UNLOCKS", "false").lower() == "true"
WEEKLY_IN_AUTO = EXECUTOR or os.getenv("AUTOTRADER_WEEKLY", "false").lower() == "true"
CASCADE_IN_AUTO = EXECUTOR or os.getenv("AUTOTRADER_CASCADE", "false").lower() == "true"
FACTOR_IN_AUTO = EXECUTOR or os.getenv("AUTOTRADER_FACTOR", "false").lower() == "true"
REVERSAL_IN_AUTO = EXECUTOR or os.getenv("AUTOTRADER_REVERSAL", "false").lower() == "true"
# The scanner lane ("ta": the scanner's conviction with Claude's confirmation, the original autotrader) was retired on
# 2026-11-03 with the user: 20 trades at −0.13R (longs −0.17R), after every scanner-signal book before it had been
# retired on the same finding. The scanner keeps running — the reversal lane is built from its scans — but its
# conviction no longer opens trades here. Set AUTOTRADER_TA_LANE=true to bring it back.
TA_LANE = os.getenv("AUTOTRADER_TA_LANE", "false").lower() == "true"
REVERSAL_K = int(os.getenv("AUTOTRADER_REVERSAL_K", "2"))          # names a side every 4h
# A reversal leg risks 0.2% of the ledger (was 1 until 2026-11-06). The lane is at measuring size — ENTRY_BUDGET_USD
# over its picks — but a leg is sized by the LARGER of that share and the risk floor, and after the 2026-11-04 re-plan
# put $50k on the prod ledger the 1% floor made every leg $5k at a 10% stop: the ZEC short lost $546 on a lane whose
# five wins had made $516 together. At 0.2% the floor equals the measuring share on a $50k ledger ($1k a leg, $100 at risk).
REVERSAL_RISK_PCT = float(os.getenv("AUTOTRADER_REVERSAL_RISK_PCT", "0.2"))
REVERSAL_MAX_OPEN = int(os.getenv("AUTOTRADER_REVERSAL_MAX_OPEN", "12"))
# Short side only (2026-11-02): on the lab's 190 slots (09-27 → 11-01) shorting the top of the score earned +1.7%/slot vs BTC
# after costs (t 4.9, both halves positive, 68% of slots) while buying the bottom LOST −0.4%/slot in both halves and under
# both BTC trends. The first live day said the same: every short won, every long lost. Set "short,long" to run both.
REVERSAL_SIDES = tuple(s.strip() for s in os.getenv("AUTOTRADER_REVERSAL_SIDES", "short").split(",") if s.strip() in ("short", "long"))
# Risk floor for every mirrored leg (2026-11-01, "real-world P&L numbers"): a leg risks at least LANE_RISK_PCT of the
# ledger at its stop, so notional ≥ equity × LANE_RISK_PCT / stop%, never above LANE_MAX_NOTIONAL_PCT of the ledger.
LANE_RISK_PCT = float(os.getenv("AUTOTRADER_LANE_RISK_PCT", "2"))    # 2% (set 3 for a day on 2026-11-01): the weekly and factor baskets mirror 9–17 legs at once
LANE_MAX_NOTIONAL_PCT = float(os.getenv("AUTOTRADER_LANE_MAX_NOTIONAL_PCT", "50"))
REVERSAL_HOLD_H = 24.0
REVERSAL_STOP_PCT = float(os.getenv("AUTOTRADER_REVERSAL_STOP_PCT", "10"))   # a disaster stop: the lab measured plain 24h returns, no stop
UNLOCK_STOP_PCT = 25.0
UNLOCK_HEDGE_STOP_PCT = 40.0

STATE_KEY = kv.key("auto", "state")
PAUSED_KEY = kv.key("auto", "paused")


@dataclass(frozen=True)
class Variant:
    """Which book the engine is running for. The real autotrader asks Claude before entering;
    its rules-only twin runs the identical rules on the same scans and enters wherever Claude
    would have been asked — a paired test of what Claude's judgment adds (experiments/books.py)."""
    name: str
    use_claude: bool
    wallet: PaperWallet | None = None      # None: the real autotrader (paper wallet + live ledger)

    @property
    def primary(self) -> bool:
        return self.wallet is None


PRIMARY = Variant("auto", use_claude=True)
RULES_TWIN = Variant("auto_rules", use_claude=False, wallet=experiment_books.WALLETS["auto_rules"])
_variant: ContextVar[Variant] = ContextVar("autotrader_variant", default=PRIMARY)


def _variants() -> list[Variant]:
    return [PRIMARY] + ([RULES_TWIN] if experiment_books.ENABLED else [])


def _v() -> Variant:
    return _variant.get()


def _state_key() -> str:
    return STATE_KEY if _v().primary else kv.key(_v().name, "state")


def _publish(topic: str, data: dict) -> None:
    """The Autotrader panel shows the real autotrader only; the twin is read from the Lab tab."""
    if _v().primary:
        bus.publish(topic, data)

_lock = asyncio.Lock()
_confirmations: set[asyncio.Task] = set()   # strong refs so in-flight calls aren't GC'd


def _cooldown_key(symbol: str) -> str:
    return kv.key(_v().name, "cd", symbol)


def _claude_key(symbol: str) -> str:
    return kv.key(_v().name, "claude", symbol)


def _budget_key() -> str:
    return kv.key("auto", "llm_budget", datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def _wallets() -> list[PaperWallet]:
    """The paper wallet and the live ledger — positions are managed wherever they were opened."""
    v = _v()
    return [v.wallet] if v.wallet else live_swing.wallets("auto")


def _active() -> PaperWallet:
    """Where new entries go: the live ledger when the autotrader is switched to live."""
    v = _v()
    return v.wallet or live_swing.active("auto")


async def is_paused() -> bool:
    return bool(await kv.get(PAUSED_KEY))


async def set_paused(paused: bool) -> None:
    if paused:
        await kv.set(PAUSED_KEY, True, 10 * 365 * 86400)
        # Pausing stops new entries — a resting limit is a new entry waiting to happen.
        async with _lock:
            for w in _wallets():
                for order in w.get_orders()["pending"]:
                    await _cancel_order(order, "paused")
    else:
        await kv.delete(PAUSED_KEY)


def open_symbols() -> set[str]:
    """Symbols with an open position or a resting order in the auto wallet (and its twin)."""
    try:
        wallets = live_swing.wallets("auto") + ([RULES_TWIN.wallet] if experiment_books.ENABLED else [])
        return {x["symbol"] for w in wallets
                for x in w.get_portfolio()["open_trades"] + w.get_orders()["pending"]}
    except Exception:
        return set()


async def _release(symbol: str) -> None:
    """The asset's position/order is over — cooldown, and the next entry earns a fresh Claude check."""
    await kv.set(_cooldown_key(symbol), {"until": time.time() + COOLDOWN_SECONDS}, COOLDOWN_SECONDS)
    await kv.delete(_claude_key(symbol))


async def _on_closed(trade: dict) -> None:
    await _release(trade["symbol"])
    logger.info(
        "Auto-trade closed: %s %s — %s @ %s, P&L $%.2f",
        trade["symbol"], trade["direction"], trade["close_reason"], trade["current_price"], trade["pnl_usd"],
    )
    _publish("auto_trade", {"event": "closed", "trade": trade})


async def _cancel_order(order: dict, reason: str) -> None:
    for w in _wallets():
        cancelled = await asyncio.to_thread(w.cancel_order, order["id"], reason)
        if cancelled:
            await _on_order_cancelled(cancelled)
            return


async def _on_order_cancelled(order: dict) -> None:
    await _release(order["symbol"])
    logger.info("Auto limit order cancelled: %s %s @ %s — %s",
                order["symbol"], order["direction"], order["limit_price"], order["close_reason"])
    _publish("auto_trade", {"event": "order_cancelled", "order": order})


# ── Monitoring: fills and exits along the 1-minute price path ───────────────────

async def _live_price(symbol: str, scans: dict[str, dict]) -> float | None:
    """The perpetual's price — that's where fills happen. Spot (scan) price as a fallback."""
    return await monitor.live_price(symbol) or scans.get(symbol, {}).get("price")


async def _monitor(scans: dict[str, dict]) -> None:
    """Replay the perpetual's minute bars through orders and positions (see trading/monitor)."""
    events = []
    for w in _wallets():
        events += await monitor.run(w)
    for event in events:
        if event["type"] == "closed":
            await _on_closed(event["trade"])
        elif event["type"] == "order_expired":
            await _on_order_cancelled(event["order"])
        elif event["type"] == "filled":
            trade = event["trade"]
            logger.info("Auto limit filled: %s %s @ %s (limit %s, %dx)",
                        trade["symbol"], trade["direction"], trade["entry_price"], trade["limit_price"],
                        trade["leverage"])
            _publish("auto_trade", {"event": "filled", "trade": trade})


async def monitor_now() -> None:
    """Fills and exits for the auto wallet, independent of any scan (called every minute)."""
    if not ENABLED:
        return
    for v in _variants():
        token = _variant.set(v)
        try:
            async with _lock:
                count = lambda: sum(len(w.get_portfolio()["open_trades"]) for w in _wallets())
                before = count()
                await _monitor({})
                changed = count() != before
            if changed and v.primary:
                bus.publish("auto", await summary())
        except Exception as e:
            logger.warning("%s monitoring failed: %s", v.name, e)
        finally:
            _variant.reset(token)


async def _thesis_exits(scans: dict[str, dict], watched: set[str]) -> None:
    """React to a weakened thesis — tighten, or close only when a reversal is confirmed.

    * Coin left the watchlist: (retired 2026-10-19, see TIGHTEN_ON_LEAVE) tightened the stop once.
      The watchlist is an attention filter, not a thesis — leaving it isn't a reason to take a
      loss at market, and the replay showed tightening didn't help either.
    * Signal flip: must persist FLIP_CONFIRM_SECONDS (about one 15m candle) before the
      position is closed; a flip that reverts within that window is ignored.
    """
    # If the state database can't be read, the watchlist reads as empty — never act on that.
    watchlist_known = await kv.healthy()
    now = time.time()

    for wallet, trade in [(w, t) for w in _wallets() for t in w.get_portfolio()["open_trades"]]:
        symbol = trade["symbol"]
        if lanes.lane_of(trade) != "ta":
            continue                                    # a mirrored book position: that book manages it, not the scan

        if TIGHTEN_ON_LEAVE and watchlist_known and symbol not in watched and not trade.get("watchlist_tightened"):
            price = await _live_price(symbol, scans) or trade["current_price"]
            s = 1 if trade["direction"] == "long" else -1
            r_unit = trade.get("r_unit") or abs(trade["entry_price"] - trade["stop_loss"]) or 1
            if (price - trade["entry_price"]) * s / r_unit >= 0.5:
                target = trade["entry_price"] + s * exits.cost_offset(trade["entry_price"])
            else:
                target = trade["stop_loss"] + (price - trade["stop_loss"]) / 2
            moved = await asyncio.to_thread(wallet.move_stop, trade["id"], target,
                                            "coin left the watchlist — stop tightened")
            await asyncio.to_thread(wallet.annotate, trade["id"], watchlist_tightened=True)
            if moved:
                logger.info("Auto-trade %s left the watchlist — stop tightened to %s", symbol, moved["stop_loss"])
                _publish("auto_trade", {"event": "adjusted", "trade": moved})

        if symbol not in scans:
            continue
        _, fast = strategy.fast_tf(scans[symbol])
        note = strategy.should_flip_exit(trade, scans[symbol], fast)
        since = trade.get("flip_pending_since")
        if not note:
            if since:
                await asyncio.to_thread(wallet.annotate, trade["id"], flip_pending_since=None)
            continue
        if not since:
            await asyncio.to_thread(wallet.annotate, trade["id"], flip_pending_since=now)
            continue
        if now - since < FLIP_CONFIRM_SECONDS:
            continue
        price = await _live_price(symbol, scans) or trade["current_price"]
        closed = await asyncio.to_thread(wallet.close_trade_manually, trade["id"], price, "signal_flip")
        if closed:
            closed["exit_note"] = f"{note} (held {int((now - since) / 60)}m)"
            await _on_closed(closed)

    for order in [o for w in _wallets() for o in w.get_orders()["pending"]]:
        symbol = order["symbol"]
        if watchlist_known and symbol not in watched:
            await _cancel_order(order, "left_watchlist")
        elif symbol in scans:
            _, fast = strategy.fast_tf(scans[symbol])
            if strategy.should_flip_exit(order, scans[symbol], fast):
                await _cancel_order(order, "signal_flip")


# ── Entering ────────────────────────────────────────────────────────────────────

def _fa_snapshot(fa: dict | None) -> dict | None:
    """The FA reading a trade was entered on — kept so History can test whether FA predicts results."""
    if not fa:
        return None
    return {"score": fa.get("score"), "label": fa.get("label"), "rs_vs_btc_24h": fa.get("rs_vs_btc_24h"),
            "components": {k: v.get("score") for k, v in (fa.get("components") or {}).items()},
            "features": fa.get("features")}


def _heat_room_pct(w: PaperWallet | None = None) -> float:
    """How much more loss-at-stop (as % of equity) a wallet — by default the active one — can take on."""
    w = w or _active()
    p = w.get_portfolio()
    equity = p["total_equity"] or 1
    pending = sum(o["notional_usd"] * abs(o["limit_price"] - o["stop_loss"]) / o["limit_price"]
                  for o in w.get_orders()["pending"] if o.get("notional_usd"))
    return strategy.MAX_OPEN_RISK_PCT - (p["open_risk_usd"] + pending) / equity * 100


def _gross_pct() -> float:
    """Committed notional — open positions plus resting orders that could fill."""
    w = _active()
    return (
        sum(t.get("size_pct", 0) for t in w.get_portfolio()["open_trades"])
        + sum(o.get("size_pct", 0) for o in w.get_orders()["pending"])
    )


def _as_maker(plan: dict, direction: str, price: float) -> dict:
    """A market entry as a post-only limit just inside the market (strategy.MAKER_ENTRIES)."""
    s = 1 if direction == "long" else -1
    limit = round(price * (1 - s * strategy.MAKER_OFFSET_BPS / 1e4), 8)
    return {**plan, "type": "limit", "price": limit, "maker": True, "valid_seconds": strategy.MAKER_TTL_S,
            "note": f"{plan['note']} — as a maker limit {limit:.6g} ({strategy.MAKER_OFFSET_BPS:g} bps inside), "
                    f"{strategy.MAKER_TTL_S // 60} min to fill"}


def _as_pullback(plan: dict, direction: str, price: float, levels: dict) -> dict:
    """A market entry as a post-only limit PULLBACK_R of the stop distance back (strategy.PULLBACK_ENTRIES)."""
    limit = strategy.pullback_limit(direction, price, levels["stop_loss"])
    hours = strategy.PULLBACK_TTL_S / 3600
    return {**plan, "type": "limit", "price": limit, "maker": True, "pullback": True, "valid_seconds": strategy.PULLBACK_TTL_S,
            "note": f"{plan['note']} — as a pullback limit {limit:.6g} ({strategy.PULLBACK_R:g}R back, stop unchanged), "
                    f"{hours:g}h to fill"}


def _market_twin(scan: dict, direction: str, price: float, fast: dict, order_id: str, extra: dict) -> None:
    """The market entry this maker order replaced, tracked as a shadow: filled maker trades are
    compared with it (the saving), and when the order misses, it shows the trade that was lost."""
    try:
        lv = strategy.build_levels(direction, price, scan, fast)
        _active().add_virtual(scan["symbol"], direction, price, lv["stop_loss"], lv["take_profit_1"],
                              lv["take_profit_2"], scan.get("best_timeframe") or "1h", strategy.RISK_PCT,
                              {"claude_verdict": "market_twin", "maker_order_id": order_id,
                               "exit_policy": extra.get("exit_policy") or exits.DEFAULT_POLICY})
    except Exception as e:
        logger.debug("Market twin for %s not tracked: %s", scan["symbol"], e)


async def _enter(
    scan: dict, direction: str, timing: dict, reasons: list[str], price: float,
    confirmation: dict, armed_at: float | None, claude_entry: float | None,
) -> dict | None:
    """Place the confirmed entry — market or limit. Caller holds `_lock`.

    Returns {"kind": "trade"|"order", "record": ..., "plan": ...} or None.
    """
    from ops import calendar
    blackout = calendar.macro_blackout()
    if blackout:
        logger.info("Auto-entry %s held back — %s", scan["symbol"], blackout)
        return None
    # Claude can take a minute; another system may have taken the coin meanwhile.
    try:
        busy = (await asyncio.to_thread(coin_lock.held_by_others, "auto")).get(scan["symbol"])
    except Exception:
        busy = [{"system": "other systems", "mode": "unreadable"}]
    if busy:
        logger.info("Auto-entry %s skipped — now in trade with %s", scan["symbol"], coin_lock.describe(busy))
        return None
    waited = await asyncio.to_thread(coin_lock.recent_stop_out, scan["symbol"], direction)
    if waited:
        logger.info("Auto-entry %s skipped — %s", scan["symbol"], waited)
        return None
    _, fast = strategy.fast_tf(scan)
    atr_levels = strategy.build_levels(direction, price, scan, fast)
    plan = strategy.plan_entry(direction, price, claude_entry, atr_levels["sl_pct"], scan.get("best_timeframe"))

    if plan["type"] == "market" and strategy.PULLBACK_ENTRIES:
        plan = _as_pullback(plan, direction, price, atr_levels)
    entry = plan["price"]
    if plan.get("pullback"):
        # The stop and targets stay where the signal price put them; only the entry is better.
        levels = {**atr_levels, "sl_pct": abs(entry - atr_levels["stop_loss"]) / entry * 100}
    else:
        levels = strategy.build_levels(direction, entry, scan, fast)
    lane_mult, lane_why = await _lane_mult("ta", scan["symbol"], direction)
    size = strategy.position_size_pct(levels["sl_pct"], _gross_pct()) * guards.risk_mult(_v().name) * lane_mult
    if size < 1.0:
        logger.info("Auto-entry %s skipped — wallet fully allocated", scan["symbol"])
        return None
    heat = _heat_room_pct()
    risk_of_new = size * levels["sl_pct"] / 100          # % of balance lost at this stop
    if risk_of_new > heat:
        if heat < strategy.RISK_PCT * 0.25:
            logger.info("Auto-entry %s skipped — open risk at the %.1f%% cap", scan["symbol"],
                        strategy.MAX_OPEN_RISK_PCT)
            return None
        size = size * heat / risk_of_new                  # shrink to fit the remaining heat

    now = time.time()
    waited = int((now - (armed_at or now)) / 60)
    all_reasons = reasons + [plan["note"]]
    trade_fields = {
        "timeframe": scan.get("best_timeframe") or "1h",
        "oracle_confidence": confirmation.get("confidence") or timing["score"],
        "oracle_reasoning": confirmation.get("thesis") or "; ".join(all_reasons),
        "hold_duration": scan.get("hold_duration") or "N/A",
    }
    extra = {
        "strategy": "auto",
        "lane": "ta", "lane_mult": lane_mult, "lane_why": lane_why,
        "exit_policy": exits.DEFAULT_POLICY,
        "entry_score": timing["score"],
        "entry_reasons": all_reasons,
        "conviction_at_entry": scan.get("conviction"),
        "fa_at_entry": _fa_snapshot(scan.get("fa")),
        "setups_at_entry": (scan.get("conviction_detail") or {}).get("aligned_setups", []),
        "armed_minutes": waited,
        "confirmed_by": confirmation.get("source"),
        "claude_verdict": "approved" if _v().use_claude else "skipped",
        "claude_confidence": confirmation.get("confidence"),
        "claude_thesis": confirmation.get("thesis"),
        "claude_entry": claude_entry,
        "market_price_at_decision": price,
        "regime_at_entry": await regime.current(),
        "near_event": calendar.near_event(scan["symbol"]),
    }

    if plan["type"] == "market" and strategy.MAKER_ENTRIES and not strategy.PULLBACK_ENTRIES:
        plan = _as_maker(plan, direction, price)
        entry = plan["price"]
    if plan["type"] == "market":
        trade = await asyncio.to_thread(
            _active().open_trade,
            symbol=scan["symbol"], direction=direction, entry_price=entry,
            stop_loss=levels["stop_loss"], take_profit_1=levels["take_profit_1"],
            take_profit_2=levels["take_profit_2"], size_pct=size,
            **trade_fields,
            extra={**extra, "entry_type": "market"},
        )
        if trade.get("error"):
            return None
        logger.info("Auto-trade opened: %s %s @ %s market (timing %d, size %.1f%%, %s %s%%) — %s",
                    scan["symbol"], direction, entry, timing["score"], size,
                    confirmation.get("source"), confirmation.get("confidence"), plan["note"])
        _publish("auto_trade", {"event": "opened", "trade": trade})
        return {"kind": "trade", "record": trade, "plan": plan}

    # Limit: the exit policy's clocks (stale, max hold) start when the order fills.
    order = await asyncio.to_thread(
        _active().place_order,
        symbol=scan["symbol"], direction=direction, limit_price=entry,
        stop_loss=levels["stop_loss"], take_profit_1=levels["take_profit_1"],
        take_profit_2=levels["take_profit_2"], size_pct=size,
        expires_at=now + plan["valid_seconds"],
        trade_fields={**trade_fields, "extra": extra},
        extra={"plan": plan, "post_only": bool(plan.get("maker")), "market_price_at_decision": price, "claude_entry": claude_entry,
               "claude_confidence": confirmation.get("confidence"), "reasons": all_reasons},
    )
    if order.get("error"):
        return None
    if plan.get("maker"):
        _market_twin(scan, direction, price, fast, order["id"], extra)
    logger.info("Auto limit placed: %s %s @ %s (market %s, Claude %s, valid %dm)",
                scan["symbol"], direction, entry, price, claude_entry, plan["valid_seconds"] // 60)
    _publish("auto_trade", {"event": "order_placed", "order": order})
    return {"kind": "order", "record": order, "plan": plan}


# ── Claude confirmation ─────────────────────────────────────────────────────────

async def llm_budget_used() -> int:
    return await kv.counter(_budget_key())


def _verdict(action: str, confidence: int, direction: str) -> tuple[bool, str]:
    wanted = "BUY" if direction == "long" else "SELL"
    if action != wanted:
        return False, f"Claude said {action}, not {wanted}"
    if confidence < MIN_CLAUDE_CONFIDENCE:
        return False, f"Claude agreed but only {confidence}% confident (need {MIN_CLAUDE_CONFIDENCE}%)"
    return True, f"Claude confirmed {wanted} at {confidence}%"


async def _reusable_signal(symbol: str, direction: str) -> dict | None:
    """A fresh scanner signal is already a Claude opinion on this asset — don't pay twice."""
    cutoff = time.time() - REUSE_SIGNAL_SECONDS
    for f in await escalation.recent_fired():
        if f.get("symbol") == symbol and f.get("fired_at", 0) >= cutoff:
            ok, _ = _verdict(f.get("action", "HOLD"), int(f.get("confidence") or 0), direction)
            if ok:
                return {"source": "scanner_signal", "confidence": f.get("confidence"),
                        "thesis": f.get("thesis", ""), "entry": f.get("entry_price")}
            return None
    return None


async def _set_state(symbol: str, **fields) -> None:
    state = (await kv.hgetall(_state_key())).get(symbol) or {"symbol": symbol}
    state.update(fields, updated_at=time.time())
    await kv.hset(_state_key(), symbol, state)


async def _hold_claude_key(symbol: str, direction: str, placed: dict, note: str) -> None:
    rec = placed["record"]
    await kv.set(_claude_key(symbol), {
        "status": "approved", "at": time.time(), "direction": direction, "note": note,
        "trade_id": rec["id"] if placed["kind"] == "trade" else None,
        "order_id": rec["id"] if placed["kind"] == "order" else None,
    }, HELD_TTL)


async def _confirm_and_enter(
    scan: dict, direction: str, timing: dict, reasons: list[str],
    decided_price: float, sl_pct: float, armed_at: float | None, global_market: dict,
) -> None:
    """Background task: ask Claude, then enter (market or limit) if it agrees."""
    symbol = scan["symbol"]
    key = _claude_key(symbol)
    try:
        result = await synthesize_trade(scan["signal_package"], global_market)
        if result.get("error"):
            logger.error("Claude confirmation failed for %s — %s", symbol, result["error"])
            await kv.delete(key)   # a failed call shouldn't count as Claude's answer
            await _set_state(symbol, status="armed", reasons=reasons + ["Claude call failed — will retry"])
            return

        rec = result.get("trade_recommendation", {})
        action = rec.get("action", "HOLD")
        confidence = int(rec.get("confidence") or 0)
        thesis = (result.get("reasoning") or {}).get("primary_thesis", "")
        ok, note = _verdict(action, confidence, direction)

        if not ok:
            logger.info("Claude declined auto-entry on %s %s — %s", symbol, direction, note)
            # Keep score: what would the rules alone have made here?
            try:
                price = await monitor.live_price(symbol) or decided_price
                _, fast = strategy.fast_tf(scan)
                lv = strategy.build_levels(direction, price, scan, fast)
                await asyncio.to_thread(
                    auto_wallet.add_virtual, symbol, direction, price, lv["stop_loss"],
                    lv["take_profit_1"], lv["take_profit_2"], scan.get("best_timeframe") or "1h",
                    strategy.RISK_PCT,
                    {"claude_verdict": "declined", "claude_confidence": confidence,
                     "fa_at_entry": _fa_snapshot(scan.get("fa")), "exit_policy": exits.DEFAULT_POLICY},
                )
            except Exception as e:
                logger.debug("Could not track declined entry for %s: %s", symbol, e)
            await kv.set(key, {"status": "rejected", "at": time.time(), "direction": direction,
                                  "note": note, "thesis": thesis}, VETO_SECONDS)
            await _set_state(symbol, status="vetoed", reasons=reasons + [note])
            return

        price = await monitor.live_price(symbol) or decided_price
        claude_entry = rec.get("entry_price")
        plan = strategy.plan_entry(direction, price, claude_entry, sl_pct, scan.get("best_timeframe"))

        # A market entry after a long think can be a chase; a limit just waits for price.
        drift = abs(price - decided_price) / decided_price * 100
        if plan["type"] == "market" and drift > MAX_DRIFT_OF_STOP * sl_pct:
            note = f"{note}, but price moved {drift:.2f}% while waiting — entry missed"
            logger.info("Auto-entry %s missed — %s", symbol, note)
            await kv.set(key, {"status": "missed", "at": time.time(), "direction": direction,
                                  "note": note}, MISSED_TTL)
            await _set_state(symbol, status="armed", reasons=reasons + [note])
            return

        confirmation = {"source": "claude", "confidence": confidence, "thesis": thesis}
        async with _lock:
            if await is_paused():
                await kv.delete(key)
                return
            placed = await _enter(scan, direction, timing, reasons + [note], price, confirmation,
                                  armed_at, claude_entry)

        if placed:
            await _hold_claude_key(symbol, direction, placed, note)
            status = "in_trade" if placed["kind"] == "trade" else "order_pending"
            await _set_state(symbol, status=status, reasons=[note, placed["plan"]["note"]], armed_at=None)
        else:
            await kv.delete(key)
    except Exception as e:
        logger.exception("Confirmation task for %s crashed: %s", symbol, e)
        await kv.delete(key)
    finally:
        bus.publish("auto", await summary())


async def _request_confirmation(
    scan: dict, direction: str, timing: dict, state: dict, global_market: dict,
) -> dict:
    """Route an entry decision through Claude. Returns the symbol's updated state."""
    symbol, reasons = scan["symbol"], state["reasons"]

    if not _v().use_claude:
        # The twin: the rules said enter — enter, exactly where the real autotrader asks Claude.
        levels = strategy.build_levels(direction, scan["price"], scan, strategy.fast_tf(scan)[1])
        if _heat_room_pct() < strategy.RISK_PCT * 0.25:
            return {**state, "status": "armed", "reasons": reasons + ["open-risk cap reached"]}
        if strategy.position_size_pct(levels["sl_pct"], _gross_pct()) < 1.0:
            return {**state, "status": "armed", "reasons": reasons + ["wallet fully allocated"]}
        note = "rules only — Claude skipped"
        price = await monitor.live_price(symbol) or scan["price"]
        placed = await _enter(scan, direction, timing, reasons + [note], price,
                              {"source": "rules", "confidence": None, "thesis": ""}, state.get("armed_at"), None)
        if placed:
            await _hold_claude_key(symbol, direction, placed, note)
            status = "in_trade" if placed["kind"] == "trade" else "order_pending"
            return {**state, "status": status, "armed_at": None, "reasons": [note, placed["plan"]["note"]]}
        return {**state, "status": "armed", "reasons": reasons + ["entry not placed"]}

    reuse = await _reusable_signal(symbol, direction)
    if reuse:
        note = "confirmed by a fresh scanner signal"
        price = await monitor.live_price(symbol) or scan["price"]
        placed = await _enter(scan, direction, timing, reasons + [note], price, reuse,
                              state.get("armed_at"), reuse.get("entry"))
        if placed:
            await _hold_claude_key(symbol, direction, placed, "reused scanner signal")
            status = "in_trade" if placed["kind"] == "trade" else "order_pending"
            return {**state, "status": status, "armed_at": None, "reasons": [note, placed["plan"]["note"]]}
        return {**state, "status": "armed", "reasons": reasons + ["wallet fully allocated"]}

    if await llm_budget_used() >= LLM_DAILY_BUDGET:
        return {**state, "status": "armed",
                "reasons": reasons + [f"Claude budget used up ({LLM_DAILY_BUDGET}/day) — entry skipped"]}

    levels = strategy.build_levels(direction, scan["price"], scan, strategy.fast_tf(scan)[1])
    if _heat_room_pct() < strategy.RISK_PCT * 0.25:
        return {**state, "status": "armed",
                "reasons": reasons + [f"portfolio at its {strategy.MAX_OPEN_RISK_PCT}% open-risk cap"]}
    if strategy.position_size_pct(levels["sl_pct"], _gross_pct()) < 1.0:
        return {**state, "status": "armed",
                "reasons": reasons + ["wallet fully allocated — waiting for a position to close"]}

    # Atomic claim — exactly one Claude call per asset, even across overlapping sweeps.
    if not await kv.acquire_once(_claude_key(symbol), PENDING_TTL):
        return {**state, "status": "confirming", "reasons": reasons + ["asking Claude…"]}
    await kv.set(_claude_key(symbol), {"status": "pending", "at": time.time(),
                                          "direction": direction}, PENDING_TTL)
    used = await kv.incr_with_expire(_budget_key(), 172800)

    logger.info("Asking Claude to confirm auto-entry %s %s (timing %d, budget %d/%d)",
                symbol, direction, timing["score"], used, LLM_DAILY_BUDGET)
    task = asyncio.create_task(
        _confirm_and_enter(scan, direction, timing, reasons, scan["price"], levels["sl_pct"],
                           state.get("armed_at"), global_market),
        name=f"auto-confirm-{symbol}",
    )
    _confirmations.add(task)
    task.add_done_callback(_confirmations.discard)
    return {**state, "status": "confirming", "reasons": reasons + ["rules say enter — asking Claude…"]}


# ── Per-sweep decision ──────────────────────────────────────────────────────────

def _fmt_minutes(seconds: float) -> str:
    m = max(0, int(seconds / 60))
    return f"{m}m" if m < 120 else f"{m // 60}h {m % 60}m"


async def _decide(
    scan: dict, prior: dict, held: set[str], orders: dict[str, dict], global_market: dict,
    elsewhere: dict[str, list[dict]] | None = None,
) -> dict:
    """One symbol's entry decision. Returns its new state."""
    symbol, now = scan["symbol"], time.time()
    tf, fast = strategy.fast_tf(scan)

    # Keep the previous fast-TF reading unless it's old enough to compare against.
    prev = prior.get("prev_fast")
    prev_at = prior.get("prev_at", 0)
    state = {
        "symbol": symbol,
        "base": scan.get("base"),
        "price": scan.get("price"),
        "conviction": scan.get("conviction"),
        "fast_tf": tf,
        "updated_at": now,
        "armed_at": prior.get("armed_at"),
        "prev_fast": prev,
        "prev_at": prev_at,
    }
    if fast and now - prev_at >= PREV_MIN_AGE:
        state["prev_fast"], state["prev_at"] = strategy.compact_fast(fast), now

    if symbol in held:
        claude = await kv.get(_claude_key(symbol)) or {}
        return {**state, "status": "in_trade", "armed_at": None,
                "reasons": ["position open"] + ([claude["note"]] if claude.get("note") else [])}

    order = orders.get(symbol)
    if order:
        away = abs(scan["price"] - order["limit_price"]) / scan["price"] * 100
        return {**state, "status": "order_pending", "direction": order["direction"], "armed_at": None,
                "reasons": [f"limit {order['limit_price']:.6g} — {away:.2f}% away, "
                            f"expires in {_fmt_minutes(order['expires_at'] - now)}"]}

    # Another system (main wallet, scalper) is in this coin — don't stack a second position on it.
    # When it exits, the coin gets a full fresh review: current scan, and a new Claude check
    # (any verdict from before the block is dropped) — the old signal is never replayed.
    busy = (elsewhere or {}).get(symbol)
    if busy:
        return {**state, "status": "busy_elsewhere", "armed_at": None, "blocked_by": busy,
                "reasons": [f"in trade with {coin_lock.describe(busy)} — re-reviewed once it exits"]}
    if prior.get("status") == "busy_elsewhere":
        await kv.delete(_claude_key(symbol))
        logger.info("Auto: %s freed by %s — re-reviewing on fresh data", symbol,
                    coin_lock.describe(prior.get("blocked_by") or []))

    cd = await kv.get(_cooldown_key(symbol))
    if cd:
        return {**state, "status": "cooldown", "armed_at": None,
                "reasons": [f"cooling down after exit — {_fmt_minutes(cd.get('until', now) - now)} left"]}

    claude = await kv.get(_claude_key(symbol))
    if claude and claude.get("status") == "approved":
        # Approved for a position/order that no longer exists (wallet reset, file edited) — stale.
        await kv.delete(_claude_key(symbol))
        claude = None
    if claude and claude.get("status") == "pending":
        return {**state, "status": "confirming", "direction": claude.get("direction"),
                "score": prior.get("score"), "reasons": ["asking Claude…"]}
    if claude and claude.get("status") == "rejected":
        return {**state, "status": "vetoed", "direction": claude.get("direction"), "armed_at": None,
                "reasons": [claude.get("note", "Claude declined"),
                            f"won't ask again for {_fmt_minutes(claude['at'] + VETO_SECONDS - now)}"]}

    direction, bias_reasons = strategy.bias(scan)
    if not direction or not fast:
        return {**state, "status": "watching", "direction": None, "reasons": bias_reasons,
                "armed_at": None}
    blocked = strategy.regime_block(direction, await btc_vol_pct())
    if blocked:
        return {**state, "status": "watching", "direction": None, "armed_at": None,
                "reasons": bias_reasons + [blocked]}

    # A flipped bias restarts the arming clock.
    if prior.get("direction") != direction or not state["armed_at"]:
        state["armed_at"] = now
    state["direction"] = direction

    t = strategy.timing(direction, fast, prev, scan)
    state["score"] = t["score"]
    state["reasons"] = bias_reasons + t["reasons"]

    if not t["enter"]:
        state["status"] = "blocked" if t["blocked"] else "armed"
        return state
    if claude and claude.get("status") == "missed":
        state["status"] = "armed"
        state["reasons"].append("recently missed an entry — pausing briefly")
        return state
    if await is_paused():
        state["status"] = "armed"
        state["reasons"].append("auto-trading paused — entry skipped")
        return state
    if not _v().primary and not book_switches.enabled(_v().name):
        state["status"] = "armed"
        state["reasons"].append("switched off on the Books tab — entry skipped")
        return state

    # Guards (trading/guards.py): direction gate and any strategy-review adjustments — before Claude is asked.
    why = guards.entry_block(_v().name, direction, scan.get("conviction"))
    if why:
        if guards.record_skip(_v().name, symbol, direction, why):
            # The skipped entry as a shadow (once an hour per coin): it measures the guard, and its
            # result is what lets the direction gate see that direction working again.
            lv = strategy.build_levels(direction, scan["price"], scan, fast)
            try:
                await asyncio.to_thread(_active().add_virtual, symbol, direction, scan["price"], lv["stop_loss"],
                                        lv["take_profit_1"], lv["take_profit_2"], scan.get("best_timeframe") or "1h",
                                        strategy.RISK_PCT, {"claude_verdict": "gated", "exit_policy": exits.DEFAULT_POLICY})
            except Exception as e:
                logger.debug("Gated shadow for %s not tracked: %s", symbol, e)
        state["status"] = "armed"
        state["reasons"].append(why)
        return state

    return await _request_confirmation(scan, direction, t, state, global_market)


def _last_entry_ts() -> float:
    """When the autotrader last opened a trade or placed an order, in any of its wallets."""
    ts = [t.get("opened_ts") or 0 for w in _wallets() for t in w.all_trades()["closed"] + w.get_portfolio()["open_trades"]]
    ts += [o.get("placed_at") or 0 for w in _wallets() for o in w.get_orders(50)["pending"] + w.get_orders(50)["history"]]
    return max(ts, default=0.0)


def idle_hours(now: float | None = None, max_age_s: float = 120) -> float:
    """Hours since the autotrader last entered anything (cached: it reads every wallet)."""
    now = now or time.time()
    if now - _dry_cache["at"] > max_age_s:
        _dry_cache.update(at=now, idle_h=(now - _last_entry_ts()) / 3600)
    return _dry_cache["idle_h"]


def dry_mode(now: float | None = None) -> dict:
    """Whether the market has been dry for the autotrader long enough to widen the net."""
    if not ENABLED:
        return {"active": False, "idle_h": 0.0, "hours": DRY_HOURS, "universe": None}
    idle = idle_hours(now)
    return {"active": idle >= DRY_HOURS, "idle_h": round(idle, 1), "hours": DRY_HOURS, "universe": DRY_UNIVERSE if idle >= DRY_HOURS else None}


def _daily_min_report() -> dict:
    forced = [t for w in _wallets() for t in w.all_trades()["closed"] if t.get("forced_daily") and t.get("risk_usd")]
    rs = [t["pnl_usd"] / t["risk_usd"] for t in forced]
    return {"enabled": DAILY_MIN_ENABLED, "hours": DAILY_MIN_HOURS, "risk_x": DAILY_MIN_RISK_X, "closed": len(forced),
            "wins": sum(r > 0 for r in rs), "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "net_usd": round(sum(t["pnl_usd"] for t in forced), 2),
            "open": sum(1 for w in _wallets() for t in w.get_portfolio()["open_trades"] if t.get("forced_daily")),
            "last": kv.get_sync(kv.key("auto", "daily_min_last"))}


async def _daily_minimum(by_symbol: dict[str, dict], held: set[str], orders: dict, elsewhere: dict) -> dict | None:
    """At least one trade a day (asked 2026-10-25). If nothing has been entered for DAILY_MIN_HOURS, take the
    strongest setup in the current scans at DAILY_MIN_RISK_X of normal risk, at market — skipping Claude's
    veto, the entry-timing filters and the direction gate, which are what kept it idle. Exits are the normal
    ones (stop, 24h cap, thesis-faded watcher). Tagged forced_daily so its results are measured on their own:
    those filters had mostly been right (their skipped trades lost ~11R in the week to 10-23)."""
    if not DAILY_MIN_ENABLED or await is_paused():
        return None
    now = time.time()
    if now - await asyncio.to_thread(_last_entry_ts) < DAILY_MIN_HOURS * 3600:
        return None
    if not await kv.acquire_once(kv.key("auto", "daily_min_try"), 1800):      # one attempt per 30 min at most
        return None
    note = lambda outcome: kv.set_sync(kv.key("auto", "daily_min_last"), {"at": time.time(), "outcome": outcome}, 7 * 86400)
    idle_h = (now - await asyncio.to_thread(_last_entry_ts)) / 3600
    # The bar steps down the longer it's idle: a flat market can sit below 0.15 for days.
    floor = DAILY_MIN_CONVICTION if idle_h < DAILY_MIN_HOURS + 6 else DAILY_MIN_FLOOR
    pool = [sc for sym, sc in by_symbol.items() if sym not in held and sym not in orders and sym not in elsewhere
            and abs(sc.get("conviction") or 0) >= floor]
    if not pool:
        logger.info("Daily minimum: no coin with |conviction| ≥ %.2f to take (idle %.0fh)", floor, idle_h)
        note(f"no coin with |conviction| ≥ {floor:.2f} among {len(by_symbol)} scanned")
        return None
    from ops import calendar
    blackout = calendar.macro_blackout(now)
    if blackout:
        note(f"held back — {blackout}")
        return None
    scan = max(pool, key=lambda sc: abs(sc["conviction"]))
    direction = "long" if scan["conviction"] > 0 else "short"
    _, fast = strategy.fast_tf(scan)
    price = await _live_price(scan["symbol"], by_symbol)
    if not price or not fast:
        note(f"{scan['symbol']}: no {'price' if not price else 'fast-timeframe data'}")
        return None
    lv = strategy.build_levels(direction, price, scan, fast)
    lane_mult, lane_why = await _lane_mult("ta", scan["symbol"], direction)
    size = strategy.position_size_pct(lv["sl_pct"], _gross_pct()) * guards.risk_mult(_v().name) * DAILY_MIN_RISK_X * lane_mult
    if size <= 0:
        note(f"{scan['symbol']}: no room in the wallet (size {size:.2f}%)")
        return None
    trade = await asyncio.to_thread(
        _active().open_trade, symbol=scan["symbol"], direction=direction, entry_price=price,
        stop_loss=lv["stop_loss"], take_profit_1=lv["take_profit_1"], take_profit_2=lv["take_profit_2"], size_pct=size,
        timeframe=scan.get("best_timeframe") or "1h", oracle_confidence=0,
        oracle_reasoning=f"daily minimum: nothing entered for {DAILY_MIN_HOURS:g}h — strongest setup, {DAILY_MIN_RISK_X:g}× risk",
        hold_duration=scan.get("hold_duration") or "N/A",
        extra={"strategy": "auto", "exit_policy": exits.DEFAULT_POLICY, "entry_type": "market", "forced_daily": True,
               "lane": "ta", "lane_mult": lane_mult, "lane_why": lane_why,
               "conviction_at_entry": scan.get("conviction"), "claude_verdict": "skipped",
               "setups_at_entry": (scan.get("conviction_detail") or {}).get("aligned_setups", []),
               "market_price_at_decision": price, "regime_at_entry": await regime.current(),
               "near_event": calendar.near_event(scan["symbol"], now)})
    if trade.get("error"):
        logger.info("Daily minimum %s %s refused: %s", direction, scan["symbol"], trade["error"])
        note(f"{direction} {scan['symbol']} refused: {trade['error']}")
        return None
    logger.info("Daily minimum: opened %s %s @ %s (conviction %+.2f, %.1f%% size)", direction, scan["symbol"], price,
                scan["conviction"], size)
    note(f"opened {direction} {scan['symbol']} (conviction {scan['conviction']:+.2f})")
    _publish("auto_trade", {"event": "opened", "trade": trade})
    return trade


async def on_scans(scans: list[dict], watched: set[str], global_market: dict | None = None) -> None:
    """Hook called by the scheduler after each sweep, with full (unstripped) scans — once for
    the real autotrader, then once for its rules-only twin on the very same scans."""
    if not ENABLED or not scans:
        return
    for v in _variants():
        token = _variant.set(v)
        try:
            await _on_scans(scans, watched, global_market)
        except Exception as e:
            logger.warning("%s sweep failed: %s", v.name, e)
        finally:
            _variant.reset(token)


def _mirror_key() -> str:
    return kv.key("auto", "event_mirror")


def _unlock_key() -> str:
    return kv.key("auto", "unlock_mirror")


def _lane_history() -> list[dict]:
    """The lanes' record: this ledger's closed trades, plus each mirror lane's signal book's own closes from before the
    lane began (those are the rule's history too; what the lane mirrored since is already in the ledger). Only the
    PaperWallet books (weekly, cascade, factor) have such a history here; the event and unlock books keep their
    positions in their own tables and start their lanes at equal size."""
    from experiments import books
    out = [t for w in _wallets() for t in w.all_trades()["closed"]]
    for lane, book in lanes.LANE_BOOK.items():
        w = books.WALLETS.get(book) if book != "auto" else None
        if w is None:
            continue
        stamp = kv.get_sync(kv.key("auto", f"{lane}_since")) or {}
        since = stamp.get("at") if isinstance(stamp, dict) else stamp
        if not isinstance(since, (int, float)):
            since = float("inf")                                      # the lane never began: the whole book is prehistory
        for t in w.all_trades()["closed"]:
            if (t.get("opened_ts") or 0) < since and t.get("close_reason") not in lanes.ADMIN_CLOSES:
                out.append({**t, "lane": lane, "prehistory": True})
    return out


async def _lane_mult(lane: str, symbol: str | None, direction: str | None = None) -> tuple[float, str]:
    """The lane allocator's size multiplier for a new entry (autotrader/lanes.py), from the record and the regime now."""
    try:
        card = await asyncio.to_thread(lambda: lanes.scorecard(_lane_history()))
        return lanes.multiplier(lane, lanes.regime_key(await regime.current()), symbol, card, direction)
    except Exception as e:
        logger.warning("Lane allocator unavailable (%s) — equal size", e)
        return 1.0, "equal size — allocator unavailable"


def _mirror_leverage(stop_pct: float, symbol: str) -> int:
    """The most leverage whose liquidation still sits beyond a wide disaster stop (3× for a 30% stop, 2× for 40%).
    The wallet's own rule wants liquidation twice the stop distance away, which a 25–40% stop can't meet at all,
    so left to itself every mirrored leg ran at 1× and locked its whole notional as margin."""
    from trading import futures
    return max(1, min(futures.MAX_LEVERAGE, int(0.95 / (stop_pct / 100 + futures.mmr(symbol)))))


def _is_hedge(t: dict) -> bool:
    return t.get("event_leg") == "hedge" or t.get("unlock_leg") == "hedge"


def opposite_holding(symbol: str, direction: str, open_trades: list[dict]) -> dict | None:
    """The open position (a hedge leg doesn't count — it is not a view) on the other side of `symbol`, if any. One
    trade, one system (2026-11-02): the ledger was long WLD in the reversal lane while the weekly book was short it,
    so the two cancelled and paid fees twice. Whoever holds the name first keeps it."""
    other = "short" if direction == "long" else "long"
    return next((t for t in open_trades if t.get("symbol") == symbol and t.get("direction") == other and not _is_hedge(t)), None)


def _holdings_elsewhere(include_books: bool = True) -> list[dict]:
    """What this ledger (and, if asked, the signal books) hold right now — the positions a new leg must not oppose."""
    from experiments import books
    from trading import overview
    out = [t for ww in _wallets() for t in ww.get_portfolio()["open_trades"]]
    for name in overview.SIGNAL_SYSTEMS if include_books else ():
        ww = books.WALLETS.get(name)
        if ww is not None:
            out += [{**t, "system": name} for t in ww.get_portfolio()["open_trades"]]
    return out


async def _open_mirror_leg(w: PaperWallet, symbol: str, direction: str, stop_pct: float, notional_pct: float,
                           reasoning: str, hold: str, expires_at: float | None, extra: dict, risk_pct: float | None = None) -> dict | None:
    """One leg of a mirrored position: market entry, a disaster stop, no targets, the book decides the exit."""
    price = await monitor.live_price(symbol)
    if not price:
        return None
    s = 1 if direction == "long" else -1
    equity = w.get_portfolio()["total_equity"] or 1
    lane = extra.get("lane", "events")
    if not _is_hedge(extra):
        # A mirror of a signal book is refused only by what this ledger already holds (two books on opposite sides of
        # a name would otherwise refuse each other forever); a lane with no book (reversal) also defers to the books.
        own_book = lanes.LANE_BOOK.get(lane, "auto") != "auto"
        opp = opposite_holding(symbol, direction, await asyncio.to_thread(_holdings_elsewhere, not own_book))
        if opp:
            logger.info("%s leg %s %s skipped: %s already holds the other side", lane, direction, symbol, opp.get("system") or lanes.lane_of(opp))
            return None
        waited = await asyncio.to_thread(coin_lock.recent_stop_out, symbol, direction)
        if waited:
            logger.info("%s leg %s %s skipped: %s", lane, direction, symbol, waited)
            return None
        # Not into a move already running against the leg: the leg is tried again on the next tick (the mirror loop
        # re-offers it while the book still holds the position; a reversal pick is simply passed over).
        from trading import tape
        into = tape.against_move(direction, await tape.coin_tape(symbol))
        if into:
            logger.info("%s leg %s %s deferred: %s", lane, direction, symbol, into)
            return None
    lane_mult, lane_why = await _lane_mult(lane, symbol if not _is_hedge(extra) else None, direction)
    notional = min(max(equity * notional_pct / 100, equity * (risk_pct if risk_pct is not None else LANE_RISK_PCT) / max(stop_pct, 0.1)) * lane_mult,
                   equity * LANE_MAX_NOTIONAL_PCT / 100)
    if not _is_hedge(extra):
        capped = lanes.net_cap_reason(direction, notional, [t for ww in _wallets() for t in ww.get_portfolio()["open_trades"]], equity)
        if capped:
            logger.info("%s leg %s %s skipped: %s", lane, direction, symbol, capped)
            return None
    t = await asyncio.to_thread(
        w.open_trade, symbol=symbol, direction=direction, entry_price=price,
        stop_loss=price * (1 - s * stop_pct / 100), take_profit_1=price * (1 + s * 0.9), take_profit_2=price * (1 + s * 0.95),
        notional_usd=notional,
        timeframe="1d", oracle_confidence=0,
        leverage=_mirror_leverage(stop_pct, symbol),
        oracle_reasoning=reasoning[:200], hold_duration=hold,
        extra={"strategy": "auto", "exit_policy": "manual", "shadows": False, "entry_type": "market", "claude_verdict": "skipped",
               "market_price_at_decision": price, "lane_mult": lane_mult, "lane_why": lane_why,
               "regime_at_entry": await regime.current(), **extra})
    if t.get("error"):
        logger.info("%s leg %s %s refused: %s", lane, direction, symbol, t["error"])
        return None
    if expires_at:
        await asyncio.to_thread(w.annotate, t["id"], expires_at=expires_at)        # the book closes it first
    return t


def _pair_leg_pct(lane: str, book_leg_pct: float) -> float:
    """A pair book's leg (LEG_PCT of its own equity) as a % of this ledger's equity: the lane's budget share × LEG_PCT.
    Falls back to EVENT_LEG_PCT when the ledger is empty."""
    equity = _active().get_portfolio()["total_equity"]
    if not equity:
        return EVENT_LEG_PCT
    return _lane_budget(lane, equity) / equity * book_leg_pct


async def _open_event_leg(w: PaperWallet, symbol: str, direction: str, stop_pct: float, p: dict, leg: str) -> dict | None:
    from experiments import events as events_book
    return await _open_mirror_leg(
        w, symbol, direction, stop_pct, _pair_leg_pct("events", events_book.LEG_PCT),
        f"event short ({p['kind']}): {p.get('title') or ''}",
        f"{p['kind']} · to {datetime.fromtimestamp(p['deadline_ts'], timezone.utc):%m-%d %H:%M} UTC", p["deadline_ts"] + 3600,
        {"lane": "events", "event_id": p["id"], "event_kind": p["kind"], "event_leg": leg})


async def _mirror_unlocks() -> None:
    """Mirror the unlock book's pairs on the autotrader's active ledger, as the event mirror does."""
    if not UNLOCKS_IN_AUTO or await is_paused():
        return
    from experiments import books as experiment_books_, unlocks
    if not experiment_books_.ENABLED:
        return
    mirrored = await kv.hgetall(_unlock_key())
    w = _active()
    open_rows = {p["id"]: p for p in unlocks.open_positions()}
    mirrored = await _mark_pre_existing("unlocks", _unlock_key(), open_rows, mirrored)
    held = {t["symbol"]: t for ww in _wallets() for t in ww.get_portfolio()["open_trades"]}
    for uid, p in open_rows.items():
        if uid in mirrored or p["symbol"] in held:
            continue
        when = datetime.fromtimestamp(p["unlock_ts"], timezone.utc).strftime("%m-%d %H:%M")
        coin = await _open_mirror_leg(w, p["symbol"], "short", UNLOCK_STOP_PCT, _pair_leg_pct("unlocks", unlocks.LEG_PCT),
                                      f"unlock short: {p.get('pct') or 0:.1f}% of supply unlocks {when} UTC", f"to the unlock on {when} UTC",
                                      p["unlock_ts"] + 3600, {"lane": "unlocks", "unlock_id": uid, "unlock_leg": "coin"})
        if not coin:
            continue
        hedge = None if unlocks.HEDGE in held else await _open_mirror_leg(
            w, unlocks.HEDGE, "long", UNLOCK_HEDGE_STOP_PCT, _pair_leg_pct("unlocks", unlocks.LEG_PCT), f"BTC hedge for the {p['symbol']} unlock short",
            f"to the unlock on {when} UTC", p["unlock_ts"] + 3600, {"lane": "unlocks", "unlock_id": uid, "unlock_leg": "hedge"})
        await kv.hset(_unlock_key(), uid, {"coin": coin["id"], "hedge": hedge["id"] if hedge else None, "symbol": p["symbol"], "opened": time.time()})
        logger.info("Autotrader mirrored unlock short: %s%s", p["symbol"], "" if hedge else " (no BTC hedge: BTC already held)")
        _publish("auto_trade", {"event": "opened", "trade": coin})
    if not mirrored:
        return
    closed_rows = {p["id"]: p for p in unlocks.closed_positions() if p["id"] in mirrored}
    open_ids = {t["id"]: (ww, t) for ww in _wallets() for t in ww.get_portfolio()["open_trades"]}
    for uid, m in list(mirrored.items()):
        if m.get("pre_existing"):
            if uid not in open_rows:
                await kv.hdel(_unlock_key(), uid)
            continue
        paper_closed = uid in closed_rows
        coin_open, hedge_open = m.get("coin") in open_ids, m.get("hedge") in open_ids
        if paper_closed or (not coin_open and hedge_open):
            reason = f"unlock_{closed_rows[uid]['close_reason']}" if paper_closed else "unlock_hedge_unwound"
            for tid in (m.get("coin"), m.get("hedge")):
                if tid in open_ids:
                    ww, t = open_ids[tid]
                    price = await monitor.live_price(t["symbol"]) or t["current_price"]
                    closed = await asyncio.to_thread(ww.close_trade_manually, tid, price, reason)
                    if closed:
                        await _on_closed(closed)
            if paper_closed or not coin_open:
                await kv.hdel(_unlock_key(), uid)
        elif not coin_open and not hedge_open and uid not in open_rows:
            await kv.hdel(_unlock_key(), uid)
        elif coin_open and m.get("hedge") and not hedge_open and uid in open_rows \
                and unlocks.HEDGE not in {t["symbol"] for _, t in open_ids.values()}:
            # The hedge leg was closed on its own (a stop, a hand close): put it back so the pair stays a pair.
            p = open_rows[uid]
            when = datetime.fromtimestamp(p["unlock_ts"], timezone.utc).strftime("%m-%d %H:%M")
            hedge = await _open_mirror_leg(w, unlocks.HEDGE, "long", UNLOCK_HEDGE_STOP_PCT, _pair_leg_pct("unlocks", unlocks.LEG_PCT),
                                           f"BTC hedge for the {p['symbol']} unlock short (re-hedged)", f"to the unlock on {when} UTC",
                                           p["unlock_ts"] + 3600, {"lane": "unlocks", "unlock_id": uid, "unlock_leg": "hedge"})
            if hedge:
                await kv.hset(_unlock_key(), uid, {**m, "hedge": hedge["id"]})


ENTRY_BUDGET_USD = float(os.getenv("AUTOTRADER_ENTRY_BUDGET_USD", "2000"))   # the scanner and reversal lanes' own budget each (measuring size)


async def _mark_pre_existing(lane: str, key: str, open_rows: dict, mirrored: dict) -> dict:
    """The first time a lane runs (or after its ledger changed), what the book already holds is recorded as pre-existing
    and never mirrored; only positions opened after that are followed."""
    since_key = kv.key("auto", f"{lane}_since")
    if await kv.get(since_key):
        return mirrored
    await kv.set(since_key, {"at": time.time()}, 10 * 365 * 86400)
    for pid, p in open_rows.items():
        if pid not in mirrored:
            await kv.hset(key, pid, {"coin": None, "hedge": None, "symbol": p.get("symbol"), "pre_existing": True, "opened": time.time()})
    return await kv.hgetall(key)


def _lane_budget(lane: str, equity: float) -> float:
    """What a lane may put on, in dollars: an executed book's lane gets that book's plan entry (its budget, capped by the
    ledger); the autotrader's own lanes (scanner, reversal) get ENTRY_BUDGET_USD each. When the ledger is smaller than
    the budgets together (the $5k demo account), every lane is scaled down pro rata."""
    from trading import capital
    plan = capital.plan()
    budgets = {l: (plan.get(lanes.LANE_BOOK[l], 0.0) if lanes.LANE_BOOK[l] != "auto" else ENTRY_BUDGET_USD) for l in lanes.LANES
               if l != "ta" or TA_LANE}                                   # the retired scanner lane reserves nothing
    total = sum(budgets.values()) or 1.0
    mine = budgets.get(lane, 0.0)
    return mine if total <= equity else equity * mine / total


def _stop_pct_of(t: dict) -> float:
    e, st = t.get("entry_price") or 0, t.get("stop_loss") or 0
    return abs(e - st) / e * 100 if e and st else 10.0


async def _mirror_wallet_book(book: str, lane: str, enabled: bool) -> None:
    """Mirror a paper-wallet signal book (cascade, multi-factor) on the active ledger: open what it opens, at the same
    share of the lane's budget that the book used of its own equity; close when it closes, with its reason."""
    if not enabled or await is_paused():
        return
    from experiments import books as experiment_books_
    if not experiment_books_.ENABLED or book not in experiment_books_.WALLETS:
        return
    src = experiment_books_.WALLETS[book]
    sp = src.get_portfolio()
    src_open = {t["id"]: t for t in sp["open_trades"]}
    key = kv.key("auto", f"{lane}_mirror")
    mirrored = await kv.hgetall(key)
    # Only positions the book opens from now on are followed (2026-11-01): what it already held when this lane began — or
    # when the ledger changed — is entered late, at a different price, with its thesis partly spent, and is left alone.
    since_key = kv.key("auto", f"{lane}_since")
    if not await kv.get(since_key):
        await kv.set(since_key, {"at": time.time()}, 10 * 365 * 86400)
        for sid, t in src_open.items():
            if sid not in mirrored:
                await kv.hset(key, sid, {"trade": None, "symbol": t["symbol"], "pre_existing": True, "opened": time.time()})
        mirrored = await kv.hgetall(key)
    w = _active()
    equity = w.get_portfolio()["total_equity"]
    budget = _lane_budget(lane, equity)
    held = {t["symbol"] for ww in _wallets() for t in ww.get_portfolio()["open_trades"]}
    for sid, t in src_open.items():
        if sid in mirrored or t["symbol"] in held:
            continue                                        # already mirrored, or ended on its own (kept as "ended" so it isn't re-opened)
        frac = (t.get("notional_usd") or 0) / (sp["total_equity"] or 1)
        notional_pct = frac * budget / (equity or 1) * 100
        if notional_pct <= 0:
            continue
        ours = await _open_mirror_leg(
            w, t["symbol"], t["direction"], _stop_pct_of(t), notional_pct,
            f"{lanes.LANES[lane]}: {t['direction']} {t['symbol']} — {(t.get('oracle_reasoning') or '')[:120]}",
            t.get("hold_duration") or "while the book holds it", t.get("expires_at"), {"lane": lane, f"{lane}_id": sid})
        if not ours:
            continue
        await kv.hset(key, sid, {"trade": ours["id"], "symbol": t["symbol"], "opened": time.time()})
        logger.info("Autotrader mirrored %s: %s %s", lane, t["direction"], t["symbol"])
        _publish("auto_trade", {"event": "opened", "trade": ours})
    if not mirrored:
        return
    open_ids = {t["id"]: (ww, t) for ww in _wallets() for t in ww.get_portfolio()["open_trades"]}
    src_closed = {t["id"]: t for t in src.all_trades()["closed"][-200:]}
    for sid, m in list(mirrored.items()):
        if sid in src_open:
            if m.get("trade") in open_ids or m.get("ended") or m.get("pre_existing"):
                continue
            await kv.hset(key, sid, {**m, "ended": time.time()})     # ours ended on its own (stop, expiry): not re-opened while the book holds it
            continue
        if m.get("pre_existing"):
            await kv.hdel(key, sid)
            continue
        if m.get("trade") in open_ids:
            ww, t = open_ids[m["trade"]]
            price = await monitor.live_price(t["symbol"]) or t["current_price"]
            reason = f"{lane}_{(src_closed.get(sid) or {}).get('close_reason') or 'closed'}"
            closed = await asyncio.to_thread(ww.close_trade_manually, m["trade"], price, reason)
            if closed:
                await _on_closed(closed)
        await kv.hdel(key, sid)


async def _reversal_lane(scans: list[dict]) -> None:
    """Every 4 hours: score the sweep's coins (autotrader/reversal.py), buy the top REVERSAL_K, sell the bottom REVERSAL_K,
    each at an equal slice of the lane's budget, for REVERSAL_HOLD_H with a disaster stop. No Claude, no chart gates —
    the lane allocator sizes it by its own record."""
    if not REVERSAL_IN_AUTO or await is_paused() or not scans:
        return
    from autotrader import reversal
    now = time.time()
    bucket = int(now // (4 * 3600))
    slot_key = kv.key("auto", "reversal_slot")
    if (await kv.get(slot_key) or {}).get("bucket") == bucket:
        return
    scores = reversal.score(scans)
    if len(scores) < 2 * REVERSAL_K + 2:
        return
    await kv.set(slot_key, {"bucket": bucket, "at": now, "n": len(scores)}, 5 * 3600)
    picks = reversal.picks(scores, REVERSAL_K, REVERSAL_SIDES)
    try:
        from experiments import prop                      # the same picks under Propr's rules, on the prop book's own ledger
        await prop.mirror_reversal(picks, scores, now)
    except Exception as e:
        logger.warning("Prop book reversal mirror failed: %s", e)
    w = _active()
    equity = w.get_portfolio()["total_equity"]
    budget = _lane_budget("reversal", equity)
    all_open = [t for ww in _wallets() for t in ww.get_portfolio()["open_trades"]]
    held = {t["symbol"] for t in all_open}
    lane_open = sum(1 for t in all_open if lanes.lane_of(t) == "reversal")
    notional_pct = budget / max(len(picks), 1) / (equity or 1) * 100
    for sym, direction in picks:
        if sym in held or notional_pct <= 0 or lane_open >= REVERSAL_MAX_OPEN:
            continue
        ours = await _open_mirror_leg(
            w, sym, direction, REVERSAL_STOP_PCT, notional_pct,
            f"reversal score {scores[sym]:+.3f}: {direction} {sym} (rank {'bottom' if direction == 'short' else 'top'} {REVERSAL_K} of {len(scores)})",
            f"{REVERSAL_HOLD_H:g}h", now + REVERSAL_HOLD_H * 3600, {"lane": "reversal", "reversal_id": f"{bucket}:{sym}", "reversal_score": scores[sym]},
            risk_pct=REVERSAL_RISK_PCT)
        if ours:
            lane_open += 1
            logger.info("Autotrader reversal lane: %s %s (score %+.3f)", direction, sym, scores[sym])
            _publish("auto_trade", {"event": "opened", "trade": ours})


async def _lanes_report() -> dict:
    def _ledger() -> tuple[list[dict], list[dict]]:
        return (_lane_history(), [t for w in _wallets() for t in w.get_portfolio()["open_trades"]])
    closed, open_trades = await asyncio.to_thread(_ledger)               # whole-ledger reads: off the event loop
    rep = await asyncio.to_thread(lanes.status, closed, open_trades, await regime.current())
    on = {"ta": TA_LANE, "events": EVENTS_IN_AUTO, "unlocks": UNLOCKS_IN_AUTO, "weekly": WEEKLY_IN_AUTO, "cascade": CASCADE_IN_AUTO,
          "factor": FACTOR_IN_AUTO, "reversal": REVERSAL_IN_AUTO}
    rep["executor"] = EXECUTOR
    for l in rep["lanes"]:
        l["enabled"] = on.get(l["lane"], True)
    return rep


async def _mirror_events() -> None:
    """Mirror the event book's positions on the autotrader's active ledger: open what it opened, close what it closed."""
    if not EVENTS_IN_AUTO or await is_paused():
        return
    from experiments import books as experiment_books_, events
    if not experiment_books_.ENABLED:
        return
    mirrored = await kv.hgetall(_mirror_key())
    w = _active()
    open_rows = {p["id"]: p for p in events.open_positions()}
    mirrored = await _mark_pre_existing("events", _mirror_key(), open_rows, mirrored)
    held = {t["symbol"]: t for ww in _wallets() for t in ww.get_portfolio()["open_trades"]}
    for eid, p in open_rows.items():
        if eid in mirrored or p["symbol"] in held:
            continue
        coin = await _open_event_leg(w, p["symbol"], "short", EVENT_STOP_PCT, p, "coin")
        if not coin:
            continue
        hedge = None if events.HEDGE in held else await _open_event_leg(w, events.HEDGE, "long", EVENT_HEDGE_STOP_PCT, p, "hedge")
        await kv.hset(_mirror_key(), eid, {"coin": coin["id"], "hedge": hedge["id"] if hedge else None, "symbol": p["symbol"],
                                           "kind": p["kind"], "opened": time.time()})
        logger.info("Autotrader mirrored event short: %s %s%s", p["kind"], p["symbol"], "" if hedge else " (no BTC hedge: BTC already held)")
        _publish("auto_trade", {"event": "opened", "trade": coin})
    if not mirrored:
        return
    closed_rows = {p["id"]: p for p in events.closed_positions() if p["id"] in mirrored}
    open_ids = {t["id"]: (ww, t) for ww in _wallets() for t in ww.get_portfolio()["open_trades"]}
    for eid, m in list(mirrored.items()):
        if m.get("pre_existing"):
            if eid not in open_rows:
                await kv.hdel(_mirror_key(), eid)          # the book closed it: forget it
            continue
        paper_closed = eid in closed_rows
        coin_open, hedge_open = m.get("coin") in open_ids, m.get("hedge") in open_ids
        if paper_closed or (not coin_open and hedge_open):
            reason = f"event_{closed_rows[eid]['close_reason']}" if paper_closed else "event_hedge_unwound"
            for tid in (m.get("coin"), m.get("hedge")):
                if tid in open_ids:
                    ww, t = open_ids[tid]
                    price = await monitor.live_price(t["symbol"]) or t["current_price"]
                    closed = await asyncio.to_thread(ww.close_trade_manually, tid, price, reason)
                    if closed:
                        await _on_closed(closed)
            if paper_closed or not coin_open:
                await kv.hdel(_mirror_key(), eid)
        elif not coin_open and not hedge_open and eid not in open_rows:
            await kv.hdel(_mirror_key(), eid)
        elif coin_open and m.get("hedge") and not hedge_open and eid in open_rows \
                and events.HEDGE not in {t["symbol"] for _, t in open_ids.values()}:
            # The hedge leg was closed on its own (a stop, a hand close): put it back so the pair stays a pair.
            hedge = await _open_event_leg(w, events.HEDGE, "long", EVENT_HEDGE_STOP_PCT, open_rows[eid], "hedge")
            if hedge:
                await kv.hset(_mirror_key(), eid, {**m, "hedge": hedge["id"]})


def _events_report() -> dict:
    trades = [t for ww in _wallets() for t in ww.all_trades()["closed"] if t.get("event_id") and t.get("event_leg") == "coin"]
    rs = []
    for t in trades:
        hedge = next((h for ww in _wallets() for h in ww.all_trades()["closed"] if h.get("event_id") == t["event_id"] and h.get("event_leg") == "hedge"), None)
        rs.append(t["pnl_usd"] + (hedge["pnl_usd"] if hedge else 0.0))
    open_n = sum(1 for ww in _wallets() for t in ww.get_portfolio()["open_trades"] if t.get("event_leg") == "coin")
    return {"enabled": EVENTS_IN_AUTO, "leg_pct": EVENT_LEG_PCT, "open": open_n, "closed": len(rs),
            "wins": sum(r > 0 for r in rs), "net_usd": round(sum(rs), 2)}


async def _on_scans(scans: list[dict], watched: set[str], global_market: dict | None) -> None:
    async with _lock:
        by_symbol = {s["symbol"]: s for s in scans}
        await _monitor(by_symbol)
        await _thesis_exits(by_symbol, watched)
        if _v().primary:
            # The evidence lanes come first: the edge is the event, the unlock or the basket, not the chart.
            for name, mirror in (("Event", _mirror_events), ("Unlock", _mirror_unlocks),
                                 ("Weekly", lambda: _mirror_wallet_book("weekly_rs", "weekly", WEEKLY_IN_AUTO)),
                                 ("Cascade", lambda: _mirror_wallet_book("cascade", "cascade", CASCADE_IN_AUTO)),
                                 ("Factor", lambda: _mirror_wallet_book("multi_factor", "factor", FACTOR_IN_AUTO)),
                                 ("Reversal", lambda: _reversal_lane(scans))):
                try:
                    await mirror()
                except Exception as e:
                    logger.warning("%s mirror failed: %s", name, e)

        held = {t["symbol"] for w in _wallets() for t in w.get_portfolio()["open_trades"]}
        orders = {o["symbol"]: o for w in _wallets() for o in w.get_orders()["pending"]}
        states = await kv.hgetall(_state_key())
        try:
            elsewhere = await asyncio.to_thread(coin_lock.held_by_others, "auto")
        except Exception:
            logger.warning("Auto: can't tell which coins other systems hold — no new entries this sweep")
            elsewhere = {s["symbol"]: [{"system": "other systems", "mode": "unreadable"}] for s in scans}

        # In a dry market every scanned coin is a candidate, not only the watchlist.
        dry = await asyncio.to_thread(dry_mode)
        consider = (watched | set(by_symbol) if dry["active"] else watched) if TA_LANE else set()     # lane retired: no candidates
        for scan in scans:
            if scan["symbol"] not in consider:
                continue
            try:
                prior = states.get(scan["symbol"]) or {}
                new_state = await _decide(scan, prior, held, orders, global_market or {}, elsewhere)
                if prior.get("status") == "busy_elsewhere" and new_state.get("status") != "busy_elsewhere":
                    freed_by = coin_lock.describe(prior.get("blocked_by") or [])
                    new_state["reasons"] = [f"{freed_by} exited — re-reviewed on fresh data"] + (new_state.get("reasons") or [])
                await kv.hset(_state_key(), scan["symbol"], new_state)
            except Exception as e:
                logger.warning("Auto-trade decision failed for %s: %s", scan["symbol"], e)

        if _v().primary and TA_LANE:
            try:
                await _daily_minimum(by_symbol, held, orders, elsewhere)
            except Exception as e:
                logger.warning("Daily-minimum entry failed: %s", e)

        # Forget symbols that left the watchlist (or the widened universe).
        stale = [s for s in states if s not in consider]
        if stale:
            await kv.hdel(_state_key(), *stale)

    if _v().primary:
        bus.publish("auto", await summary())


# ── Reporting & manual overrides ────────────────────────────────────────────────

def _downsample(points: list[dict], n: int) -> list[dict]:
    if len(points) <= n:
        return points
    step = len(points) / n
    picked = [points[int(i * step)] for i in range(n - 1)]
    return picked + [points[-1]]


def _is_pullback(o: dict) -> bool:
    return bool(o.get("pullback") or (o.get("plan") or {}).get("pullback"))


def maker_report(wallets: list[PaperWallet] | None = None, pullback_only: bool = False) -> dict:
    """How maker entries are doing against the market entries they replaced (their twins)."""
    wallets = wallets or _wallets() + [RULES_TWIN.wallet] + [experiment_books.WALLETS[b] for b in ("signals_claude", "signals_rules")]
    orders, twins, trades = [], {}, {}
    for w in wallets:
        orders += [o for o in w.get_orders(500)["history"] if o.get("post_only") and (not pullback_only or _is_pullback(o))]
        for r in w.shadow_report()["results"]:
            if r.get("claude_verdict") == "market_twin" and r.get("policy") == exits.DEFAULT_POLICY:
                twins[r.get("maker_order_id")] = r
        for t in w.all_trades()["closed"]:
            if t.get("order_id"):
                trades[t["order_id"]] = t
    filled = [o for o in orders if o.get("status") == "filled"]
    missed = [o for o in orders if o.get("status") != "filled"]
    improve = [(o["market_price_at_decision"] - o["fill_price"]) / o["market_price_at_decision"] * 1e4
               * (1 if o["direction"] == "long" else -1) for o in filled
               if o.get("fill_price") and o.get("market_price_at_decision")]
    pairs = [((trades[o["id"]]["pnl_usd"] / (trades[o["id"]].get("risk_usd") or 1)), twins[o["id"]]["r"])
             for o in filled if o["id"] in trades and o["id"] in twins]
    lost = [twins[o["id"]]["r"] for o in missed if o["id"] in twins]
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    return {"enabled": strategy.MAKER_ENTRIES or strategy.PULLBACK_ENTRIES, "pullback_r": strategy.PULLBACK_R if strategy.PULLBACK_ENTRIES else None,
            "books": [w.label for w in wallets], "placed": len(orders), "filled": len(filled), "missed": len(missed),
            "fill_rate_pct": round(len(filled) / len(orders) * 100, 1) if orders else None,
            "avg_price_improvement_bps": mean(improve),
            "filled_vs_twin": {"trades": len(pairs), "maker_r": mean([a for a, _ in pairs]), "market_twin_r": mean([b for _, b in pairs])},
            "missed_twin_r": {"trades": len(lost), "avg_r": mean(lost)},
            # Every placed order, all-in: the maker book's R (0 for a miss) minus what market entries would have made.
            "net_r_vs_market": round(sum(a - b for a, b in pairs) - sum(lost), 2) if pairs or lost else None,
            "note": "Twins are the market entries the maker orders replaced. Filled: maker R vs twin R is the saving net of "
                    "adverse selection. Missed: the twin's R is the trade that was lost (or dodged)."}


async def summary() -> dict:
    wallet = _active()
    portfolio = await asyncio.to_thread(wallet.get_portfolio, 50, 3000)
    main = await asyncio.to_thread(main_wallet.get_portfolio, 1, 500)
    orders = await asyncio.to_thread(wallet.get_orders, 30)
    portfolio["equity_history"] = _downsample(portfolio["equity_history"], HISTORY_POINTS)

    states = [
        {k: v for k, v in s.items() if k not in ("prev_fast", "prev_at")}
        for s in (await kv.hgetall(_state_key())).values()
    ]
    order = {"in_trade": 0, "order_pending": 1, "confirming": 2, "armed": 3, "vetoed": 4,
             "blocked": 5, "cooldown": 6, "watching": 7}
    states.sort(key=lambda s: (order.get(s.get("status"), 9), -(s.get("score") or 0)))

    closed = portfolio["closed_trades"]
    outcomes = {"win": 0, "loss": 0, "expired": 0, "breakeven": 0, "flip": 0, "left_watchlist": 0,
                "stale": 0, "trailing_stop": 0}
    for t in closed:
        r = t.get("close_reason")
        if r in ("expired", "breakeven", "left_watchlist", "stale", "window_exit"):
            outcomes[r] += 1
        elif r == "signal_flip":
            outcomes["flip"] += 1
        elif r == "trailing_stop":
            outcomes["trailing_stop"] += 1
        elif t["pnl_usd"] > 0:
            outcomes["win"] += 1
        else:
            outcomes["loss"] += 1

    history = orders["history"]
    filled = sum(1 for o in history if o.get("status") == "filled")
    shadow = await asyncio.to_thread(wallet.shadow_report)
    return {
        **portfolio,
        "closed_trades": list(reversed(closed)),
        "outcomes": outcomes,
        "orders": orders["pending"],
        "order_history": history,
        "order_stats": {"filled": filled, "cancelled": len(history) - filled},
        "exit_policies": shadow["policies"],
        "maker": await asyncio.to_thread(maker_report),
        "pullback": await asyncio.to_thread(maker_report, None, True),
        "daily_min": await asyncio.to_thread(_daily_min_report),
        "dry": await asyncio.to_thread(dry_mode),
        "events": await asyncio.to_thread(_events_report),
        "lanes": await _lanes_report(),
        "open_shadows": shadow["open_shadows"],
        "states": states,
        "enabled": ENABLED,
        "paused": await is_paused(),
        "benchmark": {
            "initial_balance": main["initial_balance"],
            "equity_history": _downsample(main["equity_history"], HISTORY_POINTS),
        },
        "mode": "live" if wallet.label.endswith("_live") else "paper",
        "config": {
            "entry_score": strategy.ENTRY_SCORE,
            "min_conviction": strategy.MIN_CONVICTION,
            "risk_pct": strategy.RISK_PCT,
            "tp1_r": strategy.TP1_R,
            "tp2_r": strategy.TP2_R,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "min_claude_confidence": MIN_CLAUDE_CONFIDENCE,
            "veto_seconds": VETO_SECONDS,
            "llm_budget_used": await llm_budget_used(),
            "llm_budget_total": LLM_DAILY_BUDGET,
            "llm_budget_warn": LLM_BUDGET_WARN,
            "max_open_risk_pct": strategy.MAX_OPEN_RISK_PCT,
            "allow_shorts": strategy.ALLOW_SHORTS,
            "max_btc_vol": strategy.MAX_BTC_VOL,
            "maker_entries": strategy.MAKER_ENTRIES,
            "btc_vol": round(_btc_vol["pct"], 1) if _btc_vol["pct"] is not None else None,
            "open_risk_pct": round(portfolio["open_risk_usd"] / (portfolio["total_equity"] or 1) * 100, 2),
        },
        "updated_at": time.time(),
    }


async def close(trade_id: str, reason: str = "manual") -> dict | None:
    """Manual override from the UI (or a named administrative close)."""
    found = live_swing.find_trade("auto", trade_id)
    if not found:
        return None
    wallet, trade = found
    price = await monitor.live_price(trade["symbol"]) or trade["current_price"]
    closed = await asyncio.to_thread(wallet.close_trade_manually, trade_id, price, reason)
    if closed:
        await _on_closed(closed)
        bus.publish("auto", await summary())
    return closed


async def cancel(order_id: str) -> dict | None:
    """Manual order cancel from the UI."""
    order = next((o for w in _wallets() for o in w.get_orders()["pending"] if o["id"] == order_id), None)
    if not order:
        return None
    async with _lock:
        await _cancel_order(order, "manual")
    bus.publish("auto", await summary())
    return order


