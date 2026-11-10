"""Auction-level book — the mentor's NQ system (trading/auction_levels.py) traded forward on stock and index perps, on paper.

Why (2026-10-31): the replay (research/auction_lab.py) was breakeven before costs and lost 0.13% a trade at ours even
with the filters built around its weaknesses — but the user wants it tested forward, with fundamentals considered, so
it runs at measuring size and is judged like every book, on its own 40 trades.

The book (books.WALLETS["auction"]), every closed 5-minute bar in the New York cash session (09:30–16:00 ET):
  setup     the study's acceptance → confirmation → entry sequence (VARIANT: target ≥ MIN_TARGET_BPS away, grade ≥ MIN_GRADE)
  shadows   from 2026-11-07 the grade-6 and next-open arrangements (and the base one) are scored daily on the real bars (VARIANTS_FORWARD)
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
from trading import book_switches, capital, monitor
from trading import auction_levels as AL

logger = logging.getLogger("experiments.auction")

BOOK = "auction"
LABEL = "Auction levels (mentor's NQ system, stock & index perps)"
SYMBOLS = tuple(os.getenv("AUCTION_SYMBOLS", "QQQUSDT,NVDAUSDT,TSLAUSDT,AMDUSDT").split(","))
# 2026-11-01, the mentor's reading of his study — "wait for confirmation, then the best entry and exit": the entry is the
# RETEST (price comes back to the level within 3 bars and closes beyond it again; stop under that bar), the target is the
# level BEYOND the nearest. In the replay that was the only arrangement positive in both halves at futures costs
# (+0.017% a trade); at Binance costs it still loses (−0.13%), so the minimum target distance and grade stay as gates.
VARIANT = {"min_target_bps": float(os.getenv("AUCTION_MIN_TARGET_BPS", "50")), "min_grade": int(os.getenv("AUCTION_MIN_GRADE", "7")),
           "entry": os.getenv("AUCTION_ENTRY", "retest"), "target_mode": os.getenv("AUCTION_TARGET", "second")}
# 2026-11-07: forward shadows of the arrangements the log kept skipping. In its first week the base rule took nothing on
# four instruments (40 setups: 15 never confirmed, 8 no retest, 8 graded 6 against the floor of 7, 6 stand-downs); at that
# rate its 40-trade verdict never comes. Each session day, once the cash close has passed, the study's own sequence
# (trading/auction_levels.simulate) is run over the day's real bars under each arrangement and the trades it finds are
# recorded through the book's level and entry-cutoff gates, costed at the paper wallet's rate, and scored on the same
# 40-trade verdict. The fills are the bar's close (no slippage) and the study's 17:00 session end, so the base
# arrangement's own shadow beside the live book shows what the fills and the other gates cost. Registered before the first
# session it scores (Monday 2026-11-09); the live book is unchanged.
VARIANT_COST_BPS = 8.0                                            # a side: taker fee + slippage (research/auction_lab.COST_BPS)
VARIANTS_KEEP = 2000
RISK_PCT = float(os.getenv("AUCTION_RISK_PCT", "1.0"))           # 1% of the book at the stop (2026-11-10, was 2 from 11-01): $500 a stop
                                                                 # was 3× a copy stop on a replay that is net negative; back to 1 until the
                                                                 # chase-gate shadow reaches its verdict
MAX_GROSS = float(os.getenv("AUCTION_MAX_GROSS", "2.0"))         # a trade is at most 2× the book in notional (2026-11-01, was 3): $60k on $30k
MAX_LEVERAGE = int(os.getenv("AUCTION_MAX_LEVERAGE", "5"))
MAX_OPEN = int(os.getenv("AUCTION_MAX_OPEN", "3"))               # 3 at once (was 4): at 2× each, three fill the book's margin at 5×
DAILY_GOAL = (float(os.getenv("AUCTION_GOAL_LOW", "200")), float(os.getenv("AUCTION_GOAL_HIGH", "500")))   # the user's $/day goal on $30k


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _m(hm: tuple[int, int]) -> int:
    return hm[0] * 60 + hm[1]


# 2026-11-05, after the first production trade: a 15:55 ET short into NVDA's closing auction, filled 45 bps past the level
# (the replay enters at the next open, a median 4–10 bps past it), exited on after-hours prints. These are stock and ETF
# perps, so the session ends at the cash close (the study's 17:00 is NQ futures time); nothing is accepted or entered from
# the entry cutoff on (the replay: every slot after 14:00 ET lost — 44 trades, 11% won); a fill that has already run past
# the bar's close by more than MAX_PAST_BPS is not taken; the weekly levels are left out (13 replayed trades, 1 win). The
# size stays real-world (the user's call — the book is paper). The replay's only positive slot is the first hour, so each
# trade is tagged with its slot and the verdict shows the first hour on its own.
SESSION_END = _hm(os.getenv("AUCTION_SESSION_END", "16:00"))
ENTRY_CUTOFF = _hm(os.getenv("AUCTION_ENTRY_CUTOFF", "14:00"))
MAX_PAST_BPS = float(os.getenv("AUCTION_MAX_PAST_BPS", "15"))
SKIP_LEVELS = {x.strip() for x in os.getenv("AUCTION_SKIP_LEVELS", "WKH,WKL,IBH,IBL").split(",") if x.strip()}
FIRST_HOUR_END = (10, 30)
# 2026-11-05, after the second production trade: an 11:05 ET NVDA short at the initial-balance low with the stock already
# 8% down on the morning and a 73 bps stop on a day of 1% bars (0.7 ATR) — stopped in four minutes. The replay (250
# trades): a stop under 1 ATR lost (−0.05%, 60% stopped) and one of 1 ATR or more made +0.04%; an acceptance with price
# already more than 2% from the session open in the trade's direction lost in both halves (−0.16%); the initial-balance
# high and low were the worst level family (−0.14%, both halves) — they stay as targets, not as levels to accept.
MIN_STOP_ATR = float(os.getenv("AUCTION_MIN_STOP_ATR", "1.0"))        # the stop must sit at least this many 14-bar ATRs from the entry
MAX_FROM_OPEN_PCT = float(os.getenv("AUCTION_MAX_FROM_OPEN_PCT", "2.0"))   # no acceptance once price is this far from the session open, the trade's way
# 2026-11-09, after a gap-and-go session (AMD +4%, TSLA +5%, NVDA +3% by mid-morning) on which the book took nothing — QQQ
# confirmed twice and never retested, the other three were refused by the 2%-from-the-open chase gate. Two candidate
# arrangements were written and replayed first (research/auction_trend_lab.py, 214 sessions of the four names):
# "chase_gate", the base arrangement plus the live book's gate, goes forward (base − chase_gate is what the gate costs or
# saves; in the replay the gate removed net losers). "trend_day" (trading/auction_levels.simulate_trend_day — beyond the
# initial balance by half its range with the study's bias aligned, enter the first EMA20 pullback that holds, target
# IB + 2× range) was REJECTED at the replay: 104 trades, 30% won, −0.13% a trade, both halves below zero — the pullback
# stop is hit two times in three and the far extension is reached seven times. It is not scored forward.
# chase_gate carries its own `since`, so the day it was written on is not scored for it.
VARIANTS_FORWARD = {"base": dict(VARIANT), "grade6": {**VARIANT, "min_grade": 6}, "next_open": {**VARIANT, "entry": "next_open"},
                    "chase_gate": {**VARIANT, "max_from_open_pct": MAX_FROM_OPEN_PCT, "since": 1794268800.0,
                                   "label": "retest · grade ≥ 7 · target ≥ 50 bps · ≤ %g%% from the open (the live gate)" % MAX_FROM_OPEN_PCT}}
VARIANTS_SINCE = 1794009600.0                                     # 2026-11-07 00:00 UTC (chase_gate: 2026-11-10)
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


def _session_open(d: pd.DataFrame, i: int) -> float | None:
    """The session's opening price on the bar's day (the 09:30 ET bar's open), or None before the open."""
    day = d["et"].iloc[i].date()
    for j in range(i, max(-1, i - 120), -1):
        if d["et"].iloc[j].date() != day:
            break
        if d["in_session"].iloc[j] and not (j > 0 and d["in_session"].iloc[j - 1] and d["et"].iloc[j - 1].date() == day):
            return float(d["open"].iloc[j])
    return None


async def step(sym: str, df: pd.DataFrame, now: float) -> dict | None:
    """Apply the study's sequence to the latest closed bar of one symbol. Returns what happened, or None."""
    import numpy as np
    from experiments import books
    if len(df) < 300:
        return None
    d = AL.session_frame(df, session_end=SESSION_END)
    i = len(d) - 1
    r = d.iloc[i]
    minute = _m((r["et"].hour, r["et"].minute))
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
        if level and r["in_session"] and ((s > 0 and r["close"] < level) or (s < 0 and r["close"] > level)):
            px = await monitor.live_price(sym) or float(r["close"])
            if w.close_trade_manually(mine["id"], px, "exit_through_level"):
                out = {"sym": sym, "event": "exit_through_level", "price": px}
        st["pending"], st["entering"] = None, None
        _set_state(sym, st)
        return out
    # 2. confirmed: enter — at this bar (the one after confirmation), or, in retest mode, on the bar that comes back to the
    #    level and closes beyond it again (within AL.RETEST_BARS bars; no retest → no trade)
    entering = st.get("entering")
    if entering:
        take = True
        if VARIANT.get("entry") == "retest":
            entering["waited"] = entering.get("waited", 0) + 1
            sd = 1 if entering["direction"] == "long" else -1
            touched = r["low"] <= entering["level"] if sd > 0 else r["high"] >= entering["level"]
            buf = r["close"] * AL.BUFFER_BPS / 1e4
            held_again = r["close"] > entering["level"] + buf if sd > 0 else r["close"] < entering["level"] - buf
            take = bool(touched and held_again)
            if take:
                off = r["close"] * AL.STOP_OFFSET_BPS / 1e4
                entering["stop"] = float(r["low"] - off) if sd > 0 else float(r["high"] + off)
            elif entering["waited"] < AL.RETEST_BARS and r["in_session"]:
                _set_state(sym, st)
                return None                                          # keep waiting for the retest
            else:
                _log({"ts": now, "sym": sym, "event": "no_retest", "level": entering["level_name"]})
        st["entering"] = None
        if take and r["in_session"] and len(open_trades) < MAX_OPEN and book_switches.enabled(BOOK) and books.ENABLED:
            why = "after the %02d:%02d ET entry cutoff" % ENTRY_CUTOFF if minute >= _m(ENTRY_CUTOFF) else gates(sym, now)
            if why:
                _log({"ts": now, "sym": sym, "event": "gated", "why": why, "level": entering["level_name"]})
            else:
                px = await monitor.live_price(sym) or float(r["open"])
                equity = w.get_portfolio()["total_equity"]
                notional, lev = _size(equity, px, entering["stop"])
                sd = 1 if entering["direction"] == "long" else -1
                good = (entering["stop"] < px < entering["target"]) if sd > 0 else (entering["target"] < px < entering["stop"])
                ran = (px / float(r["close"]) - 1) * 1e4 * sd                    # how far the live price has run on past the bar's close
                atr = float(r["atr"]) if r.get("atr") is not None and np.isfinite(r["atr"]) else 0.0
                stop_atr = abs(px - entering["stop"]) / atr if atr > 0 else None
                if ran > MAX_PAST_BPS:
                    _log({"ts": now, "sym": sym, "event": "skipped", "why": f"price ran {ran:.0f} bps past the bar's close (limit {MAX_PAST_BPS:.0f})",
                          "level": entering["level_name"]})
                elif stop_atr is not None and stop_atr < MIN_STOP_ATR:
                    _log({"ts": now, "sym": sym, "event": "skipped", "why": f"stop only {stop_atr:.1f} ATR away (floor {MIN_STOP_ATR:g}; ATR {atr / px * 100:.2f}%)",
                          "level": entering["level_name"]})
                elif (room := capital.coin_room(sym, entering["direction"], notional, BOOK))["notional"] <= 0:
                    _log({"ts": now, "sym": sym, "event": "skipped", "why": room["why"], "level": entering["level_name"]})
                elif notional >= 10 and good:
                    notional = room["notional"]
                    from ops import calendar
                    end_et = datetime.fromtimestamp(now, ET).replace(hour=SESSION_END[0], minute=SESSION_END[1], second=0, microsecond=0)
                    t = w.open_trade(symbol=sym, direction=entering["direction"], entry_price=px, stop_loss=entering["stop"],
                                     take_profit_1=entering["target"], take_profit_2=entering["target"], notional_usd=notional, timeframe="5m",
                                     leverage=lev, oracle_confidence=0,
                                     oracle_reasoning=f"auction: accepted {entering['level_name']} @ {entering['level']:.4g}, grade {entering['grade']}/8, "
                                                      f"target {entering['target_name']} @ {entering['target']:.4g} ({entering['target_bps']:.0f} bps)",
                                     hold_duration="to the next level, the stop or %02d:%02d ET" % SESSION_END,
                                     extra={"strategy": BOOK, "exit_policy": "manual", "shadows": False, "claude_verdict": "skipped", "entry_type": "market", "coin_cap": room["why"],
                                            "level_name": entering["level_name"], "level_px": entering["level"], "target_name": entering["target_name"],
                                            "entry_slot": "first_hour" if minute < _m(FIRST_HOUR_END) else "later",
                                            "grade": entering["grade"], "context": entering["context"], "near_event": calendar.near_event(sym, now),
                                            "variant": VARIANT})
                    if t.get("error"):
                        _log({"ts": now, "sym": sym, "event": "refused", "why": t["error"]})
                    else:
                        w.annotate(t["id"], expires_at=end_et.timestamp())
                        out = {"sym": sym, "event": "opened", "trade": t}
                        try:
                            from experiments import prop          # the same entry under Propr's rules, on the prop book's own ledger
                            await prop.mirror_auction(sym, t["direction"], px, entering["stop"], entering["target"], end_et.timestamp(),
                                                      t.get("oracle_reasoning") or "", {"level_name": entering["level_name"], "grade": entering["grade"],
                                                                                         "target_name": entering["target_name"]})
                        except Exception as e:
                            logger.warning("Prop book auction mirror failed: %s", e)
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
    # 4. acceptance on this bar — in the session, before the entry cutoff, at a level the book trades
    if r["in_session"] and minute < _m(ENTRY_CUTOFF):
        buf = r["close"] * AL.BUFFER_BPS / 1e4
        prev_c = float(d["close"].iloc[i - 1])
        ups = [(r[n], n) for n in AL.UP_LEVELS if n not in SKIP_LEVELS and np.isfinite(r[n]) and r["close"] > r[n] + buf and prev_c <= r[n] + buf]
        dns = [(r[n], n) for n in AL.DOWN_LEVELS if n not in SKIP_LEVELS and np.isfinite(r[n]) and r["close"] < r[n] - buf and prev_c >= r[n] - buf]
        if ups or dns:
            sd = 1 if ups else -1
            open_px = _session_open(d, i)
            moved = (float(r["close"]) / open_px - 1) * 100 * sd if open_px else 0.0
            if moved > MAX_FROM_OPEN_PCT:
                _log({"ts": now, "sym": sym, "event": "skipped", "why": f"price already {moved:.1f}% from the open the trade's way (limit {MAX_FROM_OPEN_PCT:g}%)",
                      "level": (max(ups) if ups else min(dns))[1]})
                _set_state(sym, st)
                return None
            up_lv, dn_lv = AL.second_levels(r) if VARIANT.get("target_mode") == "second" else AL.next_levels(r)
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


def _variant_rows() -> list[dict]:
    return kv.get_sync(_k("variants")) or []


def variant_trades_for_day(df: pd.DataFrame, sym: str, day_et: str) -> list[dict]:
    """One session day's trades under each forward arrangement: the study's sequence on the real bars, through the book's
    gates that the simulator does not know (levels the book does not trade, the entry cutoff), costed both ways."""
    out = []
    for name, v in VARIANTS_FORWARD.items():
        if v.get("since") and datetime.strptime(day_et, "%Y-%m-%d").replace(tzinfo=ET).timestamp() < v["since"]:
            continue                                                 # registered after this day: not scored on it
        if v.get("entry") == "trend_day":
            trades = AL.simulate_trend_day(df, sym, min_target_bps=v["min_target_bps"])
        else:
            trades = AL.simulate(df, sym, min_grade=v["min_grade"], min_target_bps=v["min_target_bps"],
                                 entry=v.get("entry", "next_open"), target_mode=v.get("target_mode", "next"), max_from_open_pct=v.get("max_from_open_pct"))
        for t in trades:
            if t.exit is None:
                continue
            et = t.entry_ts.tz_convert(ET)
            if et.strftime("%Y-%m-%d") != day_et or t.level_name in SKIP_LEVELS:
                continue
            minute = _m((et.hour, et.minute))
            if minute >= _m(ENTRY_CUTOFF):
                continue
            out.append({"variant": name, "symbol": sym, "day": day_et, "direction": t.direction, "level": t.level_name, "grade": t.grade,
                        "target": t.target_name, "entry_ts": float(t.entry_ts.timestamp()),
                        "exit_ts": float(t.exit_ts.timestamp()) if t.exit_ts is not None else None,
                        "entry": round(t.entry, 6), "exit": round(t.exit, 6), "exit_reason": t.exit_reason,
                        "pnl_pct": round(t.pnl_pct, 4), "net_pct": round(t.pnl_pct - 2 * VARIANT_COST_BPS / 100, 4),
                        "r": round(t.r, 3) if t.r is not None else None,
                        "slot": "first_hour" if minute < _m(FIRST_HOUR_END) else "later"})
    return out


async def variants_pass(now: float | None = None) -> int:
    """Once a session day, after the close: score the day under each arrangement. Returns the rows added."""
    now = now or time.time()
    if now < VARIANTS_SINCE:
        return 0
    et = datetime.fromtimestamp(now, ET)
    # The study's session ends at 17:00 ET (AL.SESSION_END), an hour after the book's: the pass waits for that bar so the
    # shadows' exits are the study's, not whichever bar the tick happened to find last.
    if et.weekday() >= 5 or _m((et.hour, et.minute)) < _m(AL.SESSION_END) + 15:
        return 0
    day = et.strftime("%Y-%m-%d")
    if kv.get_sync(_k("variants", "day", day)):
        return 0
    rows = _variant_rows()
    seen = {(r["variant"], r["symbol"], r["entry_ts"]) for r in rows}
    added, failed = 0, 0
    for sym in SYMBOLS:
        try:
            df = await bars(sym, now)
            for r in variant_trades_for_day(df, sym, day):
                key = (r["variant"], r["symbol"], r["entry_ts"])
                if key not in seen:
                    rows.append(r)
                    seen.add(key)
                    added += 1
        except Exception as e:
            failed += 1
            logger.warning("Auction variants %s failed for %s: %s", sym, day, e)
    kv.set_sync(_k("variants"), rows[-VARIANTS_KEEP:], 180 * 86400)
    if not failed:                                                # a symbol that failed is retried next minute; `seen` keeps the retry from double-counting
        kv.set_sync(_k("variants", "day", day), 1, 3 * 86400)
    logger.info("Auction variants: %d trade(s) scored for %s (%d symbol(s) failed)", added, day, failed)
    return added


def variants_forward() -> dict:
    """The forward arrangements' records, each judged by the Lab's standalone verdict (experiments/lab._verdict_single):
    40 trades, the 95% range of net % a trade, and the Lab's extension when the bootstrap mostly favours it."""
    import random
    from experiments import lab
    rows = _variant_rows()
    out = {}
    for name, v in VARIANTS_FORWARD.items():
        mine = [r for r in rows if r["variant"] == name]
        verdict = lab._verdict_single([{"net_pnl": r["net_pct"], "r": r["r"]} for r in mine], random.Random(5))
        lo, hi = verdict.get("expectancy_ci") or (None, None)
        first = [r for r in mine if r["slot"] == "first_hour"]
        rs = [r["r"] for r in mine if r["r"] is not None]
        out[name] = {"rule": {k: v.get(k) for k in ("entry", "min_grade", "min_target_bps", "target_mode", "max_from_open_pct", "label")}, "since": v.get("since") or VARIANTS_SINCE,
                     "trades": len(mine), "wins": sum(1 for r in mine if r["net_pct"] > 0),
                     "avg_net_pct": round(sum(r["net_pct"] for r in mine) / len(mine), 4) if mine else None,
                     "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
                     "first_hour": {"trades": len(first), "avg_net_pct": round(sum(r["net_pct"] for r in first) / len(first), 4) if first else None},
                     "status": verdict["status"], "label": verdict["label"], "ci": [lo, hi], "positive_share": verdict.get("positive_share")}
    return {"since": VARIANTS_SINCE, "cost_bps": VARIANT_COST_BPS, "days": len({r["day"] for r in rows}), "variants": out,
            "recent": sorted(rows, key=lambda r: -r["entry_ts"])[:20]}


async def tick(now: float | None = None) -> list[dict]:
    """Every minute: in the New York session, once each 5-minute bar closes, run the sequence for every symbol."""
    from experiments import books
    now = now or time.time()
    if not books.ENABLED or not book_switches.enabled(BOOK):
        return []
    try:
        await variants_pass(now)                              # after the close, once a day: the forward arrangements
    except Exception as e:
        logger.warning("Auction variants pass failed: %s", e)
    et = datetime.fromtimestamp(now, ET)
    hm = (et.hour, et.minute)
    if not (AL.SESSION_OPEN <= hm < (SESSION_END[0], SESSION_END[1] + 5)) or et.weekday() >= 5:
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
            "log": list(reversed(kv.get_sync(_k("log")) or []))[:30], "variants_forward": variants_forward()}


