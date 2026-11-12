"""Event calendar — scheduled releases the books should know about before they happen.

  macro      FOMC decisions (14:00 ET on the second day of each meeting) and CPI releases (08:30 ET), from the
             published 2026–27 schedules (2027 FOMC dates are tentative until the Fed confirms each one).
  earnings   the stock perps' underlying companies, from Nasdaq's public earnings calendar, looked ahead
             EARNINGS_LOOKAHEAD_DAYS and refreshed daily. US names only (the Korean/HK/Taiwanese listings aren't
             on it — SK Hynix, Samsung, TSMC, Tencent).

Rules (2026-10-28, deliberately mild — measured before they're tightened):
  * macro blackout: no NEW short-term crypto entries (autotrader, Claude swing) from MACRO_BEFORE_MIN before a
    release to MACRO_AFTER_MIN after it. Hedged, event and cascade books are unaffected; open positions too.
  * earnings: the stock trend book opens no NEW position in a name reporting within EARNINGS_DAYS; held ones stay
    (holding through news is what trend following does).
  * every trade opened by those books is tagged near_event, so the rules' value can be judged later.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from storage import state as kv

logger = logging.getLogger("ops.calendar")

ET = ZoneInfo("America/New_York")
MACRO_BEFORE_MIN = int(os.getenv("CALENDAR_MACRO_BEFORE_MIN", "60"))
MACRO_AFTER_MIN = int(os.getenv("CALENDAR_MACRO_AFTER_MIN", "30"))
EARNINGS_DAYS = float(os.getenv("CALENDAR_EARNINGS_DAYS", "2"))
EARNINGS_LOOKAHEAD_DAYS = 14
NEAR_H = 24                       # a trade within this of a macro release is tagged "near"
NEAR_EARNINGS_DAYS = 3

# Second (decision) day of each FOMC meeting.
FOMC_DECISION_DAYS = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28",
                      "2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15",
                      "2027-10-27", "2027-12-08"]
CPI_DAYS = ["2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-11",
            "2026-10-14", "2026-11-10", "2026-12-10", "2027-01-13", "2027-02-10", "2027-03-10", "2027-04-13", "2027-05-12",
            "2027-06-10", "2027-07-13", "2027-08-11", "2027-09-14", "2027-10-13", "2027-11-10", "2027-12-10"]
NASDAQ = "https://api.nasdaq.com/api/calendar/earnings"


def _et(day: str, hour: int, minute: int) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, minute=minute, tzinfo=ET).timestamp()


def macro_events() -> list[dict]:
    out = [{"kind": "FOMC", "ts": _et(d, 14, 0), "label": "FOMC decision"} for d in FOMC_DECISION_DAYS]
    out += [{"kind": "CPI", "ts": _et(d, 8, 30), "label": "CPI release"} for d in CPI_DAYS]
    return sorted(out, key=lambda e: e["ts"])


def macro_blackout(now: float | None = None) -> str | None:
    """Why no new short-term entry should open right now, or None."""
    now = now or time.time()
    for e in macro_events():
        if e["ts"] - MACRO_BEFORE_MIN * 60 <= now <= e["ts"] + MACRO_AFTER_MIN * 60:
            mins = round((e["ts"] - now) / 60)
            when = f"in {mins} min" if mins > 0 else f"{-mins} min ago"
            return f"{e['label']} {when} — no new entries from {MACRO_BEFORE_MIN} min before to {MACRO_AFTER_MIN} min after"
    return None


def next_macro(now: float | None = None) -> dict | None:
    now = now or time.time()
    return next((e for e in macro_events() if e["ts"] >= now - MACRO_AFTER_MIN * 60), None)


# ── Earnings ───────────────────────────────────────────────────────────────────

def _tickers() -> dict[str, str]:
    """{Nasdaq ticker: perp symbol} for the stock perps whose underlying is a US listing."""
    from markets.engine import MARKETS
    return {yt: perp for perp, yt in MARKETS["stocks"]["symbols"].items() if "." not in yt}


async def refresh_earnings(now: float | None = None) -> list[dict]:
    """Fetch the next EARNINGS_LOOKAHEAD_DAYS of Nasdaq's calendar for our names (cached a day)."""
    from data.http import get_json
    now = now or time.time()
    cached = kv.get_sync(kv.key("calendar", "earnings"))
    if cached and now - cached.get("at", 0) < 86400:
        return cached["events"]
    tickers = _tickers()
    events: list[dict] = []
    for d in range(EARNINGS_LOOKAHEAD_DAYS + 1):
        day = (datetime.fromtimestamp(now, ET) + timedelta(days=d)).strftime("%Y-%m-%d")
        try:
            data = await get_json(NASDAQ, params={"date": day}, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        except Exception as e:
            logger.debug("Nasdaq calendar %s unavailable: %s", day, e)
            continue
        for row in ((data or {}).get("data") or {}).get("rows") or []:
            tk = row.get("symbol")
            if tk in tickers:
                after = "after" in (row.get("time") or "")
                ts = _et(day, 16, 5) if after else _et(day, 7, 0)
                events.append({"kind": "earnings", "symbol": tickers[tk], "ticker": tk, "ts": ts,
                               "label": f"{tk} earnings ({'after the close' if after else 'before the open'})"})
    # Reported ones fall out of the look-ahead; keep them aside so the month grid shows history.
    past = {(e["ticker"], e["ts"]): e for e in (kv.get_sync(kv.key("calendar", "earnings_past")) or [])}
    past.update({(e["ticker"], e["ts"]): e for e in (cached or {}).get("events") or [] if e["ts"] < now})
    kv.set_sync(kv.key("calendar", "earnings_past"), [e for e in past.values() if e["ts"] > now - PAGE_PAST_DAYS * 86400], 365 * 86400)
    kv.set_sync(kv.key("calendar", "earnings"), {"at": now, "events": events}, 2 * 86400)
    return events


def earnings_events() -> list[dict]:
    cached = kv.get_sync(kv.key("calendar", "earnings")) or {}
    return cached.get("events") or []


def earnings_block(symbol: str, now: float | None = None) -> str | None:
    """Why the trend book shouldn't open `symbol` now: earnings within EARNINGS_DAYS."""
    now = now or time.time()
    for e in earnings_events():
        if e["symbol"] == symbol and 0 <= e["ts"] - now <= EARNINGS_DAYS * 86400:
            return f"{e['label']} in {(e['ts'] - now) / 86400:.1f} days"
    return None


def near_event(symbol: str | None = None, now: float | None = None) -> str | None:
    """A tag for a trade opened close to a release: the event, or None."""
    now = now or time.time()
    for e in macro_events():
        if abs(e["ts"] - now) <= NEAR_H * 3600:
            return e["label"]
    if symbol:
        for e in earnings_events():
            if e["symbol"] == symbol and abs(e["ts"] - now) <= NEAR_EARNINGS_DAYS * 86400:
                return e["label"]
    return None


def status(now: float | None = None) -> dict:
    now = now or time.time()
    upcoming = [e for e in macro_events() if e["ts"] >= now - MACRO_AFTER_MIN * 60][:4]
    upcoming += [e for e in earnings_events() if e["ts"] >= now - 3600]
    upcoming.sort(key=lambda e: e["ts"])
    return {"now": now, "blackout": macro_blackout(now), "next_macro": next_macro(now), "upcoming": upcoming[:12],
            "rules": {"macro_before_min": MACRO_BEFORE_MIN, "macro_after_min": MACRO_AFTER_MIN, "earnings_days": EARNINGS_DAYS}}


# ── Calendar tab ───────────────────────────────────────────────────────────────

PAGE_AHEAD_DAYS = 365
PAGE_PAST_DAYS = 120
PAST_MACRO = 6
# Every book stamps near_event at entry (2026-11-10; before that only the autotrader's scanner path, the Claude swing
# and the stock trend books did, none of which trades on production — the report was empty there).
HOOKED_BOOKS = ("main", "auto", "auto_live", "copy_trading", "copy_stocks", "copy_futures", "auction", "prop", "cascade", "burst_up", "burst_dip",
                "weekly_rs", "claude_swing", "stocks_trend")


def _rule(e: dict) -> str:
    if e["kind"] == "earnings":
        return f"no new {e['symbol']} position in the stock trend book from {EARNINGS_DAYS:g} days before until it reports"
    return f"no new autotrader / Claude swing entries from {MACRO_BEFORE_MIN} min before to {MACRO_AFTER_MIN} min after"


async def _btc_reaction(e: dict) -> dict | None:
    """BTC's move at a past release — the hour of the release and the 24h after — cached per event."""
    k = kv.key("calendar", "reaction", str(int(e["ts"])))
    hit = kv.get_sync(k)
    if hit:
        return hit
    from data.derivatives_client import fetch_klines_range
    try:
        rows = await fetch_klines_range("BTCUSDT", "1h", int(e["ts"] * 1000) - 3600_000, int(e["ts"] * 1000) + 25 * 3600_000)
    except Exception as ex:
        logger.debug("reaction klines unavailable: %s", ex)
        return None
    if len(rows) < 3:
        return None
    before = rows[0][4]                                             # close of the hour before the release
    hour = next((r for r in rows if r[0] >= e["ts"] * 1000), None)
    if not hour:
        return None
    rng = max(r[2] for r in rows[1:3]) / min(r[3] for r in rows[1:3]) - 1
    out = {"hour_pct": round((hour[4] / before - 1) * 100, 2), "range_2h_pct": round(rng * 100, 2),
           "day_pct": round((rows[-1][4] / before - 1) * 100, 2)}
    kv.set_sync(k, out, 30 * 86400)
    return out


def _wallets() -> dict:
    from experiments import books
    from live import swing as live_swing
    from markets import engine as markets
    from trading import history
    out = {f"auto{'_live' if w.label.endswith('_live') else ''}": w for w in live_swing.wallets("auto")}
    out["main"] = history.WALLETS["main"]
    out["stocks_trend"] = markets.WALLETS["stocks_trend"]
    for name in HOOKED_BOOKS:
        if name in books.WALLETS:
            out[name] = books.WALLETS[name]
    return out


def near_event_trades() -> dict:
    """Trades the hooked books opened since tagging began, split by whether they were near a release."""
    near, clear, rows = [], [], []
    for book, w in _wallets().items():
        all_t = w.all_trades()
        for t in all_t["closed"] + all_t["open"]:
            if "near_event" not in t:
                continue
            risk = t.get("risk_usd") or (abs(t["entry_price"] - t["stop_loss"]) * t["quantity"]
                                         if t.get("entry_price") and t.get("stop_loss") and t.get("quantity") else None)
            r = (t["pnl_usd"] / risk) if t.get("closed_at") and risk and t.get("pnl_usd") is not None else None
            row = {"book": book, "symbol": t["symbol"], "direction": t["direction"], "event": t.get("near_event"),
                   "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"), "pnl_usd": t.get("pnl_usd") if t.get("closed_at") else None,
                   "r": None if r is None else round(r, 2), "close_reason": t.get("close_reason")}
            (near if t.get("near_event") else clear).append(row)
            if t.get("near_event"):
                rows.append(row)

    def _sum(xs: list[dict]) -> dict:
        done = [x for x in xs if x["closed_at"]]
        rs = [x["r"] for x in done if x["r"] is not None]
        return {"trades": len(xs), "closed": len(done), "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
                "pnl_usd": round(sum(x["pnl_usd"] or 0 for x in done), 2),
                "win_pct": round(100 * sum((x["pnl_usd"] or 0) > 0 for x in done) / len(done)) if done else None}
    rows.sort(key=lambda x: x["opened_at"] or "", reverse=True)
    return {"near": _sum(near), "clear": _sum(clear), "rows": rows[:50]}


