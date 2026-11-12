"""The prop book — the reversal lane and the auction book run under Propr's Classic 1-Step $25K rules (2026-11-09).

The user wants a funded account. Propr (propr.xyz) is the fit: built on Hyperliquid, whose validators sit in the same
AWS region as our production box; bots allowed; the US not on its restricted list. Its evaluation is itself paper on
Hyperliquid's prices, so the sensible order is: run our strategies under its exact rulebook here first, on this book
with the account's size and limits; if it survives an evaluation's worth of trading, buy the $275 challenge and point
the same book at the account through experiments/prop_exec.py (PROP_EXEC=beta to rehearse on the firm's sandbox).

What trades here: every pick the autotrader's reversal lane makes (autotrader/engine._reversal_lane hands them over
before sizing them for its own ledger) and every entry the auction book takes (experiments/auction.py), each sized by
the rules in experiments/prop_rules.py and refused when the firm could not take it (a coin not on Hyperliquid, the
6-position cap, our per-source caps) or when our own floors say stop. The paper wallet handles stops, targets and
expiries as for every book; this module keeps the account's ledger (start-of-day balance, equity against each floor),
flattens at our floors, records what the firm would have done at its own, and mirrors the paper trades to the firm's
API when that is on. The Lab judges it like any book on its 40 trades; the Prop tab shows the evaluation.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from experiments import books, prop_exec, prop_rules as R
from storage import state as kv
from trading import book_switches, monitor

logger = logging.getLogger("experiments.prop")

BOOK = "prop"
LABEL = "Prop · Propr Classic 1-Step $25K"
REVERSAL_STOP_PCT = 10.0                   # the lane's disaster stop
REVERSAL_HOLD_H = 24.0
LOG_KEEP = 300
UNIVERSE_TTL = 6 * 3600
STATE_KEY = kv.key("prop", "state")
LOG_KEY = kv.key("prop", "log")
UNIVERSE_KEY = kv.key("prop", "universe")


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


def _wallet():
    return books.WALLETS[BOOK]


def _log(entry: dict) -> None:
    rows = (kv.get_sync(LOG_KEY) or []) + [entry]
    kv.set_sync(LOG_KEY, rows[-LOG_KEEP:])


def _fresh(now: float) -> dict:
    return {"attempt": 1, "started_at": now, "day": R.day_key(now), "sod_balance": R.PLAN["size"], "phase": "evaluation",
            "halted_day": None, "halt_why": None, "passed_at": None, "breach": None, "stopped": None,
            "min_equity": R.PLAN["size"], "max_equity": R.PLAN["size"], "flattens": 0, "halts": 0, "attempts": []}


def _state(now: float | None = None) -> dict:
    st = kv.get_sync(STATE_KEY)
    if not st:
        st = _fresh(now or time.time())
        kv.set_sync(STATE_KEY, st)
    return st


def _save(st: dict) -> None:
    kv.set_sync(STATE_KEY, st)


def ledger(w, st: dict) -> dict:
    """The firm's view of the account: starting balance + realised P&L since the attempt began (+ floating)."""
    p = w.get_portfolio(closed_limit=3000, history_limit=1)
    closed = [t for t in p["closed_trades"] if _ts(t.get("closed_at")) >= st["started_at"]]
    realised = sum(t.get("pnl_usd") or 0 for t in closed)
    balance = R.PLAN["size"] + realised
    return {"balance": round(balance, 2), "equity": round(balance + p["open_pnl"], 2), "open_pnl": round(p["open_pnl"], 2),
            "realised": round(realised, 2), "closed": closed, "open": p["open_trades"], "wallet_equity": p["total_equity"]}


