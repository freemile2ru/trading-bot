"""The reversal signal's index leg — the same timing, the alt index instead of the picked coin (2026-11-17, pre-registered).

The index shadow (experiments/index_shadow.py) scored 54 reversal episodes by 2026-11-17: the equal-weight alt index moved
+0.75% in the lane's direction (t 2.87), the coin the score picked +0.62%, the leg as traded +0.51%. The edge is the
market timing; the coin pick and the exit each give a slice back, and the coin's own stop-outs are the lane's whole loss.
This book trades that finding as a paper position instead of a replay: when the reversal lane opens a slot's legs, it
opens ONE short (or long, if the slot is long) in ALTIDX — an equal-weight index of the twenty alts the shadow uses,
rebased to 100 at the book's first tick — at the same risk as one coin leg, a STOP_PCT disaster stop, and it closes when
the slot's last coin leg closes (or at HARD_CAP_H). So every index leg is paired with the coin legs it ran beside, in R
of each leg's own risk.

Pre-registered verdict, once VERDICT_N episodes have closed: "index better" when the index leg beats the slot's mean coin
R by ≥ MIN_GAP_R with t ≥ 2 and both halves agree; "coin better" on the same terms the other way; else "no difference".
"index better" is the case for expressing the lane in the index (or majors) and retiring the coin pick.

Local only: the book sizes its leg from the coin legs' risk, so it needs a live reversal lane beside it; on production the
lane runs, and the book is promoted only on its verdict. Marking: the paper wallet prices symbols by name from Binance,
so this book marks itself each tick at the index level (PaperWallet.process_paths with the level as the live price).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime

from storage import state as kv

logger = logging.getLogger("experiments.reversal_index")

BOOK = "reversal_index"
SYMBOL = "ALTIDX"
STOP_PCT = float(os.getenv("REVERSAL_INDEX_STOP_PCT", "4"))      # ~1.5 daily ATRs of the index (the coin legs' rule), as a plain % stop
LEVERAGE = int(os.getenv("REVERSAL_INDEX_LEVERAGE", "3"))
FRESH_S = 30 * 60                                                   # a slot first seen older than this (a restart) is not taken
VERDICT_N = 40
MIN_GAP_R = 0.1
MIN_COMPONENTS = 12                                                 # fewer priced components than this: no level, no mark


def _k(*parts: str) -> str:
    return kv.key("reversal_index", *parts)


def _ts(iso: str | float | None) -> float:
    if not iso:
        return 0.0
    if isinstance(iso, (int, float)):
        return float(iso)
    return datetime.fromisoformat(iso).timestamp()


def components() -> tuple[str, ...]:
    from experiments import index_shadow
    return index_shadow.INDEX


async def level(now: float | None = None) -> tuple[float | None, dict]:
    """The index now: 100 × the mean of each component's price over its base (the price at the book's first tick, kept
    for ever). A component without a base yet gets one on first sight; one without a price now is left out."""
    from trading import monitor
    base = kv.get_sync(_k("base")) or {}
    prices = dict(zip(components(), await asyncio.gather(*(monitor.live_price(s) for s in components()))))
    changed = False
    for sym, px in prices.items():
        if px and sym not in base:
            base[sym] = float(px)
            changed = True
    if changed:
        kv.set_sync(_k("base"), base)
    ratios = [prices[s] / base[s] for s in components() if prices.get(s) and base.get(s)]
    if len(ratios) < MIN_COMPONENTS:
        return None, {"priced": len(ratios), "of": len(components())}
    return round(100.0 * sum(ratios) / len(ratios), 4), {"priced": len(ratios), "of": len(components())}


async def candles(interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """ALTIDX as OHLC bars over a window, built from the components' perpetual klines: each bar is 100 × the mean of the
    components' open/high/low/close over their base (the kv base the book marks itself at; a component without one yet
    uses its first close in the window). A bar needs MIN_COMPONENTS components; volume is the quote volume summed.
    This is what the Trade tab and the review chart show for the index book's legs (2026-11-18)."""
    from data.derivatives_client import fetch_klines_range
    base = dict(kv.get_sync(_k("base")) or {})
    syms = components()
    results = await asyncio.gather(*(fetch_klines_range(s, interval, start_ms, end_ms) for s in syms), return_exceptions=True)
    series = {s: r for s, r in zip(syms, results) if isinstance(r, list) and r}
    for s, rows in series.items():
        base.setdefault(s, rows[0][4])
    by_t: dict[int, list[tuple[float, float, float, float, float]]] = {}
    for s, rows in series.items():
        b = base[s]
        for r in rows:
            by_t.setdefault(r[0], []).append((r[1] / b, r[2] / b, r[3] / b, r[4] / b, r[6]))
    out = []
    for t in sorted(by_t):
        xs = by_t[t]
        if len(xs) < MIN_COMPONENTS:
            continue
        n = len(xs)
        out.append({"time": t // 1000, "open": round(100 * sum(x[0] for x in xs) / n, 4), "high": round(100 * sum(x[1] for x in xs) / n, 4),
                    "low": round(100 * sum(x[2] for x in xs) / n, 4), "close": round(100 * sum(x[3] for x in xs) / n, 4),
                    "volume": round(sum(x[4] for x in xs), 2)})
    return out


