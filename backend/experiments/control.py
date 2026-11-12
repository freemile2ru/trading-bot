"""Control data — what could have happened to every closed trade of a book (2026-11-09, the user's ask after a run of
cascade mirrors that left within minutes at a loss).

A trade's record says where we got out. This replays the coin's 5-minute bars from the entry for CONTROL_H hours and
writes beside the trade what the same position would have done under plain controls — held for 1/2/4/8/24/48 hours with
only its original stop (pessimistic: a bar's low/high hits the stop before anything favourable counts), its best and worst
point in each window, and, for the hours after the actual exit, how far the price went on in the trade's direction. Then
the question "was the exit early?" has a number per trade, and a report per book sums them: actual against each control,
by exit reason, with the early exits (closed before the book's own horizon) on their own line.

Books: an experiment book by name ("cascade", "burst_up", "prop"…) or the autotrader's ledger by lane ("auto:cascade",
"auto:reversal"). Records live in the state store under control:<trade_id> and are only written once a trade's entry is
CONTROL_H hours old (the whole window is known). A tick every CONTROL_EVERY_S fills in the newest closes; POST
/control/{book}/run backfills on demand. This is measurement only: nothing here trades.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from storage import state as kv

logger = logging.getLogger("experiments.control")

CONTROL_H = 48
HORIZONS_H = (1, 2, 4, 8, 24, 48)
AFTER_EXIT_H = (1, 4, 24)
CONTROL_EVERY_S = int(os.getenv("CONTROL_EVERY_S", "900"))
BATCH = int(os.getenv("CONTROL_BATCH", "20"))
BOOKS = [b.strip() for b in os.getenv("CONTROL_BOOKS", "cascade,auto:cascade,auto:reversal,burst_up,burst_dip,prop,auction,weekly_rs,auto:weekly").split(",") if b.strip()]
# Each book's own horizon: an exit before it is "early" in the report.
HORIZON_OF = {"cascade": 4.0, "auto:cascade": 4.0, "burst_up": 4.0, "burst_dip": 4.0, "auto:reversal": 24.0, "prop": 24.0, "auction": 6.5,
              "weekly_rs": 168.0, "auto:weekly": 168.0}   # weekly legs live to the next Monday: the controls (≤ 48h) show the first two days
REPLAY_V = 2                                                      # bump when replay() changes: older records are redone by the tick
FEE_BPS = 5.0                                                     # taker, one side — the controls pay an exit like ours did
INDEX_KEEP = 600
_last_tick = {"at": 0.0}


def _k(*parts: str) -> str:
    return kv.key("control", *parts)


def _ts(s) -> float:
    if not s:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return 0.0


def closed_trades(book: str) -> list[dict]:
    """The book's closed trades, oldest first, each with opened_ts / closed_ts."""
    if book.startswith("auto:"):
        from trading import history
        from autotrader import lanes
        lane = book.split(":", 1)[1]
        rows = [t for t in history.WALLETS["auto"].all_trades()["closed"] if lanes.lane_of(t) == lane]
    else:
        from experiments import books
        if book not in books.WALLETS:
            return []
        rows = books.WALLETS[book].all_trades()["closed"]
    out = []
    for t in rows:
        o, c = t.get("opened_ts") or _ts(t.get("opened_at")), _ts(t.get("closed_at"))
        if o and c:
            out.append({**t, "opened_ts": o, "closed_ts": c})
    return sorted(out, key=lambda t: t["opened_ts"])


