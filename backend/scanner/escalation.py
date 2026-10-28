"""The paid tier — decides which scans are worth a Claude Opus call.

Running the full oracle on ten coins every five minutes would be ~2,900 Opus calls a
day. Everything here exists to keep that number in the tens: a quality bar, a
per-symbol cooldown, and a hard daily ceiling that survives restarts because it lives
in Redis rather than in process memory.
"""

import logging
import os
import time
from datetime import datetime, timezone

from storage import signals as signal_log
from storage import state
from oracle.claude_oracle import synthesize_trade
from oracle.signal_history import save_signal

logger = logging.getLogger("scanner.escalation")

MIN_CONVICTION = float(os.getenv("SCANNER_MIN_CONVICTION", "0.45"))
MIN_CONFIDENCE = int(os.getenv("SCANNER_MIN_CONFIDENCE", "65"))
COOLDOWN_SECONDS = int(os.getenv("SCANNER_LLM_COOLDOWN", "3600"))
DAILY_BUDGET = int(os.getenv("SCANNER_LLM_DAILY_BUDGET", "50"))
SIGNAL_LIVE_SECONDS = 4 * 3600   # how long a fired signal stays tradeable (matches the UI)


FEED_SIZE = 20
_budget_warned: str | None = None
FEED_TTL = 86400


def _budget_key() -> str:
    return state.key("llm_budget", datetime.now(timezone.utc).strftime("%Y-%m-%d"))


async def budget_used() -> int:
    return await state.counter(_budget_key())


async def recent_fired() -> list[dict]:
    """Signals fired in the last day, newest first (from signals.db, where every fired
    signal is kept permanently — the feed just reads the last 24h of it)."""
    return signal_log.recent_fired(FEED_TTL, FEED_SIZE)


async def _record_fired(signal: dict) -> list[dict]:
    signal_log.add_fired(signal)
    return await recent_fired()


def qualifies(scan: dict) -> tuple[bool, str]:
    """Quality bar, evaluated before any Redis or network call."""
    conviction = abs(scan.get("conviction", 0.0))
    if conviction < MIN_CONVICTION:
        return False, f"conviction {conviction:.2f} < {MIN_CONVICTION}"

    if scan.get("confidence", 0) < MIN_CONFIDENCE:
        return False, f"confidence {scan.get('confidence')}% < {MIN_CONFIDENCE}%"

    if not scan.get("conviction_detail", {}).get("aligned_setups"):
        return False, "no named setup fired in the signal's direction"

    multiplier = (scan.get("tf_alignment") or {}).get("multiplier", 1.0)
    if multiplier < 1.0:
        return False, f"timeframes disagree (alignment {multiplier:.2f})"

    # Late entries: price already far past the 15m EMA. On the replay these lost money
    # while near-EMA signals made +1.8R — don't spend a Claude call confirming a chase.
    from autotrader.strategy import LATE_ATR, extension_atr
    fast = (scan.get("signal_package") or {}).get("ta_summary", {}).get("timeframe_signals", {}).get("15m") or {}
    ext = extension_atr(fast, "long" if scan.get("conviction", 0) > 0 else "short")
    if ext is not None and ext >= LATE_ATR:
        return False, f"late — {ext:.1f} ATR beyond the 15m EMA"

    return True, "qualified"


async def try_escalate(scan: dict, global_market: dict) -> dict | None:
    """Run the Claude oracle if this scan earns it. Returns a fired signal or None."""
    symbol = scan["symbol"]

    ok, reason = qualifies(scan)
    if not ok:
        logger.debug("No escalation for %s — %s", symbol, reason)
        return None

    # A live signal in the same direction already says this — re-asking Claude every
    # cooldown just produces duplicate cards and burns budget. An opposite-direction
    # setup still goes through (after the normal cooldown), since that's new information.
    action = "BUY" if scan.get("conviction", 0) > 0 else "SELL"
    cutoff = time.time() - SIGNAL_LIVE_SECONDS
    live = next((f for f in await recent_fired()
                 if f["symbol"] == symbol and f.get("fired_at", 0) >= cutoff), None)
    if live and live.get("action") == action:
        logger.debug("No escalation for %s — live %s signal from %dm ago", symbol, action,
                     (time.time() - live["fired_at"]) / 60)
        return None

    # Paired experiment: the rules-only book takes every candidate that gets this far —
    # before (and regardless of) Claude's budget, cooldown or answer.
    await _experiment("on_candidate", scan)

    used = await budget_used()
    if used >= DAILY_BUDGET:
        global _budget_warned
        day = _budget_key()
        if _budget_warned != day:        # once per day, not on every sweep
            _budget_warned = day
            logger.warning("Daily Claude budget exhausted (%d/%d) — no more escalations until the "
                           "next UTC day", used, DAILY_BUDGET)
        else:
            logger.debug("Budget exhausted — skipping %s", symbol)
        return None

    # Atomic claim: concurrent sweeps can't both fire the same symbol.
    if not await state.acquire_once(state.key("llm_cd", symbol), COOLDOWN_SECONDS):
        logger.debug("Cooldown active for %s — skipping Claude call", symbol)
        return None

    count = await state.incr_with_expire(_budget_key(), 172800)
    logger.info(
        "Escalating %s to Claude — conviction=%.2f confidence=%d%% setups=%s (budget %d/%d)",
        symbol, scan["conviction"], scan["confidence"],
        ",".join(scan["conviction_detail"]["aligned_setups"]), count, DAILY_BUDGET,
    )

    oracle_result = await synthesize_trade(scan["signal_package"], global_market)

    if oracle_result.get("error"):
        logger.error("Claude call failed for %s — %s", symbol, oracle_result["error"])
        # Release the cooldown so a failed call doesn't block the symbol for an hour.
        await state.delete(state.key("llm_cd", symbol))
        return None

    rec = oracle_result.get("trade_recommendation", {})
    action = rec.get("action", "HOLD")
    if action == "HOLD":
        logger.info("Claude returned HOLD for %s — not emitting a signal", symbol)
        return None

    fired = {
        "symbol": symbol,
        "base": scan["base"],
        "action": action,
        "confidence": rec.get("confidence", scan["confidence"]),
        "conviction": scan["conviction"],
        "entry_price": rec.get("entry_price") or scan["price"],
        "stop_loss": rec.get("stop_loss"),
        "take_profit_1": rec.get("take_profit_1"),
        "take_profit_2": rec.get("take_profit_2"),
        "hold_duration": rec.get("hold_duration") or scan["hold_duration"],
        "timeframe": rec.get("timeframe") or scan["best_timeframe"],
        "setups": scan["setups"],
        "fa_label": (scan.get("fa") or {}).get("label"),
        "regime": scan.get("regime"),
        "thesis": (oracle_result.get("reasoning") or {}).get("primary_thesis", ""),
        "risk_factors": (oracle_result.get("reasoning") or {}).get("risk_factors", []),
        "alerts": oracle_result.get("alerts", []),
        "fired_at": time.time(),
        "oracle": oracle_result,
    }

    try:
        save_signal({
            "symbol": symbol,
            "signal_package": scan["signal_package"],
            "oracle": oracle_result,
            "source": "scanner",
        })
    except Exception as e:
        logger.warning("Could not persist scanner signal history: %s", e)

    await _record_fired(fired)
    await _experiment("on_fired", fired)
    return fired


async def _experiment(hook: str, arg: dict) -> None:
    try:
        from experiments import signal_books
        await getattr(signal_books, hook)(arg)
    except Exception as e:                    # an experiment must never break the live signal path
        logger.warning("Signal experiment %s failed: %s", hook, e)