async def ticker(now: float | None = None) -> dict:
    """The index's 24h strip: the level now, the change over the last day and the day's range from hourly bars."""
    now = now or time.time()
    lvl, _ = await level(now)
    bars = await candles("1h", int((now - 25 * 3600) * 1000), int(now * 1000))
    last = lvl if lvl is not None else (bars[-1]["close"] if bars else None)
    first = bars[0]["open"] if bars else None
    return {"last": last, "change_pct": round((last / first - 1) * 100, 3) if last and first else 0.0,
            "high": max((b["high"] for b in bars), default=last), "low": min((b["low"] for b in bars), default=last),
            "volume_usd": round(sum(b["volume"] for b in bars), 2), "mark": last, "index": last, "funding_rate": 0.0, "next_funding": 0}


def _auto_wallet():
    from trading import risk_budget
    return risk_budget._wallet("auto")


def _episode(t: dict) -> str | None:
    rid = t.get("reversal_id")
    return str(rid).split(":")[0] if rid else None


def coin_legs(open_only: bool = True) -> dict[str, list[dict]]:
    """The reversal lane's legs on the autotrader's ledger, by slot (the bucket half of reversal_id)."""
    from autotrader import lanes
    w = _auto_wallet()
    if w is None:
        return {}
    rows = w.get_portfolio()["open_trades"] if open_only else w.all_trades()["closed"]
    out: dict[str, list[dict]] = {}
    for t in rows:
        if lanes.lane_of(t) != "reversal":
            continue
        ep = _episode(t)
        if ep:
            out.setdefault(ep, []).append(t)
    return out


def _risk(t: dict) -> float:
    r = t.get("risk_usd")
    if r:
        return float(r)
    plan = t.get("sizing") or {}
    return float(plan.get("notional") or t.get("notional_usd") or 0.0) * float(plan.get("stop_pct") or 0.0) / 100


async def tick(now: float | None = None) -> dict:
    """Mark the book at the index level, close legs whose slot is over, open one for each new slot."""
    from experiments import books
    from trading import book_switches
    now = now or time.time()
    if not books.ENABLED or not book_switches.enabled(BOOK):
        return {"skipped": "off"}
    w = books.WALLETS[BOOK]
    lvl, parts = await level(now)
    if lvl is None:
        return {"skipped": f"index unpriced ({parts['priced']} of {parts['of']} components)"}
    await asyncio.to_thread(w.process_paths, {SYMBOL: {"bars": [], "price": lvl}})
    legs = coin_legs()
    closed, opened = [], []
    for t in w.get_portfolio()["open_trades"]:
        ep = t.get("episode")
        age_h = (now - _ts(t.get("opened_at"))) / 3600
        from autotrader import hold_check
        reason = "episode_over" if ep not in legs else ("hard_cap" if age_h >= hold_check.HARD_CAP_H else None)
        if reason and w.close_trade_manually(t["id"], lvl, reason):
            closed.append({"episode": ep, "reason": reason, "level": lvl})
    handled = set(kv.get_sync(_k("handled")) or [])
    ours = {t.get("episode") for t in w.get_portfolio()["open_trades"]}
    for ep, ts in legs.items():
        if ep in handled or ep in ours:
            continue
        first = min(_ts(t.get("opened_at")) for t in ts)
        handled.add(ep)
        if now - first > FRESH_S:
            logger.info("Reversal index leg: slot %s first seen %.0f min old — not taken", ep, (now - first) / 60)
            continue
        longs = sum(1 for t in ts if t.get("direction") == "long")
        direction = "long" if longs > len(ts) - longs else "short"
        risk = _risk(ts[0])
        if risk <= 0:
            continue
        s = 1 if direction == "long" else -1
        t = w.open_trade(symbol=SYMBOL, direction=direction, entry_price=lvl, stop_loss=lvl * (1 - s * STOP_PCT / 100),
                         take_profit_1=lvl * (1 + s * 0.5), take_profit_2=lvl * (1 + s * 0.6), notional_usd=round(risk / (STOP_PCT / 100), 2),
                         timeframe="index", leverage=LEVERAGE,
                         extra={"strategy": BOOK, "exit_policy": "manual", "shadows": False, "claude_verdict": "skipped", "entry_type": "market",
                                "episode": ep, "coin_legs": [x["symbol"] for x in ts], "coin_risk_usd": round(risk, 2), "index_level": lvl,
                                "components": parts["priced"], "stop_pct": STOP_PCT})
        if t.get("error"):
            logger.info("Reversal index leg for slot %s refused: %s", ep, t["error"])
        else:
            opened.append({"episode": ep, "direction": direction, "level": lvl, "risk_usd": round(risk, 2)})
            logger.info("Reversal index leg: %s %s at %.2f beside %s (risk $%.0f)", direction, SYMBOL, lvl, ", ".join(x["symbol"] for x in ts), risk)
    kv.set_sync(_k("handled"), sorted(handled)[-500:])
    return {"level": lvl, "components": parts, "opened": opened, "closed": closed}


