"""Data watchdog — notices when the app stops getting market data, and says so.

Why: from 2026-10-14 05:41 to 10-17 15:46 UTC the server kept running but got no prices or scans
(most likely the Mac lost its network). Every wallet sat frozen and nobody knew for 3.5 days.

Every CHECK_S it probes the BTC perpetual price and reads the scanner's last sweep. When either has
been missing for STALE_S it raises an outage:
  * a macOS notification (local — works with no network at all)
  * an SMS through Twilio (if configured; fails quietly while the network is down)
  * a banner in the app (GET /api/health)
and when data is back, a "recovered after …" notification and SMS. A gap between its own checks
(the machine was asleep, so nothing ran) is recorded the same way. Outages are kept so experiment
results can be read with them in mind.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from storage import state as kv

logger = logging.getLogger("ops.watchdog")

ENABLED = os.getenv("WATCHDOG_ENABLED", "true").lower() != "false"
CHECK_S = 60
STALE_S = int(os.getenv("WATCHDOG_STALE_MINUTES", "30")) * 60
SMS = os.getenv("WATCHDOG_SMS", "true").lower() != "false"
KEEP = 20
_KEY = kv.key("ops", "watchdog")
_mem = {"last_check": None, "last_price_ok": None}


def _state() -> dict:
    return kv.get_sync(_KEY) or {"status": "ok", "since": None, "reason": None, "outages": []}


def _save(s: dict) -> None:
    kv.set_sync(_KEY, s)


def _fmt(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


async def _notify(title: str, body: str) -> None:
    logger.warning("%s — %s", title, body)
    if sys.platform == "darwin":
        q = lambda x: '"' + x.replace("\\", "\\\\").replace('"', '\\"') + '"'      # an AppleScript string
        script = f'display notification {q(body)} with title {q(title)} sound name "Basso"'
        try:
            proc = await asyncio.create_subprocess_exec("osascript", "-e", script,
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 10)
        except Exception as e:
            logger.debug("macOS notification failed: %s", e)
    if SMS:
        from data.notifications import send_sms
        try:
            await asyncio.to_thread(send_sms, f"{title}: {body}")
        except Exception as e:
            logger.debug("Watchdog SMS failed: %s", e)


async def _probe() -> tuple[float | None, float | None]:
    """(last good price time, last scanner sweep time)."""
    from data import feeds
    from scanner import scheduler
    try:
        if await feeds.cached_futures_price("BTCUSDT"):
            _mem["last_price_ok"] = time.time()
    except Exception as e:
        logger.debug("Watchdog price probe failed: %s", e)
    try:
        sweep = (await scheduler.get_snapshot()).get("updated_at")
    except Exception:
        sweep = None
    return _mem["last_price_ok"], sweep


async def check(now: float | None = None) -> dict:
    now = now or time.time()
    s = _state()
    last = _mem["last_check"]
    _mem["last_check"] = now
    if last and now - last >= STALE_S:                    # nothing ran in between: the machine was asleep
        s["outages"] = ([{"start": last, "end": now, "minutes": round((now - last) / 60), "reason": "the app wasn't running (sleep)"}]
                        + s["outages"])[:KEEP]
        _save(s)
        await _notify("Crypto Oracle was asleep", f"No checks for {_fmt(now - last)} — nothing traded or was managed.")
    price_at, sweep_at = await _probe()
    _mem["last_price_ok"] = price_at
    started = _mem.setdefault("started", now)
    problems, lost = [], []
    for what, at in (("no Binance price", price_at), ("no scanner sweep", sweep_at)):
        good = at or started
        if now - good >= STALE_S:
            problems.append(f"{what} for {_fmt(now - good)}")
            lost.append(good)
    if problems and s["status"] == "ok":
        s.update(status="stale", since=min(lost), reason="; ".join(problems))
        _save(s)
        await _notify("Crypto Oracle: market data stopped", f"{s['reason']}. Stops and entries are on hold — check the network.")
    elif problems:
        s["reason"] = "; ".join(problems)
        _save(s)
    elif s["status"] == "stale":
        took = now - (s["since"] or now)
        s["outages"] = ([{"start": s["since"], "end": now, "minutes": round(took / 60), "reason": s["reason"]}] + s["outages"])[:KEEP]
        s.update(status="ok", since=None, reason=None)
        _save(s)
        await _notify("Crypto Oracle: data back", f"Market data recovered after {_fmt(took)}.")
    return s


def status() -> dict:
    s = _state()
    return {**s, "enabled": ENABLED, "stale_minutes": STALE_S // 60, "checked_at": _mem["last_check"],
            "last_price_ok": _mem["last_price_ok"]}