# ── The mentor's page ──────────────────────────────────────────────────────────
_snap_cache: dict = {"at": 0.0, "data": None}
LEVEL_NAMES = ("PDH", "PDL", "ONH", "ONL", "WKH", "WKL", "ORBH", "ORBL", "IBH", "IBL", "IBH05", "IBL05", "IBH1", "IBL1", "IBH2", "IBL2")
LEVEL_LABELS = {"PDH": "Prior day high", "PDL": "Prior day low", "ONH": "Overnight high", "ONL": "Overnight low", "WKH": "Last week's high",
                "WKL": "Last week's low", "ORBH": "Opening range high (09:30–10:00)", "ORBL": "Opening range low", "IBH": "Initial balance high (09:30–10:30)",
                "IBL": "Initial balance low", "IBH05": "IB high + ½ range", "IBL05": "IB low − ½ range", "IBH1": "IB high + 1× range", "IBL1": "IB low − 1× range",
                "IBH2": "IB high + 2× range", "IBL2": "IB low − 2× range", "PSYCH": "Round number"}


def _symbol_view(sym: str, d: pd.DataFrame, now: float) -> dict:
    """What the chart would show for one instrument: the levels, the session, the context readings and the grade a long
    or short would get right now, plus where the book stands in its sequence."""
    import numpy as np
    r = d.iloc[-1]
    price = float(r["close"])
    levels = []
    for n in LEVEL_NAMES:
        v = r.get(n)
        if v is not None and np.isfinite(v):
            levels.append({"name": n, "label": LEVEL_LABELS.get(n, n), "price": round(float(v), 6), "bps": round((float(v) / price - 1) * 1e4, 1)})
    levels.sort(key=lambda x: -x["price"])
    ctx = AL.context(d, len(d) - 1)
    g_long, sd_long, why_long = AL.grade(ctx, 1)
    g_short, sd_short, why_short = AL.grade(ctx, -1)
    up, dn = AL.second_levels(r) if VARIANT.get("target_mode") == "second" else AL.next_levels(r)
    st = _state(sym)
    return {"symbol": sym, "price": price, "bar_ts": float(d.index[-1].timestamp()), "et": r["et"].strftime("%a %H:%M ET"),
            "in_session": bool(r["in_session"]), "orb_valid": bool(r["orb_valid"]), "ib_valid": bool(r["ib_valid"]),
            "vwap": round(float(r["vwap"]), 6), "ema20": round(float(r["ema20"]), 6), "ema50": round(float(r["ema50"]), 6), "ema200": round(float(r["ema200"]), 6),
            "atr_pct": round(float(r["atr"] / price * 100), 3) if np.isfinite(r["atr"]) else None,
            "levels": levels, "next_above": {"name": up[1], "price": round(float(up[0]), 6)}, "next_below": {"name": dn[1], "price": round(float(dn[0]), 6)},
            "context": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ctx.items()},
            "grade_long": {"score": g_long, "stand_down": sd_long, "reasons": why_long}, "grade_short": {"score": g_short, "stand_down": sd_short, "reasons": why_short},
            "state": {k: v for k, v in st.items() if k != "bar"}, "gate": gates(sym, now)}


