"""All three trading systems on one footing — the Trades page and the Portfolio metrics.

  main     the swing wallet you trade from the Signal tab (plus its auto-execute)
  auto     the autotrader's separate swing wallet
  scalper  the scalping engine's wallet

Every trade, open or closed, is normalised to one row shape so a single table can list,
filter and page through all of them. The Portfolio metrics are computed from the same rows
plus each wallet's recorded equity curve, per system and for everything combined.

P&L conventions: `net_pnl` is what the wallet actually gained (fees and funding included);
`gross_pnl` is the price move alone; `fees` and `funding` are costs (positive = paid).
"""

import math
import sqlite3
import time
from datetime import datetime, timezone

from scalper import config as SC
from storage.db import SWING_DB
from experiments import books as _experiments
from trading import capital as _capital, history

SYSTEMS = {"main": "Main wallet", "auto": "Autotrader", "scalper": "Scalper", "impulse": "Impulse scalper",
           "main_live": "Main wallet · live", "auto_live": "Autotrader · live", "scalper_live": "Scalper · live"}
SYSTEMS.update(_experiments.LABELS)          # side experiments: listed once they have trades, never in the paper total
SYSTEMS["hold72"] = "Hold vs BTC (24h)"
SYSTEMS["fade24"] = "Fade the scanner (24h vs BTC)"
SYSTEMS["events"] = "Event shorts (listings & delistings)"
SYSTEMS["carry_q"] = "Carry Q · quarterly basis"
SYSTEMS["carry"] = "Carry · BTC, ETH & gold"
SYSTEMS["unlocks"] = "Unlock shorts (cliff unlocks vs BTC)"
SYSTEMS["vol_premium"] = "Vol premium (weekly BTC/ETH/SOL condors)"
from markets import engine as _markets                     # noqa: E402 — stocks / futures books
SYSTEMS.update(_markets.LABELS)
PAPER_SYSTEMS = ("main", "auto", "scalper", "impulse")
# Signal ledgers (2026-10-31, "the autotrader executes the evidence"): these books still run their rules on their own paper
# wallets, but those wallets are SIGNALS — the autotrader mirrors every position on its active ledger, where the trade
# counts. Their rows are kept out of the Trades and Portfolio views and the tracker unless asked for, so a trade shows once.
# Books with their own page (2026-11-01): the mentor's system is a separate project with its own tab; its trades stay out of
# the Trades tab's default view and are shown only when that system is asked for.
STANDALONE_SYSTEMS = {"auction": "the mentor's system — see its own tab"}
SIGNAL_SYSTEMS = {"cascade": "signal ledger — executed by the autotrader's cascade lane", "weekly_rs": "signal ledger — executed by the autotrader's weekly lane",
                  "multi_factor": "signal ledger — executed by the autotrader's factor lane", "events": "signal ledger — executed by the autotrader's event lane",
                  "unlocks": "signal ledger — executed by the autotrader's unlock lane"}
# Books of retired strategies (see experiments/retired.py). Their trades stay on record but are left
# out of the Trades and Portfolio views — and the combined paper total — unless asked for.
RETIRED_SYSTEMS = {"scalper": "order-flow scalper, retired 2026-09-29", "scalper_live": "order-flow scalper, retired 2026-09-29",
                   "impulse": "impulse scalper, retired 2026-10-06",
                   # Switched off after failing (or never trading) — see trading/book_switches.RETIRED for why.
                   "auto_rules": "autotrader rules twin, retired 2026-10-17", "signals_rules": "signals · rules only, retired 2026-10-21",
                   "signals_claude": "signals · Claude, retired 2026-10-25", "hold72": "hold vs BTC, retired 2026-10-24", "fade24": "fade the scanner, retired 2026-10-28",
                   "stocks_trend": "stocks trend book, retired 2026-10-30", "stocks_rules": "stocks scanner rules, retired 2026-10-12", "futures_rules": "futures scanner rules, retired 2026-10-12",
                   "stocks_claude": "stocks Claude-confirmed, retired 2026-10-19", "futures_claude": "futures Claude-confirmed, retired 2026-10-19",
                   "claude_swing": "Claude swing, retired 2026-11-02", "copy_futures": "copy trading · commodity perps, retired 2026-11-03",
                   **{f"copy_{m}_{v}": f"copy variant wallet, folded into the {m} copy book 2026-11-02" for m in ("crypto", "stocks", "futures") for v in ("wide", "fast", "late")}}
