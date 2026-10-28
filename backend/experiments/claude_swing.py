"""Claude swing — Claude picking its own swing trades (paper), no rules deciding for it.

Every CLAUDE_SWING_INTERVAL_H hours (default 4) Claude sees a condensed brief of the coins the
scanner covers (1h / 4h / 1d technicals, key levels, derivatives positioning, headlines, the
market backdrop) and its own open positions, and decides:
  * new trades — or none: passing is a valid answer and usually the right one
  * for each open position — hold, close, or tighten the stop (never loosen)
Each trade carries its thesis and the condition that would prove it wrong. Claude's own stop
and target are the exits (the "manual" policy: no trailing, no stale timer); the hold period
it states is enforced at review time. Guard rails the app enforces, not Claude:
  reward:risk ≥ MIN_RR at entry, at most MAX_OPEN positions/orders, RISK_PCT of equity per trade,
  CLAUDE_SWING_DAILY_CALLS calls per UTC day.
Pre-registered test (see the Lab tab): after 40 closed trades, the 95% bootstrap interval of
the average net result per trade must be above zero.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from experiments import books
from oracle.claude_oracle import MODEL as DEFAULT_MODEL, _fmt, _positioning_lines, get_client, MAX_TOKENS_REVIEW, parse_json, response_text
from storage import state as kv
from trading import book_switches, monitor, regime

logger = logging.getLogger("experiments.claude_swing")

BOOK = "claude_swing"
INTERVAL_S = float(os.getenv("CLAUDE_SWING_INTERVAL_H", "4")) * 3600
DAILY_CALLS = int(os.getenv("CLAUDE_SWING_DAILY_CALLS", "10"))
MODEL = os.getenv("CLAUDE_SWING_MODEL") or DEFAULT_MODEL
MAX_OPEN = int(os.getenv("CLAUDE_SWING_MAX_OPEN", "3"))
RISK_PCT = float(os.getenv("CLAUDE_SWING_RISK_PCT", "1.0"))
MIN_RR = 2.0
LIMIT_TTL_S = 24 * 3600
TIMEFRAMES = ("1d", "4h", "1h")

_task: asyncio.Task | None = None

SYSTEM_PROMPT = f"""You are a discretionary crypto swing trader running a small, separate paper book on
Binance USDT-M perpetual futures. This is an experiment: can your own judgment find trades with a
real edge? Nobody else's rules decide for you.

How you trade:
- Holding period 1–7 days. You are not a scalper; ignore noise below the 1h chart.
- Only take a trade when the evidence is clearly one-sided. Passing is a valid answer and usually
  the right one — a review that opens nothing costs nothing; a weak trade costs money.
- Longs and shorts are equally valid.
- Every trade needs: a structural stop (beyond a level that proves you wrong, not an arbitrary %),
  one target with reward:risk of at least {MIN_RR}:1 from your entry, a hold period in days, a
  one-sentence thesis, and the specific observable condition that would invalidate it.
- Costs are ~0.1% round trip plus funding every 8h (positive = longs pay). Crowded funding against
  your side needs extra conviction.
- At most {MAX_OPEN} positions or orders at once. Each risks {RISK_PCT}% of equity at the stop.
- Entry: "market" (now), or "limit" at a level you expect price to revisit within 24h.

Managing open positions — for each one decide:
- "hold" — thesis intact.
- "close" — the invalidation condition has happened, or the thesis is plainly broken.
- "tighten" — move the stop closer (never further) to lock in progress. Give new_stop.
Don't micro-manage: a trade that is merely slow is a hold until its hold period ends.

