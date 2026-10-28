"""Walks a wallet's open positions and resting orders along the perpetual's real price path.

Shared by both wallets. Each call:
  1. fetches the 1-minute perpetual bars since each item was last checked, plus the live
     perpetual price for the still-forming minute, and replays them through the wallet
     (fills, stops, targets, liquidations, expiries — see PaperWallet.process_paths);
  2. charges any funding settlements that fell inside each position's life.

A once-a-minute price sample would miss a wick through a stop, target, limit or
liquidation between samples; the bars don't.
"""

import asyncio
import logging
import time

from data import cache, feeds
from data.binance_client import fetch_funding_history, fetch_futures_minute_bars
from trading import futures
from trading.paper_trader import MINUTE_MS, PaperWallet

logger = logging.getLogger("trading.monitor")

MAX_PATH_MINUTES = 1000     # one klines page; after a long outage older bars are skipped
FUNDING_CHECK_SECONDS = 600


async def live_price(symbol: str) -> float | None:
    try:
        return await feeds.cached_futures_price(symbol)
    except Exception as e:
        logger.debug("No perpetual price for %s: %s", symbol, e)
        return None


async def _apply_funding(wallet: PaperWallet) -> None:
    if not futures.FUNDING_ENABLED:
        return
    now_ms = int(time.time() * 1000)
    events: dict[str, list[dict]] = {}
    for symbol, through in wallet.funding_windows().items():
        # Settlements happen every 8h; checking every few minutes is plenty.
        if not await cache.acquire_once(cache.key("funding_chk", wallet.label, symbol), FUNDING_CHECK_SECONDS):
            continue
        try:
            rows = await fetch_funding_history(symbol, through + 1)
        except Exception as e:
            logger.debug("Funding history unavailable for %s: %s", symbol, e)
            continue
        price = await live_price(symbol)
        events[symbol] = [
            {"time": r["time"], "rate": r["rate"], "mark": r["mark"] or price}
            for r in rows if r["time"] <= now_ms and (r["mark"] or price)
        ]
    if events:
        charged = await asyncio.to_thread(wallet.apply_funding, events)
        if charged:
            logger.info("Funding settled on the %s wallet: $%.4f", wallet.label, charged)


async def run(wallet: PaperWallet) -> list[dict]:
    """Process one monitoring pass. Returns the wallet's events (filled/closed/order_expired)."""
    items = wallet.get_portfolio()["open_trades"] + wallet.get_orders()["pending"]
    shadow_starts = wallet.shadow_starts()
    if not items and not shadow_starts:
        return []

    now_ms = int(time.time() * 1000)
    current_minute = now_ms // MINUTE_MS * MINUTE_MS
    starts: dict[str, int] = {}
    for it in items:
        start = it.get("checked_from_ms")
        if start is None:   # market trade that hasn't been walked yet
            from datetime import datetime
            opened = datetime.fromisoformat(it["opened_at"]).timestamp()
            start = (int(opened * 1000) // MINUTE_MS + 1) * MINUTE_MS
        starts[it["symbol"]] = min(starts.get(it["symbol"], start), start)
    # Shadows keep running after their real trade closes, so they need bars too.
    for symbol, start in shadow_starts.items():
        starts[symbol] = min(starts.get(symbol, start), start)

    paths: dict[str, dict] = {}
    for symbol, start in starts.items():
        start = max(start, current_minute - MAX_PATH_MINUTES * MINUTE_MS)
        bars: list[dict] = []
        if start < current_minute:
            try:
                bars = await fetch_futures_minute_bars(symbol, start)
            except Exception as e:
                from data.http import err_text
                logger.warning("Perpetual bars unavailable for %s (%s) — live price only", symbol, err_text(e))
        paths[symbol] = {
            # The forming minute is covered by the live price; it's replayed once closed.
            "bars": [b for b in bars if b["open_time"] < current_minute],
            "price": await live_price(symbol),
        }

    events = await asyncio.to_thread(wallet.process_paths, paths)
    await _apply_funding(wallet)
    # Close trades whose thesis has faded (crypto swing books only — see trading/thesis_exit.py).
    from trading import thesis_exit
    events += await thesis_exit.check(wallet)
    return events
