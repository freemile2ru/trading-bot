"""Binance USDⓈ-M websocket feeds — order book on /public; trades, mark price and every
symbol's forced liquidations on /market.

Two long-lived connections with exponential-backoff reconnects. Every message is handed
to `on_message(stream_kind, symbol, data)`; health counters are kept per connection.
"""

import asyncio
import json
import logging
import time

import websockets

from scalper import config as C

logger = logging.getLogger("scalper.feed")


class FeedHealth:
    def __init__(self, name: str):
        self.name = name
        self.connected = False
        self.reconnects = 0
        self.messages = 0
        self.last_msg = 0.0
        self.connected_since: float | None = None
        self.last_error: str | None = None
        self._window: list[tuple[float, int]] = []

    def tick(self) -> None:
        self.messages += 1
        self.last_msg = time.time()

    def rate(self) -> float:
        now = time.time()
        self._window.append((now, self.messages))
        self._window = [w for w in self._window if now - w[0] <= 60]
        t0, m0 = self._window[0]
        return (self.messages - m0) / (now - t0) if now > t0 else 0.0

    def as_dict(self) -> dict:
        return {"connected": self.connected, "reconnects": self.reconnects, "messages": self.messages,
                "msgs_per_s": round(self.rate(), 1),
                "last_msg_age_s": round(time.time() - self.last_msg, 2) if self.last_msg else None,
                "connected_since": self.connected_since, "last_error": self.last_error}


def _url(base: str, streams: list[str]) -> str:
    return f"{base}?streams={'/'.join(streams)}"


async def _run(name: str, url: str, health: FeedHealth, on_message) -> None:
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, open_timeout=15, ping_interval=20, ping_timeout=20,
                                          max_queue=4096) as ws:
                health.connected, health.connected_since, health.last_error = True, time.time(), None
                backoff = 1.0
                logger.info("Scalper feed %s connected", name)
                async for raw in ws:
                    health.tick()
                    msg = json.loads(raw)
                    stream, data = msg.get("stream", ""), msg.get("data") or {}
                    symbol, _, kind = stream.partition("@")
                    try:
                        on_message(kind, symbol.upper(), data)
                    except Exception as e:     # a bad message must never kill the feed
                        logger.warning("Scalper handler error on %s: %s", stream, e)
        except asyncio.CancelledError:
            health.connected = False
            raise
        except Exception as e:
            health.connected = False
            health.reconnects += 1
            health.last_error = f"{type(e).__name__}: {e}"
            logger.warning("Scalper feed %s dropped (%s) — reconnecting in %.0fs", name, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def start(symbols: list[str], on_message, ws_public: str = C.WS_PUBLIC,
          ws_market: str = C.WS_MARKET) -> tuple[list[asyncio.Task], dict[str, FeedHealth]]:
    """Mainnet by default. When the scalper trades live on the testnet it must see the testnet's
    own book and trades — its prices differ from mainnet, and post-only orders priced off
    mainnet quotes kept crossing the testnet book and being rejected."""
    lower = [s.lower() for s in symbols]
    health = {"public": FeedHealth("public"), "market": FeedHealth("market")}
    tasks = [
        asyncio.create_task(_run("public", _url(ws_public, [f"{s}@depth5@100ms" for s in lower]),
                                 health["public"], on_message), name="scalper-feed-public"),
        asyncio.create_task(_run("market", _url(ws_market, [f"{s}@aggTrade" for s in lower]
                                              + [f"{s}@markPrice@1s" for s in lower]
                                              + ["!forceOrder@arr"]),
                                 health["market"], on_message), name="scalper-feed-market"),
    ]
    return tasks, health