Respond with ONLY a JSON object, no prose, no code fences:
{{
  "market_view": "<2 sentences on the overall backdrop>",
  "positions": [{{"id": "<id>", "action": "hold" | "close" | "tighten", "new_stop": <number or null>, "reason": "<short>"}}],
  "new_trades": [{{
    "symbol": "<e.g. SOLUSDT>", "direction": "long" | "short", "entry_type": "market" | "limit",
    "entry": <number>, "stop": <number>, "target": <number>, "hold_days": <number 1-7>,
    "confidence": <integer 0-100>, "thesis": "<one sentence>", "invalidation": "<observable condition>"
  }}]
}}"""


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _coin_brief(scan: dict) -> list[str]:
    pkg = scan.get("signal_package") or {}
    tfs = (pkg.get("ta_summary") or {}).get("timeframe_signals") or {}
    regime = pkg.get("market_regime") or {}
    news = pkg.get("news_summary") or {}
    fa = scan.get("fa") or pkg.get("fundamentals") or {}
    lines = [
        f"## {scan['symbol']} — ${_fmt(scan.get('price'), ',.6g')}",
        f"24h {_fmt(scan.get('change_24h'), '+.2f')}% · 7d {_fmt((pkg.get('coingecko') or {}).get('price_change_7d_pct'), '+.2f')}% · "
        f"regime {regime.get('regime', 'n/a')} ({regime.get('trend_direction', 'n/a')}), ADX {_fmt(regime.get('adx'), '.0f')}, "
        f"ATR {_fmt(regime.get('atr_pct'), '.2f')}% · relative strength vs BTC 24h {_fmt(fa.get('rs_vs_btc_24h'), '+.2f')}pp",
    ]
    for tf in TIMEFRAMES:
        d = tfs.get(tf)
        if not d:
            continue
        lines.append(
            f"[{tf}] {d.get('signal', 'n/a')} ({_fmt(d.get('score'), '+.2f')}) · structure {d.get('market_structure', 'n/a')} · "
            f"EMA {d.get('ema_alignment', 'n/a')} · RSI {_fmt(d.get('rsi'), '.0f')} div {d.get('rsi_divergence', 'none')} · "
            f"MACD {d.get('macd_cross', 'none')} · ADX {_fmt(d.get('adx'), '.0f')} · vol {d.get('volume_signal', 'n/a')} · "
            f"VWAP {_fmt(d.get('vwap_deviation_pct'), '+.1f')}% · Ichimoku {d.get('ichimoku_signal', 'n/a')} · "
            f"fib support {_fmt(d.get('fib_nearest_support'), ',.6g')} / resistance {_fmt(d.get('fib_nearest_resistance'), ',.6g')}")
    lines += [x for x in _positioning_lines(fa.get("positioning") or {}) if x and not x.startswith("- Read it")]
    heads = [h for h in (news.get("top_headlines") or []) if h][:2]
    if heads:
        lines.append(f"News ({news.get('aggregate_sentiment', 'n/a')}): " + " | ".join(heads))
    return lines


def _brief(scans: list[dict], shared: dict, positions: list[dict], orders: list[dict], equity: float) -> str:
    gm = shared.get("global_market") or {}
    fg = shared.get("fear_greed") or {}
    out = [
        f"# Review — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        f"Book equity ${equity:,.2f}. Market: total cap ${(gm.get('total_market_cap_usd') or 0) / 1e9:,.0f}B "
        f"({_fmt(gm.get('market_cap_change_24h_pct'), '+.2f')}% 24h), BTC dominance {_fmt(gm.get('btc_dominance_pct'), '.1f')}%, "
        f"Fear & Greed {fg.get('value', 'n/a')} ({fg.get('classification', 'n/a')}).",
        "",
        "# Your open positions",
    ]
    if positions:
        for t in positions:
            held_d = (time.time() - datetime.fromisoformat(t["opened_at"]).timestamp()) / 86400
            out.append(f"- id {t['id']}: {t['direction']} {t['symbol']} entry {t['entry_price']:.6g}, now {t['current_price']:.6g}, "
                       f"stop {t['stop_loss']:.6g}, target {t['take_profit_2']:.6g}, P&L {t['pnl_usd']:+.2f} USDT, "
                       f"held {held_d:.1f}d of {t.get('hold_days', '?')}d. Thesis: {t.get('claude_thesis', '')} "
                       f"Invalidated if: {t.get('invalidation', '')}")
    else:
        out.append("- none")
    for o in orders:
        out.append(f"- pending {o['direction']} limit {o['symbol']} @ {o['limit_price']:.6g} (expires in "
                   f"{(o['expires_at'] - time.time()) / 3600:.0f}h)")
    from trading import debrief
    past = debrief.lessons(BOOK)
    if past:
        out += ["", "# Lessons from your recent closed trades (your own debriefs — weigh them, don't overfit to one trade)"]
        for d in past:
            c = d["claude"]
            out.append(f"- {d['symbol']} ({d['primary']}, {d['metrics'].get('r')}R): {c.get('what_happened')} "
                       f"Lesson: {c.get('lesson')}")
    out += ["", "# Coins", ""]
    for s in scans:
        out += _coin_brief(s) + [""]
    return "\n".join(out)


def _log_review(entry: dict) -> None:
    k = kv.key(BOOK, "reviews")
    log = kv.get_sync(k) or []
    kv.set_sync(k, ([entry] + log)[:60])


def reviews() -> list[dict]:
    return kv.get_sync(kv.key(BOOK, "reviews")) or []


def validate(trade: dict, price: float, symbols: set[str]) -> str | None:
    """Why a proposed trade can't be taken, or None. Checked against the LIVE price."""
    try:
        d, entry, stop, target = trade["direction"], float(trade["entry"]), float(trade["stop"]), float(trade["target"])
    except (KeyError, TypeError, ValueError):
        return "incomplete trade"
    if trade.get("symbol") not in symbols:
        return f"{trade.get('symbol')} wasn't in the brief"
    if d not in ("long", "short"):
        return "no direction"
    s = 1 if d == "long" else -1
    ref = price if trade.get("entry_type") != "limit" else entry
    if (ref - stop) * s <= 0 or (target - ref) * s <= 0:
        return "stop/target on the wrong side of the entry"
    rr = (target - ref) * s / ((ref - stop) * s)
    if rr < MIN_RR:
        return f"reward:risk {rr:.2f} at {'the live price' if ref == price else 'entry'} is below {MIN_RR}"
    return None