async def page(now: float | None = None) -> dict:
    """Everything the Calendar tab shows."""
    now = now or time.time()
    horizon = now + PAGE_AHEAD_DAYS * 86400
    events = [{**e, "window": [e["ts"] - MACRO_BEFORE_MIN * 60, e["ts"] + MACRO_AFTER_MIN * 60]}
              for e in macro_events() if now - PAGE_PAST_DAYS * 86400 <= e["ts"] <= horizon]
    seen = {(e["ticker"], e["ts"]) for e in earnings_events()}
    events += earnings_events() + [e for e in (kv.get_sync(kv.key("calendar", "earnings_past")) or []) if (e["ticker"], e["ts"]) not in seen]
    for e in events:
        e["rule"] = _rule(e)
    events.sort(key=lambda e: e["ts"])
    past = [e for e in macro_events() if e["ts"] < now - MACRO_AFTER_MIN * 60][-PAST_MACRO:]
    for e in past:
        e["btc"] = await _btc_reaction(e)
    cached = kv.get_sync(kv.key("calendar", "earnings")) or {}
    return {**status(now), "events": events, "past": past[::-1], "trades": near_event_trades(),
            "earnings_refreshed_at": cached.get("at"), "earnings_tickers": sorted(_tickers()),
            "settings": {"before_min": MACRO_BEFORE_MIN, "after_min": MACRO_AFTER_MIN, "earnings_days": EARNINGS_DAYS,
                         "near_h": NEAR_H, "near_earnings_days": NEAR_EARNINGS_DAYS, "lookahead_days": EARNINGS_LOOKAHEAD_DAYS}}
