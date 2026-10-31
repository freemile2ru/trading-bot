"""Auction-level book — the mentor's NQ system (trading/auction_levels.py) traded forward on stock and index perps, on paper.

Why (2026-10-31): the replay (research/auction_lab.py) was breakeven before costs and lost 0.13% a trade at ours even
with the filters built around its weaknesses — but the user wants it tested forward, with fundamentals considered, so
it runs at measuring size and is judged like every book, on its own 40 trades.

The book (books.WALLETS["auction"]), every closed 5-minute bar in the New York session (09:30–17:00 ET):
  setup     the study's acceptance → confirmation → entry sequence (VARIANT: target ≥ MIN_TARGET_BPS away, grade ≥ MIN_GRADE)
  gates     calendar first — nothing inside a macro-release blackout (FOMC, CPI, NFP…) or an earnings window for the
            name (ops/calendar); the trade is tagged with any nearby release for the exit-reason record
  size      RISK_PCT of equity at the stop, at most MAX_GROSS × equity notional, leverage from the stop distance
  exits     the target (take-profit), the stop, a close back through the level (this book), the session's end (expiry)
  one trade per symbol, MAX_OPEN at once
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from storage import state as kv
from trading import book_switches, monitor
from trading import auction_levels as AL

logger = logging.getLogger("experiments.auction")

BOOK = "auction"
LABEL = "Auction levels (mentor's NQ system, stock & index perps)"
SYMBOLS = tuple(os.getenv("AUCTION_SYMBOLS", "QQQUSDT,NVDAUSDT,TSLAUSDT,AMDUSDT").split(","))
VARIANT = {"min_target_bps": float(os.getenv("AUCTION_MIN_TARGET_BPS", "50")), "min_grade": int(os.getenv("AUCTION_MIN_GRADE", "7"))}
RISK_PCT = float(os.getenv("AUCTION_RISK_PCT", "1.0"))
MAX_GROSS = float(os.getenv("AUCTION_MAX_GROSS", "3.0"))
MAX_LEVERAGE = int(os.getenv("AUCTION_MAX_LEVERAGE", "5"))
MAX_OPEN = int(os.getenv("AUCTION_MAX_OPEN", "4"))
HISTORY_DAYS = 10                      # enough closed 30-minute candles for the HTF ATR and a prior week for its levels
BAR_S = 300
LOG_KEEP = 60
ET = ZoneInfo("America/New_York")


def _k(*parts: str) -> str:
    return kv.key("auction", *parts)


def _log(entry: dict) -> None:
    rows = (kv.get_sync(_k("log")) or []) + [entry]
    kv.set_sync(_k("log"), rows[-LOG_KEEP:], 7 * 86400)


def _state(sym: str) -> dict:
    return kv.get_sync(_k("state", sym)) or {}


def _set_state(sym: str, st: dict) -> None:
    kv.set_sync(_k("state", sym), st, 3 * 86400)


async def bars(sym: str, now: float) -> pd.DataFrame:
    """Closed 5-minute bars for the last HISTORY_DAYS (the forming bar is dropped)."""
    from data.derivatives_client import fetch_klines_range
    end = int(now * 1000)
    rows = await fetch_klines_range(sym, "5m", end - HISTORY_DAYS * 86400 * 1000, end)
    rows = [r for r in rows if r[0] + BAR_S * 1000 <= end]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "quote"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def gates(sym: str, now: float) -> str | None:
    """Fundamentals before a trade: the calendar. A macro release inside its blackout, or earnings for the name, says no."""
    from ops import calendar
    why = calendar.macro_blackout(now)
    if why:
        return f"macro blackout: {why}"
    why = calendar.earnings_block(sym, now)
    if why:
        return f"earnings: {why}"
    return None


def _size(equity: float, entry: float, stop: float) -> tuple[float, int]:
    risk_frac = abs(entry - stop) / entry if entry else 0.0
    if risk_frac <= 0:
        return 0.0, 1
    notional = min(equity * RISK_PCT / 100 / risk_frac, equity * MAX_GROSS)
    from trading import futures
    lev = max(1, min(MAX_LEVERAGE, int(futures.safe_leverage(risk_frac * 100, "QQQUSDT"))))
    return round(notional, 2), lev


async def step(sym: str, df: pd.DataFrame, now: float) -> dict | None:
    """Apply the study's sequence to the latest closed bar of one symbol. Returns what happened, or None."""
    from experiments import books
    if len(df) < 300:
        return None
    d = AL.session_frame(df)
    i = len(d) - 1
    r = d.iloc[i]
    ts = d.index[i]
    bar_key = int(ts.timestamp())
    st = _state(sym)
    if st.get("bar") == bar_key:
        return None                                              # this closed bar is already decided
    st["bar"] = bar_key
    w = books.WALLETS[BOOK]
    open_trades = w.get_portfolio()["open_trades"]
    mine = next((t for t in open_trades if t["symbol"] == sym), None)
    out: dict | None = None
    # 1. an open trade: the study's exit back through the level (stop, target and session end are on the wallet)
    if mine:
        level, s = mine.get("level_px"), 1 if mine["direction"] == "long" else -1
        if level and ((s > 0 and r["close"] < level) or (s < 0 and r["close"] > level)):
            px = await monitor.live_price(sym) or float(r["close"])
            if w.close_trade_manually(mine["id"], px, "exit_through_level"):
                out = {"sym": sym, "event": "exit_through_level", "price": px}
        st["pending"], st["entering"] = None, None
        _set_state(sym, st)
        return out
    # 2. confirmed last bar: enter now (this bar's open is behind us; the live price is the next best thing)
    entering = st.get("entering")
    if entering:
        st["entering"] = None
        if r["in_session"] and len(open_trades) < MAX_OPEN and book_switches.enabled(BOOK) and books.ENABLED:
            why = gates(sym, now)
            if why:
                _log({"ts": now, "sym": sym, "event": "gated", "why": why, "level": entering["level_name"]})
            else:
                px = await monitor.live_price(sym) or float(r["open"])
                equity = w.get_portfolio()["total_equity"]
                notional, lev = _size(equity, px, entering["stop"])
                sd = 1 if entering["direction"] == "long" else -1
                good = (entering["stop"] < px < entering["target"]) if sd > 0 else (entering["target"] < px < entering["stop"])
                if notional >= 10 and good:
                    from ops import calendar
                    end_et = datetime.fromtimestamp(now, ET).replace(hour=AL.SESSION_END[0], minute=AL.SESSION_END[1], second=0, microsecond=0)
                    t = w.open_trade(symbol=sym, direction=entering["direction"], entry_price=px, stop_loss=entering["stop"],
                                     take_profit_1=entering["target"], take_profit_2=entering["target"], notional_usd=notional, timeframe="5m",
                                     leverage=lev, oracle_confidence=0,
                                     oracle_reasoning=f"auction: accepted {entering['level_name']} @ {entering['level']:.4g}, grade {entering['grade']}/8, "
                                                      f"target {entering['target_name']} @ {entering['target']:.4g} ({entering['target_bps']:.0f} bps)",
                                     hold_duration="to the next level, the stop or 17:00 ET",
                                     extra={"strategy": BOOK, "exit_policy": "manual", "shadows": False, "claude_verdict": "skipped", "entry_type": "market",
                                            "level_name": entering["level_name"], "level_px": entering["level"], "target_name": entering["target_name"],
                                            "grade": entering["grade"], "context": entering["context"], "near_event": calendar.near_event(sym, now),
                                            "variant": VARIANT})
                    if t.get("error"):
                        _log({"ts": now, "sym": sym, "event": "refused", "why": t["error"]})
                    else:
                        w.annotate(t["id"], expires_at=end_et.timestamp())
                        out = {"sym": sym, "event": "opened", "trade": t}
                        _log({"ts": now, "sym": sym, "event": "opened", "direction": t["direction"], "level": entering["level_name"], "grade": entering["grade"],
                              "target": entering["target_name"], "target_bps": entering["target_bps"], "notional": notional})
                else:
                    _log({"ts": now, "sym": sym, "event": "skipped", "why": "price already past the level or target"})
        _set_state(sym, st)
        return out
    # 3. accepted last bar: confirmation on this bar
    pending = st.get("pending")
    if pending:
        st["pending"] = None
        held = r["close"] > pending["level"] if pending["direction"] == "long" else r["close"] < pending["level"]
        if held and r["in_session"]:
            ctx = AL.context(d, i)
            g, stand_down, reasons = AL.grade(ctx, 1 if pending["direction"] == "long" else -1)
            target_bps = abs(pending["target"] / r["close"] - 1) * 1e4
            if stand_down:
                _log({"ts": now, "sym": sym, "event": "stand_down", "why": " ".join(reasons), "level": pending["level_name"], "grade": g})
            elif g < VARIANT["min_grade"]:
                _log({"ts": now, "sym": sym, "event": "skipped", "why": f"grade {g} < {VARIANT['min_grade']}", "level": pending["level_name"]})
            elif target_bps < VARIANT["min_target_bps"]:
                _log({"ts": now, "sym": sym, "event": "skipped", "why": f"target only {target_bps:.0f} bps away", "level": pending["level_name"]})
            else:
                st["entering"] = {**pending, "grade": g, "target_bps": round(target_bps, 1), "context": {k: (v if not isinstance(v, float) else round(v, 4)) for k, v in ctx.items()}}
                out = {"sym": sym, "event": "confirmed", "level": pending["level_name"], "grade": g}
        else:
            _log({"ts": now, "sym": sym, "event": "not_confirmed", "level": pending["level_name"]})
        _set_state(sym, st)
        return out
    # 4. acceptance on this bar
    if r["in_session"]:
        import numpy as np
        buf = r["close"] * AL.BUFFER_BPS / 1e4
        prev_c = float(d["close"].iloc[i - 1])
        ups = [(r[n], n) for n in AL.UP_LEVELS if np.isfinite(r[n]) and r["close"] > r[n] + buf and prev_c <= r[n] + buf]
        dns = [(r[n], n) for n in AL.DOWN_LEVELS if np.isfinite(r[n]) and r["close"] < r[n] - buf and prev_c >= r[n] - buf]
        if ups or dns:
            up_lv, dn_lv = AL.next_levels(r)
            off = r["close"] * AL.STOP_OFFSET_BPS / 1e4
            if ups:
                level, name = max(ups)
                st["pending"] = {"direction": "long", "level": float(level), "level_name": name, "stop": float(r["low"] - off),
                                 "target": float(up_lv[0]), "target_name": up_lv[1], "accept_ts": bar_key}
            else:
                level, name = min(dns)
                st["pending"] = {"direction": "short", "level": float(level), "level_name": name, "stop": float(r["high"] + off),
                                 "target": float(dn_lv[0]), "target_name": dn_lv[1], "accept_ts": bar_key}
            out = {"sym": sym, "event": "accepted", **{k: st["pending"][k] for k in ("direction", "level_name", "target_name")}}
    _set_state(sym, st)
    return out