async def bars(symbol: str, start: float, end: float) -> list[dict]:
    """5-minute perpetual bars covering [start, end] (most recent last), in one or two requests."""
    from data.http import get_json
    from data.binance_client import FAPI
    out: list[dict] = []
    cursor = int(start * 1000)
    end_ms = int(end * 1000)
    for _ in range(3):
        rows = await get_json(f"{FAPI}/klines", params={"symbol": symbol, "interval": "5m", "startTime": cursor, "endTime": end_ms, "limit": 1000})
        if not rows:
            break
        out += [{"t": r[0] / 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in rows]
        if len(rows) < 1000:
            break
        cursor = rows[-1][0] + 1
    return out


def replay(trade: dict, path: list[dict]) -> dict:
    """The controls for one trade on its bars: held-with-stop P&L by horizon, best/worst by horizon, the move after the
    actual exit. P&L in dollars at the trade's quantity, less one exit fee; % figures are of the entry price."""
    s = 1 if trade["direction"] == "long" else -1
    e, stop, q = trade["entry_price"], trade.get("stop_loss") or trade.get("original_stop_loss"), trade.get("quantity") or 0.0
    o_ts, x_ts = trade["opened_ts"], trade["closed_ts"]
    notional = e * q or 1.0
    fee = notional * FEE_BPS / 1e4
    after = [b for b in path if b["t"] >= o_ts]
    held: dict[str, float | None] = {}
    best: dict[str, float] = {}
    worst: dict[str, float] = {}
    stopped_at_h = None
    hi = lo = None
    for H in HORIZONS_H:
        window = [b for b in after if b["t"] < o_ts + H * 3600]
        if not window:
            held[f"{H}h"] = None
            continue
        pnl = None
        for b in window:
            if stopped_at_h is None and stop and ((s > 0 and b["l"] <= stop) or (s < 0 and b["h"] >= stop)):
                stopped_at_h = round((b["t"] - o_ts) / 3600, 2)
                pnl = (stop - e) * s * q - fee
                break
        if pnl is None:
            pnl = (window[-1]["c"] - e) * s * q - fee if stopped_at_h is None else held.get(f"{HORIZONS_H[HORIZONS_H.index(H) - 1]}h")
        held[f"{H}h"] = round(pnl, 2) if pnl is not None else None
        # The furthest the price went our way / against us, as signed % of the entry: for a short the
        # best is the lowest low and the worst the highest high (the first version took the wrong extremes).
        hi = max((b["h"] - e) * s / e * 100 if s > 0 else (b["l"] - e) * s / e * 100 for b in window)
        lo = min((b["l"] - e) * s / e * 100 if s > 0 else (b["h"] - e) * s / e * 100 for b in window)
        best[f"{H}h"], worst[f"{H}h"] = round(hi, 3), round(lo, 3)
    exit_bar = next((b for b in after if b["t"] >= x_ts), None)
    after_exit: dict[str, float | None] = {}
    for H in AFTER_EXIT_H:
        later = [b for b in after if x_ts <= b["t"] < x_ts + H * 3600]
        if not later or not exit_bar:
            after_exit[f"{H}h"] = None
            continue
        after_exit[f"{H}h"] = round((later[-1]["c"] - exit_bar["o"]) * s / e * 100, 3)      # % the price went on our way after we left
    return {"held": held, "best_pct": best, "worst_pct": worst, "stopped_at_h": stopped_at_h, "after_exit_pct": after_exit,
            "actual": round(trade.get("pnl_usd") or 0.0, 2), "hold_h": round((x_ts - o_ts) / 3600, 2), "bars": len(after), "at": time.time(), "v": REPLAY_V}


async def record(book: str, trade: dict) -> dict | None:
    """Replay one closed trade once its window is complete; stored under the trade's id."""
    now = time.time()
    if trade["opened_ts"] + CONTROL_H * 3600 > now:
        return None
    path = await bars(trade["symbol"], trade["opened_ts"] - 300, trade["opened_ts"] + CONTROL_H * 3600 + 300)
    if len(path) < 12:
        return None
    rec = {**replay(trade, path), "book": book, "trade_id": trade["id"], "symbol": trade["symbol"], "direction": trade["direction"],
           "opened_ts": trade["opened_ts"], "closed_ts": trade["closed_ts"], "close_reason": trade.get("close_reason"), "notional": trade.get("notional_usd"),
           "lane": trade.get("lane"), "hold_rule": trade.get("hold_rule")}
    kv.set_sync(_k(trade["id"]), rec)
    idx = kv.get_sync(_k("index", book)) or []
    if trade["id"] not in idx:
        kv.set_sync(_k("index", book), (idx + [trade["id"]])[-INDEX_KEEP:])
    return rec


async def run(book: str, limit: int = BATCH) -> dict:
    """Fill in the controls for the book's newest closed trades that lack one (and whose window has passed)."""
    done = {tid for tid in (kv.get_sync(_k("index", book)) or []) if (kv.get_sync(_k(tid)) or {}).get("v") == REPLAY_V}
    todo = [t for t in closed_trades(book) if t["id"] not in done and t["opened_ts"] + CONTROL_H * 3600 <= time.time()]
    out = []
    for t in todo[-limit:]:
        try:
            rec = await record(book, t)
            if rec:
                out.append(rec["trade_id"])
        except Exception as e:
            logger.warning("Control replay for %s %s failed: %s", book, t.get("symbol"), e)
    return {"book": book, "recorded": len(out), "pending": max(0, len(todo) - len(out))}


async def tick(now: float | None = None) -> None:
    now = now or time.time()
    if now - _last_tick["at"] < CONTROL_EVERY_S:
        return
    _last_tick["at"] = now
    for book in BOOKS:
        try:
            await run(book, BATCH)
        except Exception as e:
            logger.warning("Control tick for %s failed: %s", book, e)


def records(book: str) -> list[dict]:
    return [r for r in (kv.get_sync(_k(tid)) for tid in (kv.get_sync(_k("index", book)) or [])) if r]


def report(book: str) -> dict:
    """Actual against each control, for the book and for its early exits; by exit reason; the rows."""
    rows = records(book)
    horizon = HORIZON_OF.get(book, 24.0)

    def agg(rs: list[dict]) -> dict:
        n = len(rs)
        if not n:
            return {"n": 0}
        held = {f"{H}h": round(sum((r["held"].get(f"{H}h") if r["held"].get(f"{H}h") is not None else r["actual"]) for r in rs), 2) for H in HORIZONS_H}
        after = {f"{H}h": round(sum(r["after_exit_pct"].get(f"{H}h") or 0.0 for r in rs) / n, 3) for H in AFTER_EXIT_H}
        best4 = sum(r["best_pct"].get("4h") or 0.0 for r in rs) / n
        # Entry quality (2026-11-12): the Turtle E-ratio — mean best excursion ÷ mean worst excursion by window, in % of
        # the entry. A random entry scores ≈ 1.0; above it the entry is ahead of the price, below it behind.
        e_ratio = {}
        for H in ("1h", "4h", "24h"):
            b = [r["best_pct"].get(H) for r in rs if r["best_pct"].get(H) is not None]
            w = [-(r["worst_pct"].get(H) or 0.0) for r in rs if r["worst_pct"].get(H) is not None]
            e_ratio[H] = round(sum(b) / sum(w), 2) if b and w and sum(w) > 0 else None
        return {"n": n, "actual": round(sum(r["actual"] for r in rs), 2), "wins": sum(1 for r in rs if r["actual"] > 0), "held": held, "e_ratio": e_ratio,
                "after_exit_avg_pct": after, "best_4h_avg_pct": round(best4, 3), "worst_4h_avg_pct": round(sum(r["worst_pct"].get("4h") or 0.0 for r in rs) / n, 3),
                "stopped_share_pct": round(100 * sum(1 for r in rs if r["stopped_at_h"] is not None) / n, 1), "avg_hold_h": round(sum(r["hold_h"] for r in rs) / n, 2)}
    early = [r for r in rows if r["hold_h"] < horizon * 0.9]
    by_reason: dict[str, dict] = {}
    for reason in sorted({r.get("hold_rule") or r.get("close_reason") or "?" for r in rows}):
        by_reason[reason] = agg([r for r in rows if (r.get("hold_rule") or r.get("close_reason") or "?") == reason])
    return {"book": book, "horizon_h": horizon, "control_h": CONTROL_H, "horizons": [f"{H}h" for H in HORIZONS_H], "after_exit": [f"{H}h" for H in AFTER_EXIT_H],
            "all": agg(rows), "early": agg(early), "by_reason": by_reason,
            "rows": sorted(rows, key=lambda r: -r["opened_ts"])[:200], "books": BOOKS,
            "pending": sum(1 for t in closed_trades(book) if t["id"] not in {r["trade_id"] for r in rows if r.get("v") == REPLAY_V})}
