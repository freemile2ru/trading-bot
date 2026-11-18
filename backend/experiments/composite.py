"""One book, the short slices with a record — the single system (2026-11-18, the user: "a single auto trader system that
earns $300 to $500 daily with minimal losses. factor in all possible parameters").

What the ledgers said on 2026-11-17 (docs/research/data-review-2026-11-17.md, the replay in its 11-18 addendum): no
long slice has an edge; the short side of the reversal lane, the weekly, factor, unlock and event lanes, the weekly_rs
book and the copy-stocks book carries all of the programme's positive expectancy — 103 trades locally at +0.119R
(t 2.43), 93 on production at +0.096R (t 2.74). Scaled to one risk per trade and run under the floors below, that set
made $219–$345 a calendar day on $50k with a worst day of −$557/−$665 and a drawdown of −$827/−$805. The target is
$300–$500 a day; the record supports the bottom of it, no more, and only from a per-trade risk of 1% of the book.

The book takes every short those sources open, nothing else, each at the same risk:
  risk          RISK_PCT of equity at the source's stop (1% = $500); a leg's notional never more than NOTIONAL_CAP_PCT of
                equity (a tight stop may not buy 70% of the book — the prop book's AMD trade), the book's gross notional
                never more than GROSS_CAP_PCT, MAX_LEGS legs at once, one per symbol
  exit          the source's disaster stop, the ATR trail once the leg is 1% up (1 daily ATR from the best price), and a
                HOLD_H cap — the prop book's exit, which beat the lane's hold_check on the same picks
  floors        today's loss (realised and floating, against the start-of-day balance) halts new legs at
                HALT_DAILY_PCT and flattens everything at FLATTEN_DAILY_PCT (= the user's 1%); a static drawdown from the
                starting balance halts at HALT_DD_PCT and flattens, then stops the book, at FLATTEN_DD_PCT (= 5%)
  verdict       pre-registered: after VERDICT_LEGS legs and VERDICT_DAYS calendar days, promote when the mean $/calendar
                day ≥ PROMOTE_USD_DAY with the daily P&L's t ≥ 2 and the drawdown never past FLATTEN_DD_PCT; retire at
                RETIRE_LEGS legs with nothing to show. Local only until then.
Every leg stamps its source and the source's trade so the exits can be paired later. Nothing here is a new signal: the
book is the programme's record, filtered to what has worked, sized and floored as one account.
"""
from __future__ import annotations

import logging
import math
import os
import statistics
import time
from datetime import datetime, timezone

from experiments import books
from storage import state as kv
from trading import book_switches, monitor

logger = logging.getLogger("experiments.composite")

BOOK = "composite"
LABEL = "Composite · one book, the short slices with a record"
SIZE = 50_000.0                                      # the plan's money (trading/capital.py)
RISK_PCT = float(os.getenv("COMPOSITE_RISK_PCT", "1.0"))               # of equity at the stop, every leg
NOTIONAL_CAP_PCT = float(os.getenv("COMPOSITE_NOTIONAL_CAP_PCT", "25"))  # of equity, one leg
GROSS_CAP_PCT = float(os.getenv("COMPOSITE_GROSS_CAP_PCT", "150"))      # of equity, all legs
MAX_LEGS = int(os.getenv("COMPOSITE_MAX_LEGS", "10"))
LEVERAGE = int(os.getenv("COMPOSITE_LEVERAGE", "5"))
HOLD_H = float(os.getenv("COMPOSITE_HOLD_H", "72"))
TRAIL_ATR = float(os.getenv("COMPOSITE_TRAIL_ATR", "1.0"))
LOCK_ARM_PCT = 0.01
HALT_DAILY_PCT, FLATTEN_DAILY_PCT = 0.75, 1.0        # of the start-of-day balance
HALT_DD_PCT, FLATTEN_DD_PCT = 4.0, 5.0               # of the starting balance, static
SIDES = ("short",)
SOURCES = {"reversal": "auto:reversal", "weekly": "auto:weekly", "factor": "auto:factor", "unlocks": "auto:unlocks",
           "events": "auto:events", "weekly_rs": "weekly_rs", "copy_stocks": "copy_stocks"}