async def universe(now: float | None = None) -> dict[str, dict]:
    """Every Hyperliquid perp (native and HIP-3) with its size decimals and max leverage, cached six hours."""
    now = now or time.time()
    cached = kv.get_sync(UNIVERSE_KEY) or {}
    if cached.get("at", 0) > now - UNIVERSE_TTL and cached.get("coins"):
        return cached["coins"]
    from data import hyperliquid_client as hl
    coins: dict[str, dict] = {}
    for dex in hl.DEXES:
        body = {"type": "meta"} if not dex else {"type": "meta", "dex": dex}
        meta = await hl._post(body)
        for x in (meta or {}).get("universe", []):
            if not x.get("isDelisted"):
                coins[x["name"]] = {"szDecimals": int(x.get("szDecimals", 3)), "maxLeverage": x.get("maxLeverage")}
    if coins:
        kv.set_sync(UNIVERSE_KEY, {"at": now, "coins": coins})
    return coins or cached.get("coins", {})


def _funding(led: dict) -> bool:
    """The capital plan hasn't filled the wallet yet (a fresh book starts at the base balance): nothing trades until it has."""
    return not led["closed"] and not led["open"] and led["wallet_equity"] < R.PLAN["size"] * 0.98


def can_enter(st: dict, led: dict, assess: dict, source: str, symbol: str) -> str | None:
    """Why the book would refuse a leg now, or None."""
    if st["phase"] not in ("evaluation", "funded"):
        return f"attempt {st['attempt']} is {st['phase']}"
    if _funding(led):
        return "waiting for the capital plan to fund the wallet"
    if st.get("halted_day") == R.day_key(time.time()):
        return f"halted for the day: {st.get('halt_why')}"
    if assess["action"] != "ok":
        return f"{assess['action']}: {assess['why']}"
    if len(led["open"]) >= R.PLAN["max_open"]:
        return f"the firm's {R.PLAN['max_open']} open positions are on"
    mine = [t for t in led["open"] if t.get("source") == source]
    if len(mine) >= R.MAX_OPEN_BY_SOURCE[source]:
        return f"{source} already has {len(mine)} open (our cap {R.MAX_OPEN_BY_SOURCE[source]})"
    if any(t["symbol"] == symbol for t in led["open"]):
        return f"{symbol} already open"
    return None


async def _enter(source: str, symbol: str, direction: str, px: float, stop: float, target: float | None, why: str,
                 expires_at: float | None, extra: dict, now: float) -> dict | None:
    if not (books.ENABLED and book_switches.enabled(BOOK)):
        return None
    w = _wallet()
    st = _state(now)
    led = ledger(w, st)
    a = R.assess(led["equity"], R.PLAN["size"], st["sod_balance"])
    refused = can_enter(st, led, a, source, symbol)
    if refused:
        _log({"ts": now, "event": "refused", "source": source, "symbol": symbol, "direction": direction, "why": refused})
        return None
    coin = R.resolve(symbol, await universe(now))
    if not coin:
        _log({"ts": now, "event": "refused", "source": source, "symbol": symbol, "direction": direction, "why": "not a Hyperliquid perp"})
        return None
    cap = R.leverage_cap(coin)
    notional, lev = R.size(led["equity"], px, stop, source, cap)
    if notional < 10:
        _log({"ts": now, "event": "refused", "source": source, "symbol": symbol, "direction": direction, "why": "size under $10"})
        return None
    s = 1 if direction == "long" else -1
    tp = target if target else px * (1 + s * 0.9)
    t = w.open_trade(symbol=symbol, direction=direction, entry_price=px, stop_loss=stop, take_profit_1=tp, take_profit_2=tp,
                     notional_usd=notional, leverage=lev, timeframe="1d" if source == "reversal" else "5m", oracle_confidence=0,
                     oracle_reasoning=why[:200], hold_duration=f"{REVERSAL_HOLD_H:g}h" if source == "reversal" else "to the level, the stop or the close",
                     extra={"strategy": BOOK, "source": source, "exit_policy": "manual", "shadows": False, "claude_verdict": "skipped", "entry_type": "market",
                            "near_event": __import__("ops.calendar", fromlist=["near_event"]).near_event(symbol),
                            "prop_attempt": st["attempt"], "hl_coin": coin, "leverage_cap": cap, "risk_pct": R.RISK_PCT[source],
                            "equity_at_entry": led["equity"], **extra})
    if t.get("error"):
        _log({"ts": now, "event": "refused", "source": source, "symbol": symbol, "direction": direction, "why": t["error"][:120]})
        return None
    if expires_at:
        w.annotate(t["id"], expires_at=expires_at)
    _log({"ts": now, "event": "opened", "source": source, "symbol": symbol, "direction": direction, "coin": coin, "notional": notional,
          "leverage": lev, "stop": stop, "equity": led["equity"]})
    return t