def _stats(xs: list[float]) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0, "mean": None, "t": None}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean": round(m, 4), "t": round(m / (sd / math.sqrt(n)), 2) if sd > 0 and n > 1 else None}


def audit() -> dict:
    """Every closed index leg paired with the coin legs of its slot, in R of each leg's own risk; the pre-registered verdict."""
    from experiments import books
    w = books.WALLETS.get(BOOK)
    if w is None:
        return {"n": 0}
    coins = coin_legs(open_only=False)
    pairs = []
    for t in sorted(w.all_trades()["closed"], key=lambda t: _ts(t.get("closed_at"))):
        ep = t.get("episode")
        cs = [c for c in coins.get(ep, []) if _risk(c) > 0]
        if not ep or not cs or not _risk(t):
            continue
        idx_r = float(t.get("pnl_usd") or 0.0) / _risk(t)
        coin_r = sum(float(c.get("pnl_usd") or 0.0) / _risk(c) for c in cs) / len(cs)
        pairs.append({"episode": ep, "direction": t.get("direction"), "index_r": round(idx_r, 3), "coin_r": round(coin_r, 3), "coins": len(cs),
                      "index_pnl": round(float(t.get("pnl_usd") or 0.0), 2), "coin_pnl": round(sum(float(c.get("pnl_usd") or 0.0) for c in cs), 2),
                      "exit": t.get("close_reason"), "closed_ts": _ts(t.get("closed_at"))})
    diffs = [p["index_r"] - p["coin_r"] for p in pairs]
    st = _stats(diffs)
    half = len(diffs) // 2
    halves = [sum(diffs[:half]) / half if half else None, sum(diffs[half:]) / (len(diffs) - half) if len(diffs) - half else None]
    verdict = "collecting"
    if len(pairs) >= VERDICT_N and st["t"] is not None:
        if st["mean"] >= MIN_GAP_R and st["t"] >= 2 and all(h is not None and h > 0 for h in halves):
            verdict = "index better"
        elif st["mean"] <= -MIN_GAP_R and st["t"] <= -2 and all(h is not None and h < 0 for h in halves):
            verdict = "coin better"
        else:
            verdict = "no difference"
    return {"n": len(pairs), "verdict_n": VERDICT_N, "min_gap_r": MIN_GAP_R, "verdict": verdict,
            "index": _stats([p["index_r"] for p in pairs]), "coin": _stats([p["coin_r"] for p in pairs]), "diff": st,
            "halves": [round(h, 3) if h is not None else None for h in halves],
            "index_pnl": round(sum(p["index_pnl"] for p in pairs), 2), "coin_pnl": round(sum(p["coin_pnl"] for p in pairs), 2),
            "open": len(w.get_portfolio()["open_trades"]), "stop_pct": STOP_PCT, "recent": pairs[-10:]}