async def _apply(decision: dict, scans: dict[str, dict]) -> dict:
    wallet = books.WALLETS[BOOK]
    done: dict = {"opened": [], "rejected": [], "managed": []}
    portfolio = await asyncio.to_thread(wallet.get_portfolio, 1, 1)
    open_by_id = {t["id"]: t for t in portfolio["open_trades"]}

    for p in decision.get("positions") or []:
        t = open_by_id.get(p.get("id"))
        if not t:
            continue
        if p.get("action") == "close":
            price = await monitor.live_price(t["symbol"]) or t["current_price"]
            await asyncio.to_thread(wallet.annotate, t["id"], exit_note=p.get("reason"))
            await asyncio.to_thread(wallet.close_trade_manually, t["id"], price, "claude_closed")
            done["managed"].append({"id": t["id"], "symbol": t["symbol"], "action": "close", "reason": p.get("reason")})
        elif p.get("action") == "tighten" and p.get("new_stop"):
            moved = await asyncio.to_thread(wallet.move_stop, t["id"], float(p["new_stop"]), p.get("reason", "claude"))
            done["managed"].append({"id": t["id"], "symbol": t["symbol"], "action": "tighten" if moved else "tighten refused",
                                    "new_stop": p.get("new_stop"), "reason": p.get("reason")})

    # The stated hold period is part of the plan: past it, the trade is closed.
    for t in portfolio["open_trades"]:
        hold_days = t.get("hold_days")
        if hold_days and time.time() - datetime.fromisoformat(t["opened_at"]).timestamp() > float(hold_days) * 86400:
            price = await monitor.live_price(t["symbol"]) or t["current_price"]
            if await asyncio.to_thread(wallet.close_trade_manually, t["id"], price, "hold_period_over"):
                done["managed"].append({"id": t["id"], "symbol": t["symbol"], "action": "close", "reason": "hold period over"})

    portfolio = await asyncio.to_thread(wallet.get_portfolio, 1, 1)
    busy = {t["symbol"] for t in portfolio["open_trades"]} | {o["symbol"] for o in (await asyncio.to_thread(wallet.get_orders, 1))["pending"]}
    slots = MAX_OPEN - len(busy)
    for nt in decision.get("new_trades") or []:
        sym = nt.get("symbol")
        if slots <= 0:
            done["rejected"].append({**nt, "why": f"already {MAX_OPEN} positions/orders"})
            continue
        if sym in busy:
            done["rejected"].append({**nt, "why": "already holding it"})
            continue
        price = (await monitor.live_price(sym) if sym else None) or (scans.get(sym) or {}).get("price") or 0
        why = validate(nt, price, set(scans))
        if why:
            done["rejected"].append({**nt, "why": why})
            continue
        extra = {"strategy": BOOK, "exit_policy": "manual", "experiment": BOOK, "claude_thesis": nt.get("thesis", ""),
                 "invalidation": nt.get("invalidation", ""), "hold_days": nt.get("hold_days"),
                 "claude_confidence": nt.get("confidence"), "claude_entry": nt.get("entry"), "tp_set": True,
                 "regime_at_entry": await regime.current()}
        from ops import calendar
        blackout = calendar.macro_blackout()
        if blackout:
            done["rejected"].append({**nt, "why": blackout})
            continue
        extra = {**extra, "near_event": calendar.near_event(sym)}
        fields = dict(stop_loss=float(nt["stop"]), take_profit_1=float(nt["target"]), take_profit_2=float(nt["target"]),
                      risk_pct=RISK_PCT)
        if nt.get("entry_type") == "limit":
            r = await asyncio.to_thread(
                wallet.place_order, symbol=sym, direction=nt["direction"], limit_price=float(nt["entry"]), size_pct=None,
                expires_at=time.time() + LIMIT_TTL_S,
                trade_fields={"timeframe": "swing", "oracle_confidence": nt.get("confidence") or 0,
                              "oracle_reasoning": nt.get("thesis", ""), "hold_duration": f"{nt.get('hold_days')}d",
                              "extra": extra},
                extra={"source": BOOK}, **fields)
        else:
            r = await asyncio.to_thread(
                wallet.open_trade, symbol=sym, direction=nt["direction"], entry_price=price,
                timeframe="swing", oracle_confidence=nt.get("confidence") or 0, oracle_reasoning=nt.get("thesis", ""),
                hold_duration=f"{nt.get('hold_days')}d", extra=extra, **fields)
        if r.get("error"):
            done["rejected"].append({**nt, "why": r["error"]})
            continue
        busy.add(sym)
        slots -= 1
        done["opened"].append({**nt, "id": r["id"], "kind": "order" if nt.get("entry_type") == "limit" else "trade"})
    return done


