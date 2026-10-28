"""In-process pub/sub fanout backing the SSE endpoint."""

import asyncio
import logging
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("scanner.bus")

# Bounded so a browser tab that stops reading can't grow a queue without limit.
QUEUE_SIZE = 32

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None

# Pushed to every queue on shutdown; the SSE loop ends its response when it sees this.
CLOSE = {"type": "__close__", "data": {}}


def subscriber_count() -> int:
    return len(_subscribers)


def publish(event_type: str, data: dict) -> None:
    """Fan out to every subscriber without ever blocking the scheduler.

    A subscriber that isn't draining its queue loses the oldest event rather than
    applying backpressure — a stalled tab must not slow down the scan loop.
    """
    if not _subscribers:
        return

    event = {"type": event_type, "data": data}
    for queue in list(_subscribers):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("Dropped %s event for a saturated subscriber", event_type)


@contextmanager
def subscribe() -> Iterator[asyncio.Queue]:
    """Hand out the queue itself rather than an async generator.

    The SSE loop wraps each read in asyncio.wait_for to emit keepalives. Cancelling a
    wait on an async generator's __anext__ closes the generator, which would silently
    kill the subscription on the first keepalive — the client would stay connected and
    never receive another event. Cancelling Queue.get() is safe, so the reader owns the
    timeout and the queue outlives it.
    """
    global _loop
    _loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    _subscribers.add(queue)
    logger.info("SSE subscriber connected (%d active)", len(_subscribers))
    try:
        yield queue
    finally:
        _subscribers.discard(queue)
        logger.info("SSE subscriber disconnected (%d active)", len(_subscribers))


def close_all() -> None:
    """End every open stream. Safe to call from a signal handler.

    uvicorn's graceful shutdown (and so every --reload) waits for open connections to
    finish, and an SSE response never finishes on its own — without this, one browser
    tab left open holds a reload at "Waiting for connections to close" indefinitely.
    """
    if not _subscribers or _loop is None or _loop.is_closed():
        return

    def _push() -> None:
        for queue in list(_subscribers):
            while queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(CLOSE)

    _loop.call_soon_threadsafe(_push)
