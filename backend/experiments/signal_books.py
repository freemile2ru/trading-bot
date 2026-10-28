"""Paired paper books for the scanner's signals: what does Claude's confirmation add?

  signals_rules   every candidate the scanner's rules would send to Claude — traded whatever
                  Claude says (or whether it's asked at all: no budget, no cooldown of Claude's),
                  in the direction of the scan's conviction, with the autotrader's ATR levels
  signals_claude  only the signals Claude actually fires — its direction and its levels

Both enter the same way (a pullback limit since 2026-10-19), risk the same % of equity, and exit under the same default policy, so
the only difference is Claude's judgment (which trades, which side, where the stop sits).
"""
import asyncio
import logging
import time

from autotrader import strategy
from experiments import books
from scanner.escalation import COOLDOWN_SECONDS
from storage import state as kv
from trading import book_switches, exits, guards, monitor, regime

logger = logging.getLogger("experiments.signals")


def _extra(source: str, scan_or_signal: dict) -> dict:
    return {"strategy": source, "exit_policy": exits.DEFAULT_POLICY,
            "conviction_at_entry": scan_or_signal.get("conviction"), "experiment": source}


async def _open(book: str, symbol: str, direction: str, price: float, stop: float, tp1: float, tp2: float,
                timeframe: str, confidence: int, reasoning: str, hold: str, extra: dict) -> dict | None:
    wallet = books.WALLETS[book]
    risk = books.RISK_PCT * guards.risk_mult(book)
    p = wallet.get_portfolio(closed_limit=1, history_limit=1)
    open_risk = p["open_risk_usd"] / (p["total_equity"] or 1) * 100
    why = guards.entry_block(book, direction, extra.get("conviction_at_entry"), open_risk, risk)
    if why:
        # Kept out — but tracked as a shadow, so whether the guard helped is measured.
        guards.record_skip(book, symbol, direction, why)
        await asyncio.to_thread(wallet.add_virtual, symbol, direction, price, stop, tp1, tp2, timeframe or "1h",
                                risk, {"claude_verdict": "gated", "exit_policy": exits.DEFAULT_POLICY, "gate": why})
        logger.info("%s: %s %s skipped — %s", book, direction, symbol, why)
        return None
    extra = {**extra, "regime_at_entry": await regime.current()}
    if strategy.PULLBACK_ENTRIES:
        return await _rest_pullback(wallet, book, symbol, direction, price, stop, tp1, tp2, timeframe, confidence,
                                    reasoning, hold, risk, extra)
    trade = await asyncio.to_thread(
        wallet.open_trade, symbol=symbol, direction=direction, entry_price=price, stop_loss=stop,
        take_profit_1=tp1, take_profit_2=tp2, risk_pct=risk, timeframe=timeframe or "1h",
        oracle_confidence=confidence or 0, oracle_reasoning=reasoning, hold_duration=hold or "N/A", extra=extra)
    if trade.get("error"):
        logger.info("%s: %s %s not taken — %s", book, direction, symbol, trade["error"])
        return None
    logger.info("%s: opened %s %s @ %s (stop %s)", book, direction, symbol, trade["entry_price"], stop)
    return trade


async def _rest_pullback(wallet, book, symbol, direction, price, stop, tp1, tp2, timeframe, confidence, reasoning,
                         hold, risk, extra) -> dict | None:
    """Enter with a post-only limit PULLBACK_R of the stop distance back from the signal price, stop and
    targets unchanged (autotrader/strategy.py, PULLBACK_ENTRIES). Its market twin — the entry it replaced —
    goes in the shadow book, so the maker report measures the difference live."""
    limit = strategy.pullback_limit(direction, price, stop)
    extra = {**extra, "exit_policy": exits.DEFAULT_POLICY, "entry_type": "pullback", "market_price_at_decision": price}
    order = await asyncio.to_thread(
        wallet.place_order, symbol=symbol, direction=direction, limit_price=limit, stop_loss=stop,
        take_profit_1=tp1, take_profit_2=tp2, size_pct=None, risk_pct=risk,
        expires_at=time.time() + strategy.PULLBACK_TTL_S,
        trade_fields={"timeframe": timeframe or "1h", "oracle_confidence": confidence or 0,
                      "oracle_reasoning": reasoning, "hold_duration": hold or "N/A", "extra": extra},
        extra={"post_only": True, "market_price_at_decision": price, "pullback": True})
    if order.get("error"):
        logger.info("%s: %s %s not placed — %s", book, direction, symbol, order["error"])
        return None
    await asyncio.to_thread(wallet.add_virtual, symbol, direction, price, stop, tp1, tp2, timeframe or "1h", risk,
                            {"claude_verdict": "market_twin", "maker_order_id": order["id"], "exit_policy": exits.DEFAULT_POLICY})
    logger.info("%s: pullback limit %s %s @ %s (signal %s, stop %s)", book, direction, symbol, limit, price, stop)
    return order


async def on_candidate(scan: dict) -> dict | None:
    """The rules said "worth asking Claude" — the rules-only book just takes it."""
    if not books.ENABLED or not book_switches.enabled("signals_rules"):
        return None
    symbol = scan["symbol"]
    # Same per-coin rhythm as the Claude escalations it's paired with.
    if not await kv.acquire_once(kv.key("signals_rules", "cd", symbol), COOLDOWN_SECONDS):
        return None
    direction = "long" if scan.get("conviction", 0) > 0 else "short"
    _, fast = strategy.fast_tf(scan)
    price = await monitor.live_price(symbol) or scan["price"]
    lv = strategy.build_levels(direction, price, scan, fast)
    return await _open("signals_rules", symbol, direction, price, lv["stop_loss"], lv["take_profit_1"],
                       lv["take_profit_2"], scan.get("best_timeframe"), scan.get("confidence"),
                       "rules-qualified scanner candidate (Claude not consulted)", scan.get("hold_duration"),
                       _extra("signals_rules", scan))


async def on_fired(signal: dict) -> dict | None:
    """Claude fired a signal — the Claude book takes exactly that, at its levels."""
    if not books.ENABLED or not book_switches.enabled("signals_claude") or not signal.get("stop_loss"):
        return None
    direction = "long" if signal["action"] == "BUY" else "short"
    price = await monitor.live_price(signal["symbol"]) or signal["entry_price"]
    tp2 = signal.get("take_profit_2") or signal.get("take_profit_1")
    if not tp2:
        return None
    return await _open("signals_claude", signal["symbol"], direction, price, signal["stop_loss"],
                       signal.get("take_profit_1") or tp2, tp2, signal.get("timeframe"), signal.get("confidence"),
                       signal.get("thesis", ""), signal.get("hold_duration"), _extra("signals_claude", signal))