async def review(scans: list[dict], shared: dict) -> dict | None:
    """One review: brief → Claude → apply. Returns the logged review."""
    wallet = books.WALLETS[BOOK]
    portfolio = await asyncio.to_thread(wallet.get_portfolio, 1, 1)
    orders = (await asyncio.to_thread(wallet.get_orders, 1))["pending"]
    brief = _brief(scans, shared, portfolio["open_trades"], orders, portfolio["total_equity"])
    used = await kv.incr_with_expire(kv.key(BOOK, "calls", _utc_day()), 172800)
    started = time.time()
    try:
        resp = await get_client().messages.create(
            model=MODEL, max_tokens=MAX_TOKENS_REVIEW,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": brief}])
        decision = parse_json(response_text(resp))
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    except Exception as e:
        logger.warning("Claude swing review failed: %s", e)
        _log_review({"ts": started, "error": str(e)[:300], "calls_today": used})
        return None
    done = await _apply(decision, {s["symbol"]: s for s in scans})
    entry = {"ts": started, "market_view": decision.get("market_view", ""), "coins": [s["symbol"] for s in scans],
             **done, "positions": decision.get("positions") or [], "calls_today": used, "usage": usage}
    _log_review(entry)
    logger.info("Claude swing review: %d opened, %d rejected, %d managed", len(done["opened"]),
                len(done["rejected"]), len(done["managed"]))
    return entry


async def maybe_review(scans: list[dict], shared: dict) -> None:
    """Called after every general sweep; starts a review in the background when one is due."""
    global _task
    if not books.ENABLED or not book_switches.enabled(BOOK) or not scans or (_task and not _task.done()):
        return
    last = kv.get_sync(kv.key(BOOK, "last_review")) or 0
    if time.time() - last < INTERVAL_S:
        return
    if await calls_today() >= DAILY_CALLS:
        return
    kv.set_sync(kv.key(BOOK, "last_review"), time.time())
    _task = asyncio.create_task(review(list(scans), shared), name="claude-swing-review")


async def calls_today() -> int:
    return await kv.counter(kv.key(BOOK, "calls", _utc_day()))


def status() -> dict:
    last = kv.get_sync(kv.key(BOOK, "last_review")) or 0
    return {"interval_h": INTERVAL_S / 3600, "daily_calls": DAILY_CALLS, "max_open": MAX_OPEN, "risk_pct": RISK_PCT,
            "model": MODEL, "last_review": last or None, "next_review": (last + INTERVAL_S) if last else None,
            "calls_today": int(kv.get_sync(kv.key(BOOK, "calls", _utc_day())) or 0)}