SORTABLE = {"closed_ts", "opened_ts", "net_pnl", "gross_pnl", "r", "hold_min", "symbol", "notional",
            "system", "direction", "exit_reason", "entry_price", "exit_price", "outcome", "costs", "leverage"}


def _ts(iso) -> float | None:
    if iso is None:
        return None
    if isinstance(iso, (int, float)):
        return float(iso)
    try:
        d = datetime.fromisoformat(iso)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


# ── Normalised rows ────────────────────────────────────────────────────────────

def _lane_of(t: dict) -> str:
    from autotrader.lanes import lane_of
    return lane_of(t)


def _swing_row(t: dict, system: str) -> dict:
    opened, closed = _ts(t.get("opened_at")), _ts(t.get("closed_at"))
    fees, funding, net = t.get("fees_usd") or 0.0, t.get("funding_usd") or 0.0, t.get("pnl_usd") or 0.0
    risk = t.get("risk_usd") or 0
    is_open = t.get("status") == "open"
    return {
        "id": t["id"], "system": system, "symbol": t["symbol"], "direction": t["direction"],
        "status": "open" if is_open else "closed",
        "opened_ts": opened, "closed_ts": None if is_open else closed,
        "hold_min": _r(((closed if not is_open else time.time()) - opened) / 60, 1) if opened else None,
        "entry_price": t.get("entry_price"), "exit_price": None if is_open else t.get("current_price"),
        "mark_price": t.get("current_price") if is_open else None,
        "stop_loss": t.get("stop_loss"), "take_profit": t.get("take_profit_2"),
        "notional": _r(t.get("notional_usd") or t.get("position_value_usd")
                       or (t.get("quantity") or 0) * (t.get("entry_price") or 0)), "leverage": t.get("leverage"),
        "margin": _r(t.get("margin_usd")), "risk_usd": _r(risk),
        "gross_pnl": _r(net + fees + funding), "fees": _r(fees), "funding": _r(funding), "net_pnl": _r(net),
        "r": _r(net / risk) if risk else None,
        "exit_reason": None if is_open else t.get("close_reason"),
        "outcome": None if is_open else history.outcome(t),
        "timeframe": t.get("timeframe"),
        "note": t.get("claude_thesis") or t.get("oracle_reasoning") or "",
        # The autotrader's lane (scanner / event short / unlock short / weekly basket) — which strategy placed it.
        "lane": _lane_of(t) if system.startswith("auto") else None,
        # The mentor's book: which level was accepted, which level was the target, and the trade light's grade.
        **({"level_name": t.get("level_name"), "target_name": t.get("target_name"), "grade": t.get("grade")} if system == "auction" else {}),
        "mfe_r": t.get("peak_r"),
        "quantity": t.get("quantity"), "liquidation": t.get("liquidation_price"), "tp_set": t.get("tp_set", True),
    }


def _scalp_row(t: dict, system: str | None = None) -> dict:
    return {
        "id": t["id"], "system": system or ("scalper_live" if t.get("mode") == "live" else "scalper"), "symbol": t["symbol"], "direction": t["side"], "status": "closed",
        "opened_ts": t["entry_ts"], "closed_ts": t["exit_ts"], "hold_min": _r((t["hold_s"] or 0) / 60, 2),
        "entry_price": t["entry_price"], "exit_price": t["exit_price"], "mark_price": None,
        "stop_loss": t["sl"], "take_profit": t["tp"],
        "notional": _r(t["notional"]), "leverage": t["leverage"], "margin": _r(t["margin"]),
        "risk_usd": _r(t["risk_usd"]),
        "gross_pnl": _r(t["gross_pnl"]), "fees": _r(t["fees"]), "funding": _r(t["funding"]),
        "net_pnl": _r(t["net_pnl"]), "r": _r(t["r"]),
        "exit_reason": t["exit_reason"], "outcome": "win" if (t["net_pnl"] or 0) > 0 else "loss",
        "timeframe": "scalp", "note": "",
        # Best move in our favour while open, in R (stop distances) — how much the exit kept.
        "mfe_r": _r(t["mfe_bps"] / t["sl_bps"], 3) if t.get("mfe_bps") is not None and t.get("sl_bps") else None,
    }


