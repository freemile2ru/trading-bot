"""One coin, one system: who is in a trade on each coin right now, across every system and mode.

The autotrader doesn't enter a coin another system already holds —
two systems on one coin double the exposure, and on a shared Binance account they net into a
single position that neither ledger describes. A coin skipped this way is re-reviewed on fresh
data once the holder exits (see autotrader.engine and scalper.engine), never replayed from the
stale signal. Manual trades (the main wallet) are the user's call and are not blocked.

Reads wallets from the database, so call it from a thread, not the event loop.

Stop-out cooldown (2026-11-06): when any ledger's stop closes a trade, no automated book re-enters that coin on
that side for STOP_OUT_COOLDOWN seconds. ZEC, 2026-11-06 14:40 UTC: the reversal lane's short was stopped out by a
5% spike in ten minutes and two minutes later the copy book shorted the same coin at the spike's top, mirroring a
leader — two ledgers, no shared memory of what had just happened. The other side stays open: a stop-out is news
about the move, not a ban on the coin.
"""
from __future__ import annotations

import logging
import os
import time

from storage import state as kv

logger = logging.getLogger(__name__)

STOP_OUT_COOLDOWN_S = int(os.getenv("STOP_OUT_COOLDOWN", "3600"))

NAMES = {"main": "Main", "auto": "Auto"}


def _swing(system: str) -> list[tuple[str, dict]]:
    from live import swing as live_swing
    out = []
    for w in live_swing.wallets(system):
        mode = "live" if w.label.endswith("_live") else "paper"
        for t in w.get_portfolio(closed_limit=1, history_limit=1)["open_trades"]:
            out.append((t["symbol"], {"system": system, "mode": mode, "direction": t["direction"],
                                      "entry": t["entry_price"], "pnl_usd": t.get("pnl_usd"),
                                      "opened_at": t.get("opened_at")}))
        for o in w.get_orders(history_limit=1)["pending"]:
            out.append((o["symbol"], {"system": system, "mode": mode, "direction": o["direction"],
                                      "entry": o["limit_price"], "pending": True}))
    return out


def holders(systems=("main", "auto")) -> dict[str, list[dict]]:
    """{symbol: [holder, ...]} for the given systems — open positions and resting entry orders."""
    out: dict[str, list[dict]] = {}
    for system in systems:
        try:
            rows = _swing(system)
        except Exception as e:                  # one unreadable system must not unblock the others silently
            logger.warning("Coin lock: couldn't read %s positions (%s)", system, e)
            raise
        for sym, h in rows:
            out.setdefault(sym, []).append(h)
    return out


def held_by_others(system: str) -> dict[str, list[dict]]:
    return holders(tuple(s for s in NAMES if s != system))


def describe(hs: list[dict]) -> str:
    return ", ".join(f"{NAMES.get(h['system'], h['system'])} ({h['mode']}{', order' if h.get('pending') else ''})" for h in hs)


# ── Stop-out cooldown ─────────────────────────────────────────────────────────

def _stop_key(symbol: str, direction: str) -> str:
    return kv.key("stop_out", symbol, direction)


def record_stop_out(book: str, trade: dict) -> None:
    """Called by every wallet as it books a close: a stop-loss close starts the cooldown for that coin and side."""
    if trade.get("close_reason") != "stop_loss" or STOP_OUT_COOLDOWN_S <= 0:
        return
    try:
        kv.set_sync(_stop_key(trade["symbol"], trade["direction"]),
                    {"book": book, "at": time.time(), "price": trade.get("current_price"), "pnl_usd": trade.get("pnl_usd")},
                    STOP_OUT_COOLDOWN_S)
    except Exception as e:                      # a close is never undone by its bookkeeping
        logger.warning("Stop-out cooldown for %s not recorded (%s)", trade.get("symbol"), e)


def recent_stop_out(symbol: str, direction: str) -> str | None:
    """Why a new `direction` entry on `symbol` should wait — or None when no stop closed that side recently."""
    rec = kv.get_sync(_stop_key(symbol, direction))
    if not rec:
        return None
    ago = max(0.0, time.time() - float(rec.get("at") or 0)) / 60
    return (f"{symbol} {direction} was stopped out on {rec.get('book')} {ago:.0f} min ago — "
            f"no {direction} re-entry for {STOP_OUT_COOLDOWN_S // 60} min")
