"""Market recorder — keeps the research archive growing after the scalpers were retired.

Listens to the futures feed for the scalper symbols and writes, to the research market DB
(scalper_market.db): 1-second bars (mid OHLC, spread, depth imbalance, aggressor volumes,
trade count, VWAP) and every liquidation on the exchange. research/scalper_lab.py and the
other labs read that archive. It never trades: the engine base runs with a paused book and
no strategy.

History: the order-flow scalper (retired 2026-09-29) and then the impulse scalper (retired
2026-10-06) ran on this engine and recorded these bars as they traded; this is the part kept.
"""
from __future__ import annotations

import logging
import os

from scalper import config as C
from scalper.book import Book
from scalper.engine import ScalperEngine
from scalper.store import Store

logger = logging.getLogger("scalper.recorder")

ENABLED = os.getenv("RECORDER_ENABLED", "true").lower() != "false"


class MarketRecorder(ScalperEngine):
    def __init__(self, store: Store | None = None):
        super().__init__(store or Store(C.RECORDER_DB, C.MARKET_DB))

    def _make_book(self) -> Book:
        book = Book(C.INITIAL_BALANCE)
        book.paused = True                     # no strategy, and nothing could ever enter
        return book

    def _restore_positions(self) -> None:
        return None                            # it never holds anything

    async def _evaluate(self, now: float) -> None:
        return None

    def status(self) -> dict:
        h = self.health_report()
        return {"recording": True, "symbols": C.SYMBOLS, "history_s": h["history_s"], "feeds": h["feeds"],
                "db_bytes": h["db_bytes"], "store_write_errors": h["store_write_errors"], "uptime_s": h["uptime_s"]}


engine: MarketRecorder | None = None


async def start() -> None:
    global engine
    if not ENABLED or engine is not None:
        return
    engine = MarketRecorder()
    await engine.start()
    logger.info("Market recorder writing 1s bars and liquidations for %d coins", len(C.SYMBOLS))


async def stop() -> None:
    global engine
    if engine is not None:
        await engine.stop()
        engine = None