def _scalper_con():
    return sqlite3.connect(SC.TRADES_DB, timeout=10)


def all_rows() -> tuple[list[dict], dict]:
    """Every trade from every system, plus each system's starting balance."""
    rows, initial = [], {}
    for system, wallet in history.WALLETS.items():
        data = wallet.all_trades()
        initial[system] = data["initial_balance"]
        rows += [_swing_row({**t, "status": "closed"}, system) for t in data["closed"]]
        rows += [_swing_row({**t, "status": "open"}, system) for t in data["open"]]

    # Live swing ledgers — listed only once they've been used.
    from live import swing as live_swing
    for system, wallet in live_swing.LIVE.items():
        data = wallet.all_trades()
        if data["closed"] or data["open"]:
            initial[f"{system}_live"] = data["initial_balance"]
            rows += [_swing_row({**t, "status": "closed"}, f"{system}_live") for t in data["closed"]]
            rows += [_swing_row({**t, "status": "open"}, f"{system}_live") for t in data["open"]]

    initial["scalper"] = SC.INITIAL_BALANCE
    try:
        con = _scalper_con()
        con.row_factory = sqlite3.Row
        rows += [_scalp_row(dict(r)) for r in con.execute("SELECT * FROM trades")]
        live_init = con.execute("SELECT value FROM state WHERE key='live_initial_balance'").fetchone()
    except sqlite3.Error:
        live_init = None
    # The order-flow scalper was retired on 2026-09-29: its trades stay as a record, nothing is open.
    if "scalper_live" not in initial and any(r["system"] == "scalper_live" for r in rows):
        initial["scalper_live"] = float(live_init[0]) if live_init else 0.0

    # Side experiments (experiments/books.py) — once they've traded.
    for name, wallet in _experiments.WALLETS.items():
        data = wallet.all_trades()
        if data["closed"] or data["open"]:
            initial[name] = data["initial_balance"]
            rows += [_swing_row({**t, "status": "closed"}, name) for t in data["closed"]]
            rows += [_swing_row({**t, "status": "open"}, name) for t in data["open"]]

    # Hold-72 pairs (coin vs BTC, its own ledger) — once it has traded.
    from experiments import hold72
    h_rows = hold72.rows()
    if h_rows:
        initial["hold72"] = hold72.INITIAL_BALANCE
        rows += h_rows
    from experiments import fade24
    f_rows = fade24.rows()
    if f_rows:
        initial["fade24"] = fade24.INITIAL_BALANCE
        rows += f_rows
    from experiments import events
    e_rows = events.rows()
    if e_rows:
        initial["events"] = _capital.initial("events", events.INITIAL_BALANCE)
        rows += e_rows
    elif "events" in _capital.PLAN:
        # Holds its share of the programme's capital between events — part of the whole portfolio even before a trade.
        initial["events"] = _capital.initial("events", events.INITIAL_BALANCE)
    from experiments import carry_q
    q_rows = carry_q.rows()
    if q_rows:
        initial["carry_q"] = _capital.initial("carry_q", carry_q.INITIAL_BALANCE)
        rows += q_rows
    from experiments import unlocks, vol_premium
    for mod, name in ((unlocks, "unlocks"), (vol_premium, "vol_premium")):
        m_rows = mod.rows()
        if m_rows:
            initial[name] = _capital.initial(name, mod.INITIAL_BALANCE)
            rows += m_rows

    # Stocks and futures books (markets/) — once they've traded.
    for name, wallet in _markets.WALLETS.items():
        data = wallet.all_trades()
        if data["closed"] or data["open"]:
            initial[name] = data["initial_balance"]
            rows += [_swing_row({**t, "status": "closed"}, name) for t in data["closed"]]
            rows += [_swing_row({**t, "status": "open"}, name) for t in data["open"]]

    # Cash-and-carry positions (long spot / short perp, its own ledger) — once it has traded.
    from experiments import carry
    c_rows = carry.rows()
    if c_rows:
        initial["carry"] = _capital.initial("carry", carry.INITIAL_BALANCE)
        rows += c_rows

    # The impulse scalper's record (retired 2026-10-06 — nothing is open).
    initial["impulse"] = SC.IMPULSE_BALANCE
    try:
        con = sqlite3.connect(SC.IMPULSE_TRADES_DB, timeout=10)
        con.row_factory = sqlite3.Row
        rows += [_scalp_row(dict(r), "impulse") for r in con.execute("SELECT * FROM trades")]
    except sqlite3.Error:
        pass
    return rows, initial


