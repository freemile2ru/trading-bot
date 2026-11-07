"""Trade alerts — an email and a text for every position opened or closed on any book (2026-11-06, the user's ask).

Every ledger (trading/paper_trader.PaperWallet, the live ledger included) reports here from the four places a
position is born or dies: a market entry, a limit fill, a full close and a partial close. Nothing is sent from
there — the event goes on a queue and a worker thread sends, so a slow mail server never holds the wallet lock
or the scheduler. Events that land within WINDOW seconds go out as one message (a basket opens four legs at
once; a red minute closes several), so a burst is one email and one text, not eight.

On by default in production (TRADE_ALERTS=email,whatsapp), off locally (the experiments open dozens of trades a
day): TRADE_ALERTS with any of email, whatsapp, sms turns it on anywhere, TRADE_ALERTS=off silences production.
WhatsApp goes through Twilio's sandbox (data/notifications.send_whatsapp: the 24-hour window and the template fall
back); an SMS costs Twilio's per-message rate and needs a verified sender; email is free. Shadow trades, resting
orders and the autotrader's funding entries are not reported — only positions that exist.
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

_DEFAULT = "email,whatsapp" if os.getenv("APP_ENV") == "production" else "off"
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


def _pct(x: float | None, digits: int = 1) -> str:
    return "" if x is None else f"{x:.{digits}f}%"


def _px(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{x:,.6g}" if x < 1 else f"{x:,.2f}" if x < 1000 else f"{x:,.0f}"


def _stop_pct(t: dict) -> float | None:
    return abs(t["stop_loss"] / t["entry_price"] - 1) * 100 if t.get("stop_loss") and t.get("entry_price") else None


def _r(t: dict) -> float | None:
    pnl, risk = t.get("pnl_usd") or 0.0, t.get("risk_usd") or 0.0
    return pnl / risk if risk else None


def _why(t: dict, n: int = 160) -> str:
    why = (t.get("oracle_reasoning") or t.get("reasoning") or t.get("entry_note") or "").strip().replace("\n", " ")
    return (why[:n] + "…") if len(why) > n else why


def line(ev: dict) -> str:
    """One event as plain text: a headline line, a numbers line, and the why — what WhatsApp and SMS show."""
    t, book = ev["trade"], ev["book"]
    side = (t.get("direction") or "?").upper()
    sym = t.get("symbol", "?")
    at = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%H:%M UTC")
    if ev["kind"] == "opened":
        sp = _stop_pct(t)
        head = f"🟢 OPENED {at} · {_where(book, t)}"
        what = f"{side} {sym} · ${t.get('notional_usd', 0):,.0f} at {t.get('leverage', '?')}x"
        nums = f"entry {_px(t.get('entry_price'))} · stop {_px(t.get('stop_loss'))}" + (f" ({_pct(sp)})" if sp is not None else "") + f" · risk ${t.get('risk_usd') or 0:,.0f}"
        why = _why(t)
        return "\n".join(x for x in (head, what, nums, f"why: {why}" if why else "") if x)
    pnl = t.get("pnl_usd") or 0.0
    r = _r(t)
    head = f"{'🟢' if pnl >= 0 else '🔴'} CLOSED {at} · {_where(book, t)}"
    what = f"{side} {sym} · {_money(pnl)}" + (f" ({r:+.2f}R)" if r is not None else "")
    nums = f"{t.get('close_reason') or 'closed'} · held {_hours(t)} · {_px(t.get('entry_price'))} → {_px(t.get('exit_price'))}"
    if t.get("locked_usd"):
        nums += f" · lock kept ${t['locked_usd']:,.0f}"
    return "\n".join((head, what, nums))


def compose(events: list[dict]) -> tuple[str, str]:
    """(subject, body) for a batch: the subject names each position, the body has a block per event."""
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
    body = "\n\n".join(line(e) for e in events)
    if n_close > 1:
        body += f"\n\n{n_close} closed: {_money(net)} net"
    if n_open or n_close:
        body += f"\n\nCrypto Oracle · {INSTANCE}"
    return subject, body


# ── The email: the same facts as tables ──────────────────────────────────────

_CSS = ("font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#1f2937;line-height:1.45")
_TH = "text-align:left;padding:6px 10px;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#6b7280;border-bottom:1px solid #e5e7eb;white-space:nowrap"
_TD = "padding:7px 10px;border-bottom:1px solid #f3f4f6;vertical-align:top;white-space:nowrap"
_GREEN, _RED, _GRAY = "#047857", "#b91c1c", "#6b7280"


def _esc(x) -> str:
    return (str(x if x is not None else "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pill(text: str, color: str) -> str:
    return f'<span style="display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600;color:#fff;background:{color}">{_esc(text)}</span>'


def _side(t: dict) -> str:
    side = (t.get("direction") or "?").upper()
    return _pill(side, _GREEN if side == "LONG" else _RED if side == "SHORT" else _GRAY)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    th = "".join(f'<th style="{_TH}">{_esc(h)}</th>' for h in headers)
    trs = "".join("<tr>" + "".join(f'<td style="{_TD}">{c}</td>' for c in r) + "</tr>" for r in rows)
    return f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;max-width:980px">' \
           f'<thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>'


def compose_html(events: list[dict]) -> str:
    """The batch as an HTML email: one table for the positions opened, one for those closed, a net line and the why."""
    when = datetime.fromtimestamp(events[0]["ts"], timezone.utc).strftime("%a %d %b %Y · %H:%M UTC")
    opened = [e for e in events if e["kind"] == "opened"]
    closed = [e for e in events if e["kind"] == "closed"]
    parts = [f'<div style="{_CSS}">',
             f'<div style="margin:0 0 14px"><span style="font-size:16px;font-weight:700">Crypto Oracle</span> '
             f'<span style="color:{_GRAY}">· {_esc(INSTANCE)} · {_esc(when)}</span></div>']
    if opened:
        rows = []
        for e in opened:
            t = e["trade"]
            sp = _stop_pct(t)
            rows.append([_esc(_where(e["book"], t)), f'{_side(t)} <b>{_esc(t.get("symbol", "?"))}</b>',
                         f'${t.get("notional_usd", 0):,.0f} <span style="color:{_GRAY}">at {_esc(t.get("leverage", "?"))}x</span>',
                         _esc(_px(t.get("entry_price"))),
                         _esc(_px(t.get("stop_loss"))) + (f' <span style="color:{_GRAY}">({_esc(_pct(sp))})</span>' if sp is not None else ""),
                         f'${t.get("risk_usd") or 0:,.0f}',
                         f'<span style="white-space:normal;color:{_GRAY}">{_esc(_why(t, 220))}</span>'])
        parts.append(f'<div style="margin:12px 0 6px;font-weight:600">{_pill("OPENED", _GREEN)} {len(opened)} position{"s" if len(opened) != 1 else ""}</div>')
        parts.append(_table(["Book", "Position", "Size", "Entry", "Stop", "Risk", "Why"], rows))
    if closed:
        rows = []
        for e in closed:
            t = e["trade"]
            pnl, r = t.get("pnl_usd") or 0.0, _r(t)
            color = _GREEN if pnl >= 0 else _RED
            rows.append([_esc(_where(e["book"], t)), f'{_side(t)} <b>{_esc(t.get("symbol", "?"))}</b>',
                         f'<b style="color:{color}">{_esc(_money(pnl))}</b>' + (f' <span style="color:{_GRAY}">({r:+.2f}R)</span>' if r is not None else ""),
                         _esc(t.get("close_reason") or "closed"), _esc(_hours(t)),
                         f'{_esc(_px(t.get("entry_price")))} → {_esc(_px(t.get("exit_price")))}',
                         (f'${t["locked_usd"]:,.0f}' if t.get("locked_usd") else f'<span style="color:{_GRAY}">—</span>')])
        net = sum((e["trade"].get("pnl_usd") or 0.0) for e in closed)
        parts.append(f'<div style="margin:16px 0 6px;font-weight:600">{_pill("CLOSED", _GREEN if net >= 0 else _RED)} {len(closed)} position{"s" if len(closed) != 1 else ""}'
                     + (f' <span style="color:{_GREEN if net >= 0 else _RED}">· {_esc(_money(net))} net</span>' if len(closed) > 1 else "") + '</div>')
        parts.append(_table(["Book", "Position", "P&L", "Exit", "Held", "Entry → Exit", "Lock kept"], rows))
    parts.append(f'<div style="margin-top:16px;font-size:12px;color:{_GRAY}">Sent by the {_esc(INSTANCE)} instance. Times are UTC.</div></div>')
    return "".join(parts)


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
        out["email"] = notifications.send_email(subject, body, html=compose_html(events))
    text = subject.replace(f"[Oracle {INSTANCE}] ", "") + "\n" + body[:900]
    if "whatsapp" in CHANNELS:
        out["whatsapp"] = notifications.send_whatsapp(text, title=f"Oracle {INSTANCE}",
                                                      when=datetime.fromtimestamp(events[0]["ts"], timezone.utc).strftime("%d %b %H:%M UTC"))
    if "sms" in CHANNELS:
        out["sms"] = notifications.send_sms(text)
    logger.info("trade alert: %d event(s) → %s", len(events), out)
    return out


def flush(timeout: float = 25.0) -> None:
    """Send what is queued now (for scripts that exit right after closing trades, and for tests)."""
    deadline = time.time() + timeout
    while _pending > 0 and time.time() < deadline:
        _ensure_worker()
        time.sleep(0.05)


atexit.register(lambda: flush(10.0) if CHANNELS and _pending > 0 else None)
