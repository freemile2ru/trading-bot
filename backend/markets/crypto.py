"""The Markets tab's crypto view, in the same shape as the stocks and futures views (markets/engine.summary).

Crypto has no separate market engine — its scans come from the main scanner (scanner/scheduler) and its books are the
crypto paper books — so this builds the same summary from those: every coin's latest scan with what the autotrader's
entry rules say about it, plus a card per crypto book.
"""
from __future__ import annotations

import time

from storage import state as kv
from trading import book_switches

LABEL = "Crypto"
# The crypto books with a paper wallet, in display order; the pair-ledger books (carry, events, unlocks, vol premium)
# have their own panels on the Lab tab.
BOOKS = ("auto", "main", "claude_swing", "weekly_rs", "cascade", "multi_factor", "copy_trading")


def _wallet(name: str):
    if name == "auto":
        from autotrader import engine
        return engine._active()
    if name == "main":
        from trading.paper_trader import main_wallet
        return main_wallet
    from experiments import books
    return books.WALLETS[name]


def _note(name: str) -> str:
    entry = next((b for b in book_switches.BOOKS if b["name"] == name), None)
    return (entry or {}).get("description") or ""


def _book(name: str) -> dict:
    p = _wallet(name).get_portfolio(closed_limit=200, history_limit=300)
    closed = p["closed_trades"]
    rs = [t["pnl_usd"] / t["risk_usd"] for t in closed if t.get("risk_usd")]
    label = next((b["label"] for b in book_switches.BOOKS if b["name"] == name), name)
    return {"name": name, "kind": "book", "label": label, "note": _note(name), "enabled": book_switches.enabled(name),
            "equity": p["total_equity"], "initial": p["initial_balance"], "balance": p["balance"],
            "return_pct": round((p["total_equity"] / p["initial_balance"] - 1) * 100, 2) if p["initial_balance"] else None,
            "open": p["open_trades"], "closed": len(closed), "wins": sum(1 for t in closed if t["pnl_usd"] > 0),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "net_pnl": round(sum(t["pnl_usd"] for t in closed), 2),
            "recent_closed": list(reversed(closed[-10:])), "equity_history": p.get("equity_history", [])[-300:]}


def _rules(sc: dict, state: dict | None) -> dict:
    """What the autotrader's rules say about a scan right now, in the shape the Markets tab's scan table reads."""
    from autotrader import strategy
    direction, why = strategy.bias(sc)
    out: dict = {"direction": direction, "reasons": why}
    if direction:
        try:
            _, fast = strategy.fast_tf(sc)
            if fast:
                t = strategy.timing(direction, fast, (state or {}).get("prev_fast"), sc)
                out.update(score=t["score"], enter=t["enter"], timing=t["reasons"])
        except Exception:
            pass
    if state:
        status = state.get("status")
        reasons = state.get("reasons") or []
        if status and status != "watching":
            out["claude"] = f"autotrader: {status.replace('_', ' ')}" + (f" — {reasons[-1]}" if reasons else "")
    return out


def summary(states: dict[str, dict] | None = None) -> dict:
    """`states` is the autotrader's per-coin state (symbol → state), read by the async route."""
    from autotrader import engine
    from scanner import scheduler
    states = states or {}
    scans = []
    for sc in scheduler.latest_full_scans():
        sym = sc.get("symbol")
        if not sym:
            continue
        scans.append({"symbol": sym, "base": sym.removesuffix("USDT"), "price": sc.get("price") or sc.get("current_price") or 0.0,
                      "conviction": sc.get("conviction") or 0.0, "confidence": sc.get("confidence") or 0, "best_timeframe": sc.get("best_timeframe"),
                      "scanned_at": sc.get("scanned_at") or sc.get("timestamp") or 0, "rules": _rules(sc, states.get(sym))})
    scans.sort(key=lambda s: -abs(s["conviction"]))
    st = scheduler._state
    return {"market": "crypto", "label": LABEL, "symbols": [s["symbol"] for s in scans], "books": [_book(n) for n in BOOKS], "scans": scans,
            "last_sweep": st.get("last_general"), "sweep_s": scheduler.GENERAL_INTERVAL,
            "claude_calls_today": int(kv.get_sync(engine._budget_key()) or 0),
            "claude_daily": engine.LLM_DAILY_BUDGET, "last_trend": None, "enabled": engine.ENABLED,
            "picks": None, "picks_per_day": 0, "pick_hour_utc": 0, "at": time.time()}