# ── Trades page ────────────────────────────────────────────────────────────────

def matches(r: dict, f: dict) -> bool:
    """The Trades page filters — shared by the table and its charts."""
    if (r["system"] in RETIRED_SYSTEMS or r["system"] in SIGNAL_SYSTEMS) and not f.get("include_retired"):
        return False
    if f.get("system") and r["system"] != f["system"]:
        return False
    if f.get("mode") and ("live" if r["system"].endswith("_live") else "paper") != f["mode"]:
        return False
    if f.get("status") and r["status"] != f["status"]:
        return False
    if f.get("symbol") and r["symbol"] != f["symbol"]:
        return False
    if f.get("direction") and r["direction"] != f["direction"]:
        return False
    if f.get("result") == "win" and not (r["net_pnl"] or 0) > 0:
        return False
    if f.get("result") == "loss" and (r["net_pnl"] or 0) > 0:
        return False
    if f.get("exit_reason") and r["exit_reason"] != f["exit_reason"]:
        return False
    ts = r["closed_ts"] or r["opened_ts"] or 0
    if f.get("date_from") and ts < f["date_from"]:
        return False
    if f.get("date_to") and ts > f["date_to"]:
        return False
    return True



def trades(filters: dict, sort: str = "closed_ts", order: str = "desc", page: int = 1, page_size: int = 50) -> dict:
    rows, _ = all_rows()
    standalone_asked = filters.get("system") in STANDALONE_SYSTEMS
    hidden = 0 if filters.get("include_retired") else sum(1 for r in rows if r["system"] in RETIRED_SYSTEMS or r["system"] in SIGNAL_SYSTEMS
                                                           or (r["system"] in STANDALONE_SYSTEMS and not standalone_asked))
    rows = [r for r in rows if filters.get("include_retired") or (r["system"] not in RETIRED_SYSTEMS and r["system"] not in SIGNAL_SYSTEMS
                                                                  and (r["system"] not in STANDALONE_SYSTEMS or standalone_asked))]
    facets = {"symbols": sorted({r["symbol"] for r in rows}),
              "exit_reasons": sorted({r["exit_reason"] for r in rows if r["exit_reason"]})}
    open_rows = [r for r in rows if r["status"] == "open"]

    selected = [r for r in rows if matches(r, filters)]
    key = sort if sort in SORTABLE else "closed_ts"
    # Open trades have no close time: sort them by when they opened, ahead of closed ones.
    if key == "closed_ts":
        val = lambda r: r["closed_ts"] or r["opened_ts"] or 0
    elif key == "costs":                                   # fees + funding, the Trades page's "Costs" column
        val = lambda r: (r.get("fees") or 0) + (r.get("funding") or 0)
    else:
        val = lambda r: r.get(key)
    present = [r for r in selected if val(r) is not None]
    missing = [r for r in selected if val(r) is None]
    present.sort(key=val, reverse=order == "desc")
    selected = present + missing

    closed = [r for r in selected if r["status"] == "closed"]
    totals = {
        "trades": len(closed), "net_pnl": _r(sum(r["net_pnl"] or 0 for r in closed)),
        "gross_pnl": _r(sum(r["gross_pnl"] or 0 for r in closed)),
        "fees": _r(sum(r["fees"] or 0 for r in closed)), "funding": _r(sum(r["funding"] or 0 for r in closed)),
        "wins": sum(1 for r in closed if (r["net_pnl"] or 0) > 0),
    }
    page_size = max(1, min(page_size, 200))
    total = len(selected)
    pages = max(1, -(-total // page_size))
    page = max(1, min(page, pages))
    return {"rows": selected[(page - 1) * page_size: page * page_size], "total": total, "page": page,
            "pages": pages, "page_size": page_size, "facets": facets, "totals": totals,
            "open_count": {s: sum(1 for r in open_rows if r["system"] == s) for s in SYSTEMS
                           if (s in PAPER_SYSTEMS and (filters.get("include_retired") or s not in RETIRED_SYSTEMS))
                           or any(r["system"] == s for r in rows)},
            "retired_hidden": hidden, "retired_systems": RETIRED_SYSTEMS}


# ── Portfolio metrics ──────────────────────────────────────────────────────────

def _equity_series(system: str) -> list[tuple[float, float]]:
    try:
        if system == "impulse":
            con = sqlite3.connect(SC.IMPULSE_TRADES_DB, timeout=10)
            return [(ts, eq) for ts, eq in con.execute("SELECT ts, equity FROM equity ORDER BY ts")]
        if system.startswith("scalper"):
            con = _scalper_con()
            cond = "mode = 'live'" if system == "scalper_live" else "COALESCE(mode, 'paper') = 'paper'"
            return [(ts, eq) for ts, eq in con.execute(f"SELECT ts, equity FROM equity WHERE {cond} ORDER BY ts")]
        from storage.db import connect
        con = connect(SWING_DB)
        try:
            return [(_ts(ts), eq) for ts, eq in con.execute(
                "SELECT ts, equity FROM equity WHERE label=? ORDER BY ts", (system,)).fetchall()]
        finally:
            con.close()
    except Exception:
        return []


def _daily(series: list[tuple[float, float]]) -> list[float]:
    """Last equity of each UTC day."""
    by_day: dict[str, float] = {}
    for ts, eq in series:
        if ts:
            by_day[datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")] = eq
    return list(by_day.values())


def _drawdown(series: list[tuple[float, float]]) -> dict:
    peak, max_dd, max_dd_pct, peak_ts, dd_start, longest = None, 0.0, 0.0, None, None, 0.0
    for ts, eq in series:
        if peak is None or eq >= peak:
            if dd_start is not None and ts:
                longest = max(longest, ts - dd_start)
            peak, peak_ts, dd_start = eq, ts, None
        else:
            dd_start = dd_start or peak_ts
            max_dd = max(max_dd, peak - eq)
            max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100 if peak else 0)
    cur = series[-1][1] if series else None
    if dd_start is not None and series and series[-1][0]:
        longest = max(longest, series[-1][0] - dd_start)
    return {"max_drawdown_usd": _r(max_dd), "max_drawdown_pct": _r(max_dd_pct),
            "current_drawdown_pct": _r((peak - cur) / peak * 100) if peak and cur is not None else None,
            "longest_drawdown_h": _r(longest / 3600, 1)}


def _ratios(series: list[tuple[float, float]]) -> dict:
    days = _daily(series)
    rets = [b / a - 1 for a, b in zip(days, days[1:]) if a]
    if len(rets) < 5:
        return {"sharpe": None, "sortino": None, "days": len(days)}
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    down = [min(r, 0) for r in rets]
    dsd = math.sqrt(sum(d * d for d in down) / len(down))
    return {"sharpe": _r(mean / sd * math.sqrt(365)) if sd else None,
            "sortino": _r(mean / dsd * math.sqrt(365)) if dsd else None, "days": len(days)}


def _streaks(closed: list[dict]) -> dict:
    """Runs of consecutive wins (+n) or losses (−n), in close order."""
    run, best_w, best_l = 0, 0, 0
    for r in sorted(closed, key=lambda r: r["closed_ts"] or 0):
        if (r["net_pnl"] or 0) > 0:
            run = run + 1 if run > 0 else 1
            best_w = max(best_w, run)
        else:
            run = run - 1 if run < 0 else -1
            best_l = max(best_l, -run)
    return {"current_streak": run, "longest_win_streak": best_w, "longest_loss_streak": best_l}


def metrics(rows: list[dict], initial: float, series: list[tuple[float, float]]) -> dict:
    closed = [r for r in rows if r["status"] == "closed"]
    open_ = [r for r in rows if r["status"] == "open"]
    net = [r["net_pnl"] or 0 for r in closed]
    wins, losses = [x for x in net if x > 0], [x for x in net if x <= 0]
    realized, unrealized = sum(net), sum(r["net_pnl"] or 0 for r in open_)
    equity = series[-1][1] if series else initial + realized + unrealized
    gross_win, gross_loss = sum(wins), -sum(losses)
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    fees = sum(r["fees"] or 0 for r in closed)
    funding = sum(r["funding"] or 0 for r in closed)
    gross = sum(r["gross_pnl"] or 0 for r in closed)
    rs = [r["r"] for r in closed if r["r"] is not None]
    now = time.time()

    def pnl_since(sec):
        return _r(sum(r["net_pnl"] or 0 for r in closed if (r["closed_ts"] or 0) >= now - sec))

    def side(d):
        g = [r for r in closed if r["direction"] == d]
        return {"trades": len(g), "net_pnl": _r(sum(r["net_pnl"] or 0 for r in g)),
                "win_rate": _r(sum(1 for r in g if (r["net_pnl"] or 0) > 0) / len(g) * 100, 1) if g else None}

    by_symbol: dict[str, float] = {}
    for r in closed:
        by_symbol[r["symbol"]] = by_symbol.get(r["symbol"], 0) + (r["net_pnl"] or 0)
    ranked = sorted(by_symbol.items(), key=lambda kv: kv[1])
    hold_w = [r["hold_min"] for r in closed if (r["net_pnl"] or 0) > 0 and r["hold_min"] is not None]
    hold_l = [r["hold_min"] for r in closed if (r["net_pnl"] or 0) <= 0 and r["hold_min"] is not None]
    long_notional = sum(r["notional"] or 0 for r in open_ if r["direction"] == "long")
    short_notional = sum(r["notional"] or 0 for r in open_ if r["direction"] == "short")
    dd = _drawdown(series)
    payoff = avg_win / avg_loss if avg_loss else None

    return {
        "initial_balance": _r(initial), "equity": _r(equity),
        "total_return_pct": _r((equity - initial) / initial * 100) if initial else None,
        "realized_pnl": _r(realized), "unrealized_pnl": _r(unrealized),
        "pnl_24h": pnl_since(86400), "pnl_7d": pnl_since(7 * 86400), "pnl_30d": pnl_since(30 * 86400),
        "trades": len(closed), "open_positions": len(open_),
        "win_rate": _r(len(wins) / len(closed) * 100, 1) if closed else None,
        "profit_factor": _r(gross_win / gross_loss) if gross_loss else None,
        "payoff_ratio": _r(payoff),
        "breakeven_win_rate": _r(100 / (1 + payoff), 1) if payoff else None,
        "expectancy_usd": _r(realized / len(closed)) if closed else None,
        "avg_r": _r(sum(rs) / len(rs), 3) if rs else None, "total_r": _r(sum(rs)) if rs else None,
        "avg_win": _r(avg_win), "avg_loss": _r(-avg_loss),
        "best_trade": _r(max(net, default=0)), "worst_trade": _r(min(net, default=0)),
        "gross_pnl": _r(gross), "fees": _r(fees), "funding": _r(funding),
        "costs_pct_of_gross_wins": _r((fees + funding) / gross_win * 100, 1) if gross_win else None,
        **dd,
        "recovery_factor": _r(realized / dd["max_drawdown_usd"]) if dd["max_drawdown_usd"] else None,
        **_ratios(series),
        **_streaks(closed),
        "avg_hold_win_min": _r(sum(hold_w) / len(hold_w), 1) if hold_w else None,
        "avg_hold_loss_min": _r(sum(hold_l) / len(hold_l), 1) if hold_l else None,
        "long": side("long"), "short": side("short"),
        "best_symbol": {"symbol": ranked[-1][0], "net_pnl": _r(ranked[-1][1])} if ranked else None,
        "worst_symbol": {"symbol": ranked[0][0], "net_pnl": _r(ranked[0][1])} if ranked else None,
        "exposure": {
            "long_notional": _r(long_notional), "short_notional": _r(short_notional),
            "net_notional": _r(long_notional - short_notional),
            "gross_leverage": _r((long_notional + short_notional) / equity) if equity else None,
            "margin_used": _r(sum(r["margin"] or 0 for r in open_)),
            "open_risk_usd": _r(sum(r["risk_usd"] or 0 for r in open_)),
            "open_risk_pct": _r(sum(r["risk_usd"] or 0 for r in open_) / equity * 100) if equity else None,
        },
    }


def _thin(series: list[tuple[float, float]], n: int = 300) -> list[dict]:
    step = max(1, len(series) // n)
    pts = series[::step] + ([series[-1]] if series and (len(series) - 1) % step else [])
    return [{"ts": ts, "equity": _r(eq)} for ts, eq in pts if ts]


def portfolio(include_retired: bool = False) -> dict:
    rows, initial = all_rows()
    out, curves = {}, {}
    paper_systems = tuple(s for s in PAPER_SYSTEMS if include_retired or s not in RETIRED_SYSTEMS)
    present = [s for s in SYSTEMS if (s in paper_systems or s in initial)
               and (include_retired or (s not in RETIRED_SYSTEMS and s not in SIGNAL_SYSTEMS))]
    for system in present:
        series = _equity_series(system)
        curves[system] = _thin(series)
        out[system] = {"label": SYSTEMS[system],
                       **metrics([r for r in rows if r["system"] == system], initial.get(system, 0), series)}

    def combine(systems: tuple[str, ...]) -> list[tuple[float, float]]:
        """Sum the systems' equity on a shared hourly clock (forward-filled)."""
        cs = {s: curves[s] for s in systems if s in curves}
        hours = sorted({int(p["ts"] // 3600) for c in cs.values() for p in c})
        combined, last, idx = [], {s: initial.get(s, 0) for s in cs}, {s: 0 for s in cs}
        for h in hours:
            for s, c in cs.items():
                while idx[s] < len(c) and c[idx[s]]["ts"] // 3600 <= h:
                    last[s] = c[idx[s]]["equity"]
                    idx[s] += 1
            combined.append((h * 3600.0, sum(last.values())))
        return combined

    # "all": the main and autotrader paper wallets (the original paper total). "everything": every paper book —
    # the whole $50k programme. Live money is never folded into either — live ledgers stand on their own.
    combined = combine(paper_systems)
    out["all"] = {"label": "Main + autotrader (paper)",
                  **metrics([r for r in rows if r["system"] in paper_systems], sum(initial.get(s, 0) for s in paper_systems), combined)}
    curves["all"] = _thin(combined)
    every = tuple(s for s in present if not s.endswith("_live"))
    whole = combine(every)
    out["everything"] = {"label": "Whole portfolio (every paper book)",
                         **metrics([r for r in rows if r["system"] in every], sum(initial.get(s, 0) for s in every), whole)}
    curves["everything"] = _thin(whole)
    return {"systems": out, "curves": curves, "updated_at": time.time(), "include_retired": include_retired,
            "retired_systems": RETIRED_SYSTEMS}