async def mirror_reversal(picks: list[tuple[str, str]], scores: dict[str, float], now: float | None = None) -> list[dict]:
    """The reversal lane's picks of this 4-hour slot, as the firm's account would take them."""
    now = now or time.time()
    out = []
    for sym, direction in picks:
        px = await monitor.live_price(sym)
        if not px:
            continue
        s = 1 if direction == "long" else -1
        t = await _enter("reversal", sym, direction, px, px * (1 - s * REVERSAL_STOP_PCT / 100), None,
                         f"reversal score {scores.get(sym, 0):+.3f}: {direction} {sym}", now + REVERSAL_HOLD_H * 3600,
                         {"reversal_score": scores.get(sym)}, now)
        if t:
            out.append(t)
    return out


async def mirror_auction(sym: str, direction: str, px: float, stop: float, target: float, expires_at: float, why: str, meta: dict,
                         now: float | None = None) -> dict | None:
    """One auction entry, as the firm's account would take it."""
    return await _enter("auction", sym, direction, px, stop, target, why, expires_at, meta, now or time.time())


async def flatten(reason: str, now: float) -> list[dict]:
    w = _wallet()
    out = []
    for t in w.get_portfolio()["open_trades"]:
        price = await monitor.live_price(t["symbol"]) or t.get("current_price") or t["entry_price"]
        done = w.close_trade_manually(t["id"], price, f"prop: {reason}")
        if done:
            out.append({"symbol": t["symbol"], "pnl_usd": done.get("pnl_usd")})
    return out


async def tick(now: float | None = None) -> dict:
    """Every minute: roll the firm's day, read the account against its floors, act on ours, note what the firm
    would have done, mirror to the API when it is on."""
    now = now or time.time()
    if not books.ENABLED:
        return {}
    w = _wallet()
    st = _state(now)
    led = ledger(w, st)
    today = R.day_key(now)
    if st.get("day") != today:
        st["day"], st["sod_balance"], st["halted_day"], st["halt_why"] = today, led["balance"], None, None
        _log({"ts": now, "event": "day", "sod_balance": led["balance"]})
    a = R.assess(led["equity"], R.PLAN["size"], st["sod_balance"])
    st["min_equity"], st["max_equity"] = min(st.get("min_equity", led["equity"]), led["equity"]), max(st.get("max_equity", led["equity"]), led["equity"])
    if a["breach"] and not st.get("breach"):
        st["breach"] = {"ts": now, "rule": a["breach"], "equity": led["equity"]}
        _log({"ts": now, "event": "breach", "rule": a["breach"], "equity": led["equity"], "note": "the firm would have closed the account here"})
    if st["phase"] == "evaluation" and R.passed(led["balance"], R.PLAN["size"]):
        st["phase"], st["passed_at"] = "funded", now
        _log({"ts": now, "event": "passed", "balance": led["balance"], "days": round((now - st["started_at"]) / 86400, 1)})
    acted = None
    if st["phase"] in ("evaluation", "funded") and not _funding(led):
        if a["action"] == "flatten" and led["open"]:
            closed = await flatten(a["why"], now)
            st["flattens"] = st.get("flattens", 0) + 1
            acted = {"flattened": closed, "why": a["why"]}
            _log({"ts": now, "event": "flatten", "why": a["why"], "closed": closed, "equity": led["equity"]})
        if a["action"] in ("halt", "flatten") and st.get("halted_day") != today:
            st["halted_day"], st["halt_why"] = today, a["why"]
            st["halts"] = st.get("halts", 0) + 1
            _log({"ts": now, "event": "halt", "why": a["why"], "equity": led["equity"]})
        if led["equity"] <= a["floors"]["flatten_dd"] and st["phase"] != "stopped":
            st["phase"], st["stopped"] = "stopped", {"ts": now, "why": a["why"], "equity": led["equity"]}
            _log({"ts": now, "event": "stopped", "why": f"{R.FLATTEN_DD_PCT}% of the {R.PLAN['max_dd_pct']}% drawdown used — the attempt ends on our rule",
                  "equity": led["equity"]})
    _save(st)
    if prop_exec.enabled():
        try:
            await _sync_exec(w, led, now)
        except Exception as e:
            logger.warning("Prop API sync failed: %s", e)
    return {"assess": a, "ledger": {k: led[k] for k in ("balance", "equity", "open_pnl")}, "acted": acted, "phase": st["phase"]}


