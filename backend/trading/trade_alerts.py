"""Trade alerts — an email and a text for every position opened or closed on any book (2026-11-06, the user's ask).

Every ledger (trading/paper_trader.PaperWallet, the live ledger included) reports here from the four places a
position is born or dies: a market entry, a limit fill, a full close and a partial close. Nothing is sent from
there — the event goes on a queue and a worker thread sends, so a slow mail server never holds the wallet lock
or the scheduler. Events that land within WINDOW seconds go out as one message (a basket opens four legs at
once; a red minute closes several), so a burst is one email and one text, not eight.

On by default in production (TRADE_ALERTS=email,sms), off locally (the experiments open dozens of trades a day):
TRADE_ALERTS=email or email,sms turns it on anywhere, TRADE_ALERTS=off silences production. Each text costs
Twilio's per-message rate; email is free. Shadow trades, resting orders and the autotrader's funding entries are not
reported — only positions that exist.
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("trading.trade_alerts")

_DEFAULT = "email,sms" if os.getenv("APP_ENV") == "production" else "off"
CHANNELS = {c.strip() for c in os.getenv("TRADE_ALERTS", _DEFAULT).split(",") if c.strip() and c.strip() != "off"}
WINDOW = float(os.getenv("TRADE_ALERT_WINDOW_S", "20"))       # events this close together share one message
INSTANCE = "prod" if os.getenv("APP_ENV") == "production" else "local"

_q: "queue.Queue[dict]" = queue.Queue()
_worker: threading.Thread | None = None
_lock = threading.Lock()
_pending = 0                                                  # queued or being sent: what flush() waits for


def enabled() -> bool:
    return bool(CHANNELS)


# ── What the wallets call ─────────────────────────────────────────────────────

def opened(book: str, trade: dict) -> None:
    _put("opened", book, trade)


def closed(book: str, trade: dict) -> None:
    _put("closed", book, trade)


def _put(kind: str, book: str, trade: dict) -> None:
    if not CHANNELS:
        return
    global _pending
    try:
        with _lock:
            _pending += 1
        _q.put_nowait({"kind": kind, "book": book, "trade": dict(trade), "ts": time.time()})
        _ensure_worker()
    except Exception as e:                      # never let a notification break a trade
        logger.debug("trade alert dropped: %s", e)


# ── Wording ───────────────────────────────────────────────────────────────────

def _money(x: float | None) -> str:
    x = x or 0.0
    return f"{'+' if x >= 0 else '−'}${abs(x):,.0f}"


def _hours(trade: dict) -> str:
    try:
        o = datetime.fromisoformat(trade["opened_at"]).timestamp()
        c = datetime.fromisoformat(trade["closed_at"]).timestamp() if trade.get("closed_at") else time.time()
        h = (c - o) / 3600
    except Exception:
        return ""
    return f"{h * 60:.0f}m" if h < 1 else (f"{h:.1f}h" if h < 48 else f"{h / 24:.1f}d")


def _where(book: str, trade: dict) -> str:
    lane = trade.get("lane")
    return f"{book} · {lane} lane" if lane else book


def line(ev: dict) -> str:
    t, book = ev["trade"], ev["book"]
    side = (t.get("direction") or "?").upper()
    sym = t.get("symbol", "?")
    at = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%H:%M UTC")
    if ev["kind"] == "opened":
        stop_pct = abs(t["stop_loss"] / t["entry_price"] - 1) * 100 if t.get("stop_loss") and t.get("entry_price") else None
        bits = [f"OPENED {at}", _where(book, t), f"{side} {sym}", f"${t.get('notional_usd', 0):,.0f} at {t.get('leverage', '?')}x",
                f"entry {t.get('entry_price')}", f"stop {t.get('stop_loss')}" + (f" ({stop_pct:.1f}%)" if stop_pct is not None else ""),
                f"risk ${t.get('risk_usd') or 0:,.0f}"]
        why = (t.get("oracle_reasoning") or t.get("reasoning") or "").strip().replace("\n", " ")
        if why:
            bits.append("why: " + (why[:160] + "…" if len(why) > 160 else why))
    else:
        pnl, risk = t.get("pnl_usd") or 0.0, t.get("risk_usd") or 0.0
        r = f" ({pnl / risk:+.2f}R)" if risk else ""
        bits = [f"CLOSED {at}", _where(book, t), f"{side} {sym}", f"{_money(pnl)}{r}", str(t.get("close_reason") or "closed"),
                f"held {_hours(t)}", f"{t.get('entry_price')} → {t.get('exit_price')}"]
        if t.get("locked_usd"):
            bits.append(f"lock kept ${t['locked_usd']:,.0f}")
    return " · ".join(b for b in bits if b)


def compose(events: list[dict]) -> tuple[str, str]:
    """(subject, body) for a batch: the subject names each position, the body has a line per event."""
    heads = []
    for ev in events:
        t = ev["trade"]
        head = f"{(t.get('direction') or '?')[0].upper()} {t.get('symbol', '?')}"
        if ev["kind"] == "closed":
            head += f" {_money(t.get('pnl_usd'))}"
        heads.append(("opened " if ev["kind"] == "opened" else "closed ") + head)
    n_open, n_close = sum(e["kind"] == "opened" for e in events), sum(e["kind"] == "closed" for e in events)
    subject = f"[Oracle {INSTANCE}] " + "; ".join(heads[:4]) + (f" (+{len(heads) - 4} more)" if len(heads) > 4 else "")
    net = sum((e["trade"].get("pnl_usd") or 0.0) for e in events if e["kind"] == "closed")
    body = "\n".join(line(e) for e in events)
    if n_close > 1:
        body += f"\n\n{n_close} closed: {_money(net)} net"
    if n_open or n_close:
        body += f"\n\nCrypto Oracle · {INSTANCE}"
    return subject, body


# ── The worker ────────────────────────────────────────────────────────────────

def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="trade-alerts", daemon=True)
            _worker.start()


def _drain(first: dict) -> list[dict]:
    """The first event plus everything that arrives within WINDOW seconds of it."""
    batch, deadline = [first], time.time() + WINDOW
    while True:
        left = deadline - time.time()
        if left <= 0:
            return batch
        try:
            batch.append(_q.get(timeout=left))
        except queue.Empty:
            return batch


def _run() -> None:
    while True:
        try:
            first = _q.get(timeout=300)
        except queue.Empty:
            return                                    # idle: the next event starts a fresh worker
        batch = _drain(first)
        try:
            send(batch)
        except Exception as e:
            logger.warning("trade alert failed: %s", e)
        finally:
            global _pending
            with _lock:
                _pending -= len(batch)


def send(events: list[dict]) -> dict:
    """Send one message for the batch on every configured channel. Returns what went where."""
    from data import notifications
    subject, body = compose(events)
    out = {}
    if "email" in CHANNELS:
        out["email"] = notifications.send_email(subject, body)
    if "sms" in CHANNELS:
        out["sms"] = notifications.send_sms(subject.replace(f"[Oracle {INSTANCE}] ", "") + "\n" + body[:900])
    logger.info("trade alert: %d event(s) → %s", len(events), out)
    return out


def flush(timeout: float = 25.0) -> None:
    """Send what is queued now (for scripts that exit right after closing trades, and for tests)."""
    deadline = time.time() + timeout
    while _pending > 0 and time.time() < deadline:
        _ensure_worker()
        time.sleep(0.05)


atexit.register(lambda: flush(10.0) if CHANNELS and _pending > 0 else None)