REVERSAL_STOP_PCT = 10.0                             # the lane's disaster stop, on the picks taken straight from the scores
TARGET_USD_DAY = (300.0, 500.0)
VERDICT_LEGS, VERDICT_DAYS, PROMOTE_USD_DAY, RETIRE_LEGS = 40, 20, 150.0, 60
FRESH_S = 30 * 60                                    # an offer older than this (a restart) is not taken
LOG_KEEP = 300
STATE_KEY = kv.key("composite", "state")
LOG_KEY = kv.key("composite", "log")


def _ts(s) -> float:
    if not s:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def day_key(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def _wallet():
    return books.WALLETS[BOOK]


def _log(entry: dict) -> None:
    rows = (kv.get_sync(LOG_KEY) or []) + [entry]
    kv.set_sync(LOG_KEY, rows[-LOG_KEEP:])


def _fresh(now: float) -> dict:
    return {"started_at": now, "day": day_key(now), "sod_balance": None, "phase": "running", "halted_day": None, "halt_why": None,
            "halts": 0, "flattens": 0, "min_equity": None, "max_equity": None, "stopped": None}


def _state(now: float | None = None) -> dict:
    st = kv.get_sync(STATE_KEY)
    if not st:
        st = _fresh(now or time.time())
        kv.set_sync(STATE_KEY, st)
    return st


def _save(st: dict) -> None:
    kv.set_sync(STATE_KEY, st)


def ledger(w, st: dict) -> dict:
    p = w.get_portfolio(closed_limit=5000, history_limit=1)
    closed = [t for t in p["closed_trades"] if _ts(t.get("closed_at")) >= st["started_at"]]
    return {"balance": p["balance"], "equity": p["total_equity"], "open_pnl": p["open_pnl"], "open": p["open_trades"], "closed": closed,
            "gross": p["notional_open"]}


def funding(led: dict) -> bool:
    """The capital plan hasn't filled the wallet to the book's size yet: nothing trades until it has."""
    return not led["closed"] and not led["open"] and led["equity"] < SIZE * 0.98


# ── The floors ─────────────────────────────────────────────────────────────────

def floors(sod_balance: float) -> dict[str, float]:
    return {"halt_daily": sod_balance * (1 - HALT_DAILY_PCT / 100), "flatten_daily": sod_balance * (1 - FLATTEN_DAILY_PCT / 100),
            "halt_dd": SIZE * (1 - HALT_DD_PCT / 100), "flatten_dd": SIZE * (1 - FLATTEN_DD_PCT / 100)}


def assess(equity: float, sod_balance: float) -> dict:
    """Where the book stands against its four floors: ok / halt / flatten, today's loss and the drawdown in % and in
    % of the limit used, and the dollars of room left."""
    f = floors(sod_balance)
    daily_loss_pct = max(0.0, (sod_balance - equity) / sod_balance * 100) if sod_balance else 0.0
    dd_pct = max(0.0, (SIZE - equity) / SIZE * 100)
    if equity <= f["flatten_daily"] or equity <= f["flatten_dd"]:
        action, why = "flatten", (f"today's loss {daily_loss_pct:.2f}% ≥ {FLATTEN_DAILY_PCT}%" if equity <= f["flatten_daily"]
                                  else f"drawdown {dd_pct:.2f}% ≥ {FLATTEN_DD_PCT}%")
    elif equity <= f["halt_daily"] or equity <= f["halt_dd"]:
        action, why = "halt", (f"today's loss {daily_loss_pct:.2f}% ≥ {HALT_DAILY_PCT}%" if equity <= f["halt_daily"]
                               else f"drawdown {dd_pct:.2f}% ≥ {HALT_DD_PCT}%")
    else:
        action, why = "ok", None
    return {"action": action, "why": why, "daily_loss_pct": round(daily_loss_pct, 3), "dd_pct": round(dd_pct, 3),
            "daily_used_pct": round(daily_loss_pct / FLATTEN_DAILY_PCT * 100, 1), "dd_used_pct": round(dd_pct / FLATTEN_DD_PCT * 100, 1),
            "room_today_usd": round(equity - f["flatten_daily"], 2), "room_total_usd": round(equity - f["flatten_dd"], 2),
            "floors": {k: round(v, 2) for k, v in f.items()}}


# ── Sizing ─────────────────────────────────────────────────────────────────────

def size(equity: float, px: float, stop_px: float, gross_open: float) -> dict:
    """A leg's notional: RISK_PCT of equity at the stop, never more than NOTIONAL_CAP_PCT of equity, never past the
    gross cap. `wanted` is what the risk rule asked for, before the caps."""
    stop_frac = abs(px - stop_px) / px if px else 0.0
    if stop_frac <= 0:
        return {"notional": 0.0, "wanted": 0.0, "risk_usd": 0.0, "bound": None, "stop_frac": 0.0}
    wanted = equity * RISK_PCT / 100 / stop_frac
    cap = equity * NOTIONAL_CAP_PCT / 100
    room = max(0.0, equity * GROSS_CAP_PCT / 100 - gross_open)
    notional = min(wanted, cap, room)
    bound = "notional_cap" if wanted > cap and cap <= room else "gross_cap" if wanted > room else None
    return {"notional": round(notional, 2), "wanted": round(wanted, 2), "risk_usd": round(notional * stop_frac, 2), "bound": bound,
            "stop_frac": stop_frac}


def can_enter(st: dict, led: dict, a: dict, source: str, symbol: str, direction: str) -> str | None:
    """Why the book would refuse a leg now, or None."""
    if source not in SOURCES:
        return f"{source} is not one of the book's sources"
    if direction not in SIDES:
        return f"{direction}s are not taken"
    if st["phase"] != "running":
        return f"the book is {st['phase']}: {(st.get('stopped') or {}).get('why')}"
    if funding(led):
        return "waiting for the capital plan to fund the wallet"
    if st.get("halted_day") == day_key(time.time()):
        return f"halted for the day: {st.get('halt_why')}"
    if a["action"] != "ok":
        return f"{a['action']}: {a['why']}"
    if len(led["open"]) >= MAX_LEGS:
        return f"{MAX_LEGS} legs are on"
    if any(t["symbol"] == symbol for t in led["open"]):
        return f"{symbol} already open"
    return None


# ── Entries ────────────────────────────────────────────────────────────────────

async def offer(source: str, symbol: str, direction: str, px: float, stop_px: float, why: str, extra: dict | None = None,
                now: float | None = None) -> dict | None:
    """A source opened (or picked) a leg: take it the book's way, or write down why not."""
    now = now or time.time()
    if not (books.ENABLED and book_switches.enabled(BOOK)):
        return None
    if extra and extra.get("offered_at") and now - extra["offered_at"] > FRESH_S:
        return None
    w = _wallet()
    st = _state(now)
    led = ledger(w, st)
    a = assess(led["equity"], st.get("sod_balance") or led["balance"])
    refused = can_enter(st, led, a, source, symbol, direction)
    if refused:
        if direction in SIDES:                                       # the longs are not the book's business; no need to log each
            _log({"ts": now, "event": "refused", "source": source, "symbol": symbol, "direction": direction, "why": refused})
        return None
    sz = size(led["equity"], px, stop_px, led["gross"])
    if sz["notional"] < 10:
        _log({"ts": now, "event": "refused", "source": source, "symbol": symbol, "direction": direction, "why": f"size under $10 ({sz['bound']})"})
        return None
    s = 1 if direction == "long" else -1
    atr = None
    try:
        from autotrader import sizing
        atr = await sizing.daily_atr_pct(symbol, now)
    except Exception:
        pass
    t = w.open_trade(symbol=symbol, direction=direction, entry_price=px, stop_loss=stop_px, take_profit_1=px * (1 + s * 0.9), take_profit_2=px * (1 + s * 0.95),
                     notional_usd=sz["notional"], leverage=LEVERAGE, timeframe="1d", oracle_confidence=0, oracle_reasoning=why[:200],
                     hold_duration=f"the ATR trail, {HOLD_H:g}h cap",
                     extra={"strategy": BOOK, "source": source, "source_book": SOURCES[source], "exit_policy": "manual_lock", "lock_arm_pct": LOCK_ARM_PCT,
                            "lock_trail_atr": TRAIL_ATR, "atr_pct": atr, "shadows": False, "claude_verdict": "skipped", "entry_type": "market",
                            "risk_pct": RISK_PCT, "sizing_cap": {"bound": sz["bound"], "wanted_notional": sz["wanted"], "notional_cap_pct": NOTIONAL_CAP_PCT},
                            "equity_at_entry": led["equity"], "day": day_key(now), **(extra or {})})
    if t.get("error"):
        _log({"ts": now, "event": "refused", "source": source, "symbol": symbol, "direction": direction, "why": t["error"][:120]})
        return None
    w.annotate(t["id"], expires_at=now + HOLD_H * 3600)
    _log({"ts": now, "event": "opened", "source": source, "symbol": symbol, "direction": direction, "notional": sz["notional"], "risk_usd": sz["risk_usd"],
          "bound": sz["bound"], "stop": stop_px, "equity": led["equity"]})
    logger.info("Composite %s %s from %s: $%.0f at %.4g, stop %.4g (%s)", direction, symbol, source, sz["notional"], px, stop_px, sz["bound"] or "risk-sized")
    return t


async def mirror_reversal(picks: list[tuple[str, str]], scores: dict[str, float], now: float | None = None) -> list[dict]:
    """The reversal lane's picks of this 4-hour slot, the short ones, at the lane's disaster stop."""
    now = now or time.time()
    out = []
    for sym, direction in picks:
        if direction not in SIDES:
            continue
        px = await monitor.live_price(sym)
        if not px:
            continue
        s = 1 if direction == "long" else -1
        t = await offer("reversal", sym, direction, px, px * (1 - s * REVERSAL_STOP_PCT / 100),
                        f"reversal score {scores.get(sym, 0):+.3f}: {direction} {sym}", {"reversal_score": scores.get(sym)}, now)
        if t:
            out.append(t)
    return out


async def mirror(source: str, t: dict, now: float | None = None) -> dict | None:
    """A source book's trade as it opened: the same symbol, side, entry and stop, the book's size and exit."""
    if not t or t.get("error") or t.get("direction") not in SIDES:
        return None
    return await offer(source, t["symbol"], t["direction"], float(t["entry_price"]), float(t["stop_loss"]),
                       f"{source}: {t.get('oracle_reasoning') or t.get('entry_note') or ''}"[:200],
                       {"source_trade_id": t.get("id"), "offered_at": _ts(t.get("opened_at")) or (now or time.time())}, now)


# ── The clock ──────────────────────────────────────────────────────────────────

async def flatten(reason: str, now: float) -> list[dict]:
    w = _wallet()
    out = []
    for t in w.get_portfolio()["open_trades"]:
        price = await monitor.live_price(t["symbol"]) or t.get("current_price") or t["entry_price"]
        done = w.close_trade_manually(t["id"], price, f"composite: {reason}")
        if done:
            out.append({"symbol": t["symbol"], "pnl_usd": done.get("pnl_usd")})
    return out


async def tick(now: float | None = None) -> dict:
    """Every minute: roll the day, read the book against its floors, act."""
    now = now or time.time()
    if not books.ENABLED:
        return {}
    w = _wallet()
    st = _state(now)
    led = ledger(w, st)
    today = day_key(now)
    if st.get("day") != today or st.get("sod_balance") is None:
        st["day"], st["sod_balance"], st["halted_day"], st["halt_why"] = today, led["balance"], None, None
        _log({"ts": now, "event": "day", "sod_balance": led["balance"]})
    a = assess(led["equity"], st["sod_balance"])
    st["min_equity"] = min(st.get("min_equity") or led["equity"], led["equity"])
    st["max_equity"] = max(st.get("max_equity") or led["equity"], led["equity"])
    acted = None
    if st["phase"] == "running" and not funding(led):
        if a["action"] == "flatten" and led["open"]:
            closed = await flatten(a["why"], now)
            st["flattens"] = st.get("flattens", 0) + 1
            acted = {"flattened": closed, "why": a["why"]}
            _log({"ts": now, "event": "flatten", "why": a["why"], "closed": closed, "equity": led["equity"]})
        if a["action"] in ("halt", "flatten") and st.get("halted_day") != today:
            st["halted_day"], st["halt_why"] = today, a["why"]
            st["halts"] = st.get("halts", 0) + 1
            _log({"ts": now, "event": "halt", "why": a["why"], "equity": led["equity"]})
        if led["equity"] <= a["floors"]["flatten_dd"]:
            st["phase"], st["stopped"] = "stopped", {"ts": now, "why": f"drawdown {a['dd_pct']:.2f}% ≥ {FLATTEN_DD_PCT}%", "equity": led["equity"]}
            _log({"ts": now, "event": "stopped", "why": f"{FLATTEN_DD_PCT}% drawdown — the book stops until it is reset", "equity": led["equity"]})
    _save(st)
    return {"assess": a, "equity": led["equity"], "acted": acted, "phase": st["phase"]}


async def reset(note: str = "reset") -> dict:
    """Start again from the plan's money: open legs closed, the floors re-based, the record kept on the ledger."""
    now = time.time()
    closed = await flatten(note, now)
    st = _fresh(now)
    _save(st)
    _log({"ts": now, "event": "reset", "note": note, "closed": closed})
    return st


# ── The record ─────────────────────────────────────────────────────────────────

def _daily(closed: list[dict], started_at: float, now: float) -> dict:
    by: dict[str, float] = {}
    for t in closed:
        by[day_key(_ts(t["closed_at"]))] = by.get(day_key(_ts(t["closed_at"])), 0.0) + (t.get("pnl_usd") or 0)
    vals = [by[d] for d in sorted(by)]
    cal_days = max(1, int((now - started_at) / 86400))
    cum = pk = mdd = 0.0
    for v in vals:
        cum += v; pk = max(pk, cum); mdd = min(mdd, cum - pk)
    sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    t_stat = (statistics.mean(vals) / (sd / math.sqrt(len(vals)))) if sd and len(vals) > 1 else None
    return {"days": [{"day": d, "pnl": round(by[d], 2)} for d in sorted(by)][-30:], "trading_days": len(vals), "cal_days": cal_days,
            "net": round(sum(vals), 2), "per_cal_day": round(sum(vals) / cal_days, 2), "per_trading_day": round(statistics.mean(vals), 2) if vals else 0.0,
            "median_day": round(statistics.median(vals), 2) if vals else 0.0, "pos_days_pct": round(100 * sum(1 for v in vals if v > 0) / len(vals), 1) if vals else 0.0,
            "worst_day": round(min(vals), 2) if vals else 0.0, "best_day": round(max(vals), 2) if vals else 0.0, "max_dd": round(mdd, 2),
            "t": round(t_stat, 2) if t_stat is not None else None}


def verdict(closed: list[dict], daily: dict, st: dict) -> dict:
    n = len(closed)
    if st["phase"] == "stopped":
        return {"status": "stopped", "note": f"the book hit its {FLATTEN_DD_PCT}% drawdown floor — retire or reset", "legs": n, "days": daily["cal_days"]}
    if n < VERDICT_LEGS or daily["cal_days"] < VERDICT_DAYS:
        return {"status": "collecting", "note": f"{n}/{VERDICT_LEGS} legs, {daily['cal_days']}/{VERDICT_DAYS} days — no verdict before both",
                "legs": n, "days": daily["cal_days"]}
    if daily["per_cal_day"] >= PROMOTE_USD_DAY and (daily["t"] or 0) >= 2:
        return {"status": "promote", "note": f"${daily['per_cal_day']:.0f}/day over {daily['cal_days']} days, t {daily['t']}: the case for production",
                "legs": n, "days": daily["cal_days"]}
    if n >= RETIRE_LEGS and daily["per_cal_day"] <= 0:
        return {"status": "retire", "note": f"${daily['per_cal_day']:.0f}/day after {n} legs", "legs": n, "days": daily["cal_days"]}
    return {"status": "collecting", "note": f"${daily['per_cal_day']:.0f}/day (t {daily['t']}) — short of ${PROMOTE_USD_DAY:.0f}/day with t ≥ 2; retire at {RETIRE_LEGS} legs if ≤ $0",
            "legs": n, "days": daily["cal_days"]}


def status() -> dict:
    now = time.time()
    w = _wallet()
    st = _state(now)
    led = ledger(w, st)
    sod = st.get("sod_balance") or led["balance"]
    a = assess(led["equity"], sod)
    closed = led["closed"]
    daily = _daily(closed, st["started_at"], now)
    by_source = {}
    for src in SOURCES:
        mine = [t for t in closed if t.get("source") == src]
        R = [(t.get("pnl_usd") or 0) / t["risk_usd"] for t in mine if t.get("risk_usd")]
        by_source[src] = {"n": len(mine), "pnl": round(sum(t.get("pnl_usd") or 0 for t in mine), 2), "wins": sum(1 for t in mine if (t.get("pnl_usd") or 0) > 0),
                          "mean_r": round(statistics.mean(R), 3) if R else None, "open": sum(1 for t in led["open"] if t.get("source") == src),
                          "book": SOURCES[src]}
    today_pnl = round(led["equity"] - sod, 2)
    return {
        "label": LABEL, "size": SIZE, "phase": "funding" if funding(led) else st["phase"], "started_at": st["started_at"], "days": round((now - st["started_at"]) / 86400, 1),
        "balance": led["balance"], "equity": led["equity"], "open_pnl": led["open_pnl"], "sod_balance": sod, "today": today_pnl,
        "min_equity": st.get("min_equity"), "max_equity": st.get("max_equity"), "stopped": st.get("stopped"),
        "assess": a, "halted_day": st.get("halted_day"), "halt_why": st.get("halt_why"), "halts": st.get("halts", 0), "flattens": st.get("flattens", 0),
        "target": {"low": TARGET_USD_DAY[0], "high": TARGET_USD_DAY[1], "per_cal_day": daily["per_cal_day"],
                   "progress_pct": round(100 * daily["per_cal_day"] / TARGET_USD_DAY[0], 1) if daily["per_cal_day"] > 0 else 0.0},
        "daily": daily, "verdict": verdict(closed, daily, st),
        "rules": {"risk_pct": RISK_PCT, "notional_cap_pct": NOTIONAL_CAP_PCT, "gross_cap_pct": GROSS_CAP_PCT, "max_legs": MAX_LEGS, "leverage": LEVERAGE,
                  "hold_h": HOLD_H, "trail_atr": TRAIL_ATR, "halt_daily_pct": HALT_DAILY_PCT, "flatten_daily_pct": FLATTEN_DAILY_PCT,
                  "halt_dd_pct": HALT_DD_PCT, "flatten_dd_pct": FLATTEN_DD_PCT, "sides": list(SIDES), "sources": SOURCES,
                  "verdict": {"legs": VERDICT_LEGS, "days": VERDICT_DAYS, "promote_usd_day": PROMOTE_USD_DAY, "retire_legs": RETIRE_LEGS},
                  "risk_usd": round(SIZE * RISK_PCT / 100, 2), "notional_cap_usd": round(SIZE * NOTIONAL_CAP_PCT / 100, 2)},
        "open": [{"id": t["id"], "symbol": t["symbol"], "direction": t["direction"], "source": t.get("source"), "notional": t.get("notional_usd"),
                  "risk_usd": t.get("risk_usd"), "entry": t["entry_price"], "stop": t.get("stop_loss"), "pnl_usd": t.get("pnl_usd"), "opened_at": t.get("opened_at"),
                  "bound": (t.get("sizing_cap") or {}).get("bound")} for t in led["open"]],
        "closed_n": len(closed), "closed_pnl": round(sum(t.get("pnl_usd") or 0 for t in closed), 2), "wins": sum(1 for t in closed if (t.get("pnl_usd") or 0) > 0),
        "by_source": by_source,
        "recent": [{"symbol": t["symbol"], "direction": t["direction"], "source": t.get("source"), "pnl_usd": t.get("pnl_usd"), "risk_usd": t.get("risk_usd"),
                    "close_reason": t.get("close_reason"), "closed_at": t.get("closed_at")} for t in closed[-15:]][::-1],
        "log": (kv.get_sync(LOG_KEY) or [])[-60:][::-1],
    }


def config() -> dict:
    return status()["rules"]