async def _sync_exec(w, led: dict, now: float) -> None:
    """Paper → firm: open what the paper book opened, close what it closed, each once, three tries."""
    client = prop_exec.Client()
    account = await client.account_id()
    if not account:
        return
    uni = await universe(now)
    for t in led["open"]:
        ex = t.get("exec") or {}
        if ex.get("opened") or ex.get("tries", 0) >= 3:
            continue
        coin = t.get("hl_coin") or R.resolve(t["symbol"], uni)
        try:
            await client.set_leverage(account, coin, int(t.get("leverage_cap") or R.leverage_cap(coin)))
            orders = await client.open(account, coin, t["direction"], R.quantity(t["notional_usd"], t["entry_price"], uni.get(coin, {}).get("szDecimals", 3)), t["stop_loss"])
            w.annotate(t["id"], exec={"opened": now, "orders": [o.get("orderId") for o in orders], "account": account, "mode": prop_exec.MODE})
        except Exception as e:
            w.annotate(t["id"], exec={**ex, "tries": ex.get("tries", 0) + 1, "error": str(e)[:160]})
            _log({"ts": now, "event": "exec_error", "symbol": t["symbol"], "why": str(e)[:160]})
    for t in led["closed"]:
        ex = t.get("exec") or {}
        if not ex.get("opened") or ex.get("closed") or ex.get("close_tries", 0) >= 3 or _ts(t.get("closed_at")) < now - 3600:
            continue
        coin = t.get("hl_coin") or R.resolve(t["symbol"], uni)
        try:
            await client.close(account, coin, t["direction"], R.quantity(t["notional_usd"], t["entry_price"], uni.get(coin, {}).get("szDecimals", 3)))
            w.annotate(t["id"], exec={**ex, "closed": now})
        except Exception as e:
            w.annotate(t["id"], exec={**ex, "close_tries": ex.get("close_tries", 0) + 1, "error": str(e)[:160]})
            _log({"ts": now, "event": "exec_error", "symbol": t["symbol"], "why": str(e)[:160]})


async def reset(note: str = "new attempt") -> dict:
    """Start a new attempt (what buying another challenge would be): close everything, keep the old attempt's record."""
    now = time.time()
    st = _state(now)
    closed = await flatten("reset", now)
    led = ledger(_wallet(), st)
    st.setdefault("attempts", []).append({"attempt": st["attempt"], "started_at": st["started_at"], "ended_at": now, "phase": st["phase"],
                                          "balance": led["balance"], "breach": st.get("breach"), "note": note})
    new = _fresh(time.time() + 0.01)                      # after the flatten's closes, so the new ledger starts clean
    new["attempt"], new["attempts"] = st["attempt"] + 1, st["attempts"][-20:]
    _save(new)
    _log({"ts": now, "event": "reset", "attempt": new["attempt"], "note": note, "closed": closed})
    return status()