async def tick(now: float | None = None) -> list[dict]:
    """Every minute: in the New York session, once each 5-minute bar closes, run the sequence for every symbol."""
    from experiments import books
    now = now or time.time()
    if not books.ENABLED or not book_switches.enabled(BOOK):
        return []
    et = datetime.fromtimestamp(now, ET)
    hm = (et.hour, et.minute)
    if not (AL.SESSION_OPEN <= hm < (AL.SESSION_END[0], AL.SESSION_END[1] + 5)) or et.weekday() >= 5:
        return []
    if int(now) % BAR_S > 90:
        return []                                                 # act in the first 90 seconds after a bar closes
    out = []
    for sym in SYMBOLS:
        try:
            df = await bars(sym, now)
            ev = await step(sym, df, now)
            if ev:
                out.append(ev)
                if ev["event"] in ("opened", "exit_through_level"):
                    logger.info("Auction %s: %s", sym, ev)
        except Exception as e:
            logger.warning("Auction %s failed: %s", sym, e)
    return out


def status() -> dict:
    return {"symbols": list(SYMBOLS), "variant": VARIANT, "risk_pct": RISK_PCT, "max_gross": MAX_GROSS, "max_open": MAX_OPEN,
            "states": {s: {k: v for k, v in _state(s).items() if k != "bar"} for s in SYMBOLS},
            "log": list(reversed(kv.get_sync(_k("log")) or []))[:30]}
