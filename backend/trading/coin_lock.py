"""One coin, one system: who is in a trade on each coin right now, across every system and mode.

The autotrader doesn't enter a coin another system already holds —
two systems on one coin double the exposure, and on a shared Binance account they net into a
single position that neither ledger describes. A coin skipped this way is re-reviewed on fresh
data once the holder exits (see autotrader.engine and scalper.engine), never replayed from the
stale signal. Manual trades (the main wallet) are the user's call and are not blocked.

Reads wallets from the database, so call it from a thread, not the event loop.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

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