def status() -> dict:
    now = time.time()
    w = _wallet()
    st = _state(now)
    led = ledger(w, st)
    a = R.assess(led["equity"], R.PLAN["size"], st["sod_balance"])
    closed = led["closed"]
    by_source = {}
    for src in ("reversal", "auction"):
        mine = [t for t in closed if t.get("source") == src]
        wins = sum(1 for t in mine if (t.get("pnl_usd") or 0) > 0)
        by_source[src] = {"n": len(mine), "pnl": round(sum(t.get("pnl_usd") or 0 for t in mine), 2), "wins": wins,
                          "open": sum(1 for t in led["open"] if t.get("source") == src), "cap": R.MAX_OPEN_BY_SOURCE[src], "risk_pct": R.RISK_PCT[src]}
    days = (now - st["started_at"]) / 86400
    log = kv.get_sync(LOG_KEY) or []
    return {
        "plan": R.PLAN, "attempt": st["attempt"], "phase": "funding" if _funding(led) else st["phase"], "started_at": st["started_at"],
        "days": round(days, 1), "passed_at": st.get("passed_at"), "breach": st.get("breach"), "stopped": st.get("stopped"),
        "balance": led["balance"], "equity": led["equity"], "open_pnl": led["open_pnl"], "realised": led["realised"],
        "sod_balance": st["sod_balance"], "min_equity": st.get("min_equity"), "max_equity": st.get("max_equity"),
        "target": {"usd": R.PLAN["size"] * (1 + R.PLAN["target_pct"] / 100), "progress_pct": R.target_progress_pct(led["balance"], R.PLAN["size"]),
                   "to_go_usd": round(R.PLAN["size"] * (1 + R.PLAN["target_pct"] / 100) - led["balance"], 2)},
        "assess": a, "halted_day": st.get("halted_day"), "halt_why": st.get("halt_why"), "halts": st.get("halts", 0), "flattens": st.get("flattens", 0),
        "ours": {"halt_daily_pct": R.HALT_DAILY_PCT, "flatten_daily_pct": R.FLATTEN_DAILY_PCT, "halt_dd_pct": R.HALT_DD_PCT, "flatten_dd_pct": R.FLATTEN_DD_PCT,
                 "risk_pct": R.RISK_PCT, "max_open_by_source": R.MAX_OPEN_BY_SOURCE},
        "leverage": {"default": R.LEVERAGE_DEFAULT, "overrides": R.LEVERAGE},
        "open": [{"id": t["id"], "symbol": t["symbol"], "coin": t.get("hl_coin"), "direction": t["direction"], "source": t.get("source"),
                  "notional": t.get("notional_usd"), "leverage": t.get("leverage"), "entry": t["entry_price"], "stop": t.get("stop_loss"),
                  "pnl_usd": t.get("pnl_usd"), "opened_at": t.get("opened_at"), "exec": t.get("exec")} for t in led["open"]],
        "closed_n": len(closed), "closed_pnl": round(sum(t.get("pnl_usd") or 0 for t in closed), 2),
        "wins": sum(1 for t in closed if (t.get("pnl_usd") or 0) > 0), "by_source": by_source,
        "recent": [{"symbol": t["symbol"], "direction": t["direction"], "source": t.get("source"), "pnl_usd": t.get("pnl_usd"),
                    "close_reason": t.get("close_reason"), "closed_at": t.get("closed_at")} for t in closed[-15:]][::-1],
        "log": log[-60:][::-1], "attempts": st.get("attempts", []), "exec": prop_exec.describe(),
    }


def config() -> dict:
    return {"plan": R.PLAN, "leverage": {"default": R.LEVERAGE_DEFAULT, "overrides": R.LEVERAGE}, "ours": status()["ours"], "exec": prop_exec.describe()}