async def snapshot(now: float | None = None) -> dict:
    """Everything the mentor's page shows, refreshed at most once a minute."""
    from experiments import books
    from trading import overview, trade_analytics
    import random
    now = now or time.time()
    if _snap_cache["data"] and now - _snap_cache["at"] < 60:
        return _snap_cache["data"]
    views = []
    for sym in SYMBOLS:
        try:
            df = await bars(sym, now)
            if len(df) < 300:
                views.append({"symbol": sym, "error": "not enough history yet"})
                continue
            views.append(_symbol_view(sym, AL.session_frame(df, session_end=SESSION_END), now))
        except Exception as e:
            views.append({"symbol": sym, "error": str(e)})
    w = books.WALLETS[BOOK]
    p = w.get_portfolio(closed_limit=100)
    rows, _ = overview.all_rows()
    mine = [r for r in rows if r["system"] == BOOK]
    closed = [r for r in mine if r["status"] == "closed"]
    conf = trade_analytics._confidence(closed, random.Random(3)) if closed else {"trades": 0, "enough": False}
    lo, hi = (conf.get("expectancy_ci") or (None, None))
    verdict = ("collecting" if len(closed) < 40 else "edge" if lo is not None and lo > 0 else "no_edge" if hi is not None and hi < 0 else "inconclusive")
    # The record by entry slot: the replay's only positive slot was the first hour, so the forward test judges it on its own.
    slices = {}
    for t in w.all_trades()["closed"]:
        slot = t.get("entry_slot") or "later"
        sl = slices.setdefault(slot, {"trades": 0, "wins": 0, "net_pnl": 0.0, "r_sum": 0.0, "r_n": 0})
        pnl = t.get("pnl_usd") or 0.0
        sl["trades"] += 1
        sl["wins"] += pnl > 0
        sl["net_pnl"] += pnl
        if t.get("risk_usd"):
            sl["r_sum"] += pnl / t["risk_usd"]
            sl["r_n"] += 1
    slices = {k: {"trades": v["trades"], "wins": v["wins"], "net_pnl": round(v["net_pnl"], 2), "avg_r": round(v["r_sum"] / v["r_n"], 3) if v["r_n"] else None}
              for k, v in slices.items()}
    try:
        import json as _json
        replay = _json.load(open(os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "auction_results.json")))
        variants = replay.get("variants") or {}
        replay_summary = {"verdict": replay.get("verdict"), "variant_verdict": replay.get("variant_verdict"),
                          "variants": {k: {"trades": v.get("trades"), "binance": (v.get("all") or {}).get("avg_net_pct"),
                                           "futures": ((v.get("low_cost") or {}).get("all") or {}).get("avg_net_pct"),
                                           "halves_futures": [((v.get("low_cost") or {}).get("first_half") or {}).get("avg_net_pct"),
                                                              ((v.get("low_cost") or {}).get("second_half") or {}).get("avg_net_pct")]}
                                       for k, v in variants.items() if v.get("trades")}}
    except Exception:
        replay_summary = None
    # $/day: closed P&L by New York day, today and the 7-day average
    by_day: dict[str, float] = {}
    for r in closed:
        if r.get("closed_ts"):
            day = datetime.fromtimestamp(r["closed_ts"], ET).strftime("%Y-%m-%d")
            by_day[day] = by_day.get(day, 0.0) + (r.get("net_pnl") or 0.0)
    today = datetime.fromtimestamp(now, ET).strftime("%Y-%m-%d")
    last7 = [datetime.fromtimestamp(now - i * 86400, ET).strftime("%Y-%m-%d") for i in range(7)]
    daily = {"goal": list(DAILY_GOAL), "today": round(by_day.get(today, 0.0), 2), "avg7": round(sum(by_day.get(d_, 0.0) for d_ in last7) / 7, 2),
             "days": {d_: round(v, 2) for d_, v in sorted(by_day.items())[-14:]}}
    data = {"at": now, "daily": daily, "book": {"equity": p["total_equity"], "initial": p["initial_balance"], "balance": p["balance"],
                                "return_pct": round((p["total_equity"] / p["initial_balance"] - 1) * 100, 2) if p["initial_balance"] else None,
                                "open": p["open_trades"], "closed": len(closed), "wins": sum(1 for r in closed if (r.get("net_pnl") or 0) > 0),
                                "net_pnl": round(sum(r.get("net_pnl") or 0 for r in closed), 2),
                                "avg_r": round(sum(r["r"] for r in closed if r.get("r") is not None) / max(1, sum(1 for r in closed if r.get("r") is not None)), 3) if closed else None,
                                "enabled": book_switches.enabled(BOOK), "equity_history": p.get("equity_history", [])[-300:]},
            "verdict": {"status": verdict, "trades": len(closed), "needed": 40, "expectancy_usd": conf.get("expectancy_usd"), "ci": [lo, hi], "slices": slices},
            "config": {"symbols": list(SYMBOLS), "variant": VARIANT, "risk_pct": RISK_PCT, "max_gross": MAX_GROSS, "max_open": MAX_OPEN, "max_leverage": MAX_LEVERAGE,
                       "session": "09:30–%02d:%02d ET, entries until %02d:%02d" % (*SESSION_END, *ENTRY_CUTOFF),
                       "entry_cutoff": "%02d:%02d" % ENTRY_CUTOFF, "max_past_bps": MAX_PAST_BPS, "skip_levels": sorted(SKIP_LEVELS),
                       "min_stop_atr": MIN_STOP_ATR, "max_from_open_pct": MAX_FROM_OPEN_PCT,
                       "buffer_bps": AL.BUFFER_BPS, "stop_offset_bps": AL.STOP_OFFSET_BPS, "retest_bars": AL.RETEST_BARS},
            "instruments": views, "log": list(reversed(kv.get_sync(_k("log")) or []))[:40], "replay": replay_summary,
            "variants_forward": variants_forward(), "level_labels": LEVEL_LABELS}
    _snap_cache.update(at=now, data=data)
    return data


# ── The chart: bars with the study's levels, lines, signals and this book's trades drawn on them ────────────────────
CHART_LEVELS = ("PDH", "PDL", "ONH", "ONL", "WKH", "WKL", "ORBH", "ORBL", "IBH", "IBL", "IBH1", "IBL1", "IBH2", "IBL2")


async def chart(sym: str, bars_n: int = 600, now: float | None = None) -> dict:
    """What the mentor's chart shows for one instrument: the last `bars_n` closed 5-minute bars, VWAP and the EMAs, every
    level as a per-bar series (they step at the session boundaries), the study's own signals over the window (what the
    sequence would have marked: acceptance, confirmation, retest entry, exit) and this book's real trades."""
    import numpy as np
    now = now or time.time()
    df = await bars(sym, now)
    if len(df) < 300:
        return {"symbol": sym, "error": "not enough history yet"}
    d = AL.session_frame(df, session_end=SESSION_END)
    tail = d.iloc[-bars_n:]
    ts = [int(t.timestamp()) for t in tail.index]

    def series(col: str) -> list:
        vals = tail[col].values
        return [None if (v is None or not np.isfinite(v)) else round(float(v), 6) for v in vals]
    out = {"symbol": sym, "at": now, "bar_s": BAR_S,
           "bars": [{"t": t, "o": round(float(r.open), 6), "h": round(float(r.high), 6), "l": round(float(r.low), 6), "c": round(float(r.close), 6), "v": round(float(r.volume), 2),
                     "session": bool(r.in_session)} for t, r in zip(ts, tail.itertuples())],
           "lines": {k: series(k) for k in ("vwap", "ema9", "ema20", "ema50", "ema200")},
           "levels": {k: series(k) for k in CHART_LEVELS},
           "current_levels": [{"name": k, "label": LEVEL_LABELS.get(k, k), "price": round(float(tail.iloc[-1][k]), 6)} for k in CHART_LEVELS if np.isfinite(tail.iloc[-1][k])]}
    # the study's signals over the window, under the book's own arrangement (every setup, gates aside)
    events = []
    try:
        trades = AL.simulate(df, sym, min_grade=0, entry=VARIANT.get("entry", "retest"), target_mode=VARIANT.get("target_mode", "second"))
        lo = tail.index[0]
        for t in trades:
            if t.accept_ts < lo:
                continue
            g = t.grade
            events.append({"t": int(t.accept_ts.timestamp()), "kind": "accept", "direction": t.direction, "level": t.level_name, "price": t.level})
            events.append({"t": int(t.confirm_ts.timestamp()), "kind": "confirm", "direction": t.direction, "level": t.level_name, "grade": g})
            events.append({"t": int(t.entry_ts.timestamp()), "kind": "entry", "direction": t.direction, "price": t.entry, "stop": t.stop, "target": t.target,
                           "target_name": t.target_name, "grade": g, "taken": g >= VARIANT.get("min_grade", 0) and abs(t.target / t.entry - 1) * 1e4 >= VARIANT.get("min_target_bps", 0)})
            if t.exit_ts is not None:
                events.append({"t": int(t.exit_ts.timestamp()), "kind": "exit", "direction": t.direction, "price": t.exit, "reason": t.exit_reason,
                               "pnl_pct": round(t.pnl_pct, 3), "r": round(t.r, 2) if t.r is not None else None})
    except Exception as e:
        out["events_error"] = str(e)
    out["study_events"] = events
    # this book's real trades on the instrument
    from experiments import books
    w = books.WALLETS[BOOK]
    p = w.get_portfolio(closed_limit=100)
    mine = []
    for t in p["open_trades"] + p["closed_trades"]:
        if t["symbol"] != sym:
            continue
        mine.append({"id": t["id"], "direction": t["direction"], "entry_ts": _ts_of(t.get("opened_at")), "exit_ts": _ts_of(t.get("closed_at")),
                     "entry": t["entry_price"], "exit": t.get("current_price") if t.get("status") == "open" else t.get("exit_price") or t.get("exit_trigger_price") or t.get("current_price"),
                     "stop": t.get("stop_loss"), "target": t.get("take_profit_1"), "level": t.get("level_name"), "target_name": t.get("target_name"),
                     "grade": t.get("grade"), "reason": t.get("close_reason"), "pnl_usd": t.get("pnl_usd"), "status": t.get("status")})
    out["trades"] = mine
    return out


def _ts_of(iso) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None
