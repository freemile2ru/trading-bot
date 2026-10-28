"""Trade history across both wallets — filtering, pagination and a backtest replay.

The table and the backtest are two views of one query: filters select a set of closed
trades, the table pages through it, and the backtest replays the whole set in close-time
order. Keeping them on the same selection means the chart always describes exactly the
rows the user is looking at.

The backtest is reported in two units:
  * USD — what the wallet actually made, sizing included.
  * R   — P&L divided by the risk taken at entry (distance to the original stop × size).
          This strips out position sizing, so trades from wallets of different sizes,
          or sized differently over time, compare on equal terms.
"""

from datetime import datetime, timezone

from autotrader.engine import auto_wallet
from trading.paper_trader import main_wallet

WALLETS = {"main": main_wallet, "auto": auto_wallet}


LEDGERS = ("both", "paper", "live")


def in_ledger(system: str, ledger: str = "both") -> bool:
    """Does a system's trade belong to the chosen ledger? (paper = its own simulated wallet,
    live = the ledger mirroring the Binance account, both = the decisions regardless of fills)."""
    return ledger == "both" or (system.endswith("_live") == (ledger == "live"))


def _ledgers(wallet: str, ledger: str = "both") -> list[tuple[str, object]]:
    """The paper wallet and/or live ledger of each selected system."""
    from live import swing as live_swing
    names = list(WALLETS) if wallet == "all" else [wallet]
    out = [(n, WALLETS[n]) for n in names] + [(f"{n}_live", live_swing.LIVE[n]) for n in names]
    return [(n, w) for n, w in out if in_ledger(n, ledger)]
SORTABLE = {"closed_at", "opened_at", "symbol", "pnl_usd", "pnl_pct", "r_multiple",
            "duration_minutes", "entry_price", "size_pct", "oracle_confidence"}


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def outcome(trade: dict) -> str:
    """One label per trade — how it ended, not just whether it made money."""
    reason = trade.get("close_reason")
    if reason in ("take_profit_2",):
        return "target"
    if reason in ("stop_loss", "tightened_stop"):
        return "stopped"
    if reason in ("breakeven", "expired", "left_watchlist", "signal_flip", "manual", "liquidated",
                  "stale", "trailing_stop", "window_exit", "thesis_faded"):
        return reason
    return "win" if trade.get("pnl_usd", 0) > 0 else "loss"


FA_ALIGNED = 0.2


def fa_bucket(trade: dict) -> str:
    """FA at entry relative to the trade's direction: with it, against it, or neither."""
    score = (trade.get("fa_at_entry") or {}).get("score")
    if score is None:
        return "unknown"
    aligned = score * (1 if trade["direction"] == "long" else -1)
    return "with" if aligned >= FA_ALIGNED else "against" if aligned <= -FA_ALIGNED else "neutral"


def _enrich(trade: dict, wallet: str) -> dict:
    opened, closed = _ts(trade.get("opened_at")), _ts(trade.get("closed_at"))
    risk = trade.get("risk_usd") or 0
    return {
        **trade,
        "wallet": wallet,
        "outcome": outcome(trade),
        "entry_type": trade.get("entry_type") or "market",
        "opened_ts": opened,
        "closed_ts": closed,
        "duration_minutes": round((closed - opened) / 60, 1) if opened and closed else None,
        "r_multiple": round(trade["pnl_usd"] / risk, 2) if risk else None,
        "exit_price": trade.get("current_price"),
        "fa_score": (trade.get("fa_at_entry") or {}).get("score"),
        "fa_bucket": fa_bucket(trade),
    }


def load(wallet: str = "all", ledger: str = "both") -> tuple[list[dict], float]:
    trades, capital = [], 0.0
    for name, w in _ledgers(wallet, ledger):
        data = w.all_trades()
        if name.endswith("_live") and not data["closed"]:
            continue
        capital += data["initial_balance"]
        trades += [_enrich(t, name) for t in data["closed"]]
    return trades, capital


def _matches(t: dict, f: dict) -> bool:
    if f.get("symbol") and t["symbol"] != f["symbol"]:
        return False
    if f.get("direction") and t["direction"] != f["direction"]:
        return False
    if f.get("outcome") and t["outcome"] != f["outcome"]:
        return False
    if f.get("result") == "win" and t["pnl_usd"] <= 0:
        return False
    if f.get("result") == "loss" and t["pnl_usd"] > 0:
        return False
    if f.get("entry_type") and t["entry_type"] != f["entry_type"]:
        return False
    if f.get("fa") and t["fa_bucket"] != f["fa"]:
        return False
    if f.get("date_from") and (t["closed_ts"] or 0) < f["date_from"]:
        return False
    if f.get("date_to") and (t["closed_ts"] or 0) > f["date_to"]:
        return False
    if f.get("q"):
        hay = " ".join(str(t.get(k) or "") for k in
                       ("symbol", "oracle_reasoning", "claude_thesis", "close_reason", "id")).lower()
        if f["q"].lower() not in hay:
            return False
    return True


def backtest(trades: list[dict], capital: float) -> dict:
    """Replay trades in close order: equity curve, drawdown and the usual statistics."""
    ordered = sorted(trades, key=lambda t: t["closed_ts"] or 0)
    curve = [{"ts": ordered[0]["opened_ts"], "pnl": 0.0, "r": 0.0, "drawdown": 0.0, "drawdown_r": 0.0,
              "trade": None}] if ordered else []

    pnl = r_sum = peak = peak_r = 0.0
    max_dd = max_dd_r = 0.0
    streak = best_win_streak = best_loss_streak = 0
    for t in ordered:
        pnl += t["pnl_usd"]
        r_sum += t["r_multiple"] or 0.0
        peak, peak_r = max(peak, pnl), max(peak_r, r_sum)
        dd, dd_r = pnl - peak, r_sum - peak_r
        max_dd, max_dd_r = min(max_dd, dd), min(max_dd_r, dd_r)

        won = t["pnl_usd"] > 0
        streak = (streak + 1 if streak >= 0 else 1) if won else (streak - 1 if streak <= 0 else -1)
        best_win_streak, best_loss_streak = max(best_win_streak, streak), min(best_loss_streak, streak)

        curve.append({"ts": t["closed_ts"], "pnl": round(pnl, 2), "r": round(r_sum, 2),
                      "drawdown": round(dd, 2), "drawdown_r": round(dd_r, 2),
                      "trade": f"{t['symbol'].replace('USDT', '')} {t['direction']} · {t['outcome']}",
                      "trade_pnl": t["pnl_usd"], "trade_r": t["r_multiple"]})

    wins = [t for t in ordered if t["pnl_usd"] > 0]
    losses = [t for t in ordered if t["pnl_usd"] <= 0]
    gross_win = sum(t["pnl_usd"] for t in wins)
    gross_loss = -sum(t["pnl_usd"] for t in losses)
    rs = [t["r_multiple"] for t in ordered if t["r_multiple"] is not None]
    durations = [t["duration_minutes"] for t in ordered if t["duration_minutes"] is not None]

    by_symbol: dict[str, dict] = {}
    for t in ordered:
        b = by_symbol.setdefault(t["symbol"], {"symbol": t["symbol"], "trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
        b["trades"] += 1
        b["wins"] += t["pnl_usd"] > 0
        b["pnl"] = round(b["pnl"] + t["pnl_usd"], 2)
        b["r"] = round(b["r"] + (t["r_multiple"] or 0), 2)

    outcomes: dict[str, int] = {}
    for t in ordered:
        outcomes[t["outcome"]] = outcomes.get(t["outcome"], 0) + 1

    # Does FA at entry predict anything? Same trades, split by whether FA agreed.
    by_fa = []
    for bucket in ("with", "neutral", "against", "unknown"):
        grp = [t for t in ordered if t["fa_bucket"] == bucket]
        if not grp:
            continue
        grp_r = [t["r_multiple"] for t in grp if t["r_multiple"] is not None]
        by_fa.append({
            "bucket": bucket, "trades": len(grp),
            "win_rate": round(sum(1 for t in grp if t["pnl_usd"] > 0) / len(grp) * 100, 1),
            "avg_r": round(sum(grp_r) / len(grp_r), 2) if grp_r else None,
            "pnl": round(sum(t["pnl_usd"] for t in grp), 2),
        })

    n = len(ordered)
    return {
        "curve": curve,
        "stats": {
            "trades": n,
            "capital": capital,
            "net_pnl": round(pnl, 2),
            "return_pct": round(pnl / capital * 100, 2) if capital else 0.0,
            "net_r": round(r_sum, 2),
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "expectancy_usd": round(pnl / n, 2) if n else 0.0,
            "expectancy_r": round(sum(rs) / len(rs), 2) if rs else 0.0,
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "best": max((t["pnl_usd"] for t in ordered), default=0.0),
            "worst": min((t["pnl_usd"] for t in ordered), default=0.0),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd / capital * 100, 2) if capital else 0.0,
            "max_drawdown_r": round(max_dd_r, 2),
            "avg_hold_minutes": round(sum(durations) / len(durations), 1) if durations else None,
            "longest_win_streak": best_win_streak,
            "longest_loss_streak": -best_loss_streak,
        },
        "by_symbol": sorted(by_symbol.values(), key=lambda b: b["pnl"], reverse=True),
        "outcomes": outcomes,
        "by_fa": by_fa,
    }


CSV_FIELDS = [
    "wallet", "id", "symbol", "direction", "entry_type", "outcome", "close_reason",
    "opened_at", "closed_at", "duration_minutes", "entry_price", "exit_price", "stop_loss",
    "take_profit_1", "take_profit_2", "size_pct", "position_value_usd", "pnl_usd", "pnl_pct",
    "r_multiple", "risk_usd", "leverage", "notional_usd", "margin_usd", "liquidation_price",
    "roe_pct", "fees_usd", "funding_usd", "oracle_confidence", "claude_confidence", "claude_entry",
    "confirmed_by", "fa_score", "fa_bucket", "timeframe", "oracle_reasoning",
]


def to_csv(rows: list[dict]) -> str:
    import csv
    import io

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def query(
    wallet: str = "all",
    filters: dict | None = None,
    sort: str = "closed_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    filters = filters or {}
    trades, capital = load(wallet)

    # Facets come from the wallet selection only, so a filter never hides its own options.
    facets = {
        "symbols": sorted({t["symbol"] for t in trades}),
        "outcomes": sorted({t["outcome"] for t in trades}),
        "entry_types": sorted({t["entry_type"] for t in trades}),
    }

    selected = [t for t in trades if _matches(t, filters)]
    key = sort if sort in SORTABLE else "closed_at"
    ts_key = {"closed_at": "closed_ts", "opened_at": "opened_ts"}.get(key, key)
    present = [t for t in selected if t.get(ts_key) is not None]
    missing = [t for t in selected if t.get(ts_key) is None]
    present.sort(key=lambda t: t[ts_key], reverse=order == "desc")
    selected = present + missing   # rows without a value go last either way

    total = len(selected)
    page_size = max(1, min(page_size, 200))
    pages = max(1, -(-total // page_size))
    page = max(1, min(page, pages))
    start = (page - 1) * page_size

    return {
        "rows": selected[start:start + page_size],
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "facets": facets,
        "backtest": backtest(selected, capital),
    }


def exit_comparison(wallet: str = "all", ledger: str = "both") -> dict:
    """Every exit policy's results on the same trades (the shadow book), merged across wallets."""
    from trading import exits

    ledgers = _ledgers(wallet, ledger)
    results, open_shadows = [], 0
    for _, w in ledgers:
        report = w.shadow_report()
        results += report["results"]
        open_shadows += report["open_shadows"]
    taken = [r for r in results if r.get("claude_verdict") not in exits.NOT_TAKEN]

    # Interventions: each real trade vs its own untouched shadow under the same exit policy.
    # The shadow ignores tightened stops, flip exits, position-manager adds/trims and manual
    # closes — so real R − shadow R is exactly what those interventions added or cost.
    shadow_r = {(r["trade_id"], r["policy"]): r["r"] for r in results}
    by_reason: dict[str, list[float]] = {}
    for _, w in ledgers:
        for t in w.all_trades()["closed"]:
            key = (t["id"], t.get("exit_policy"))
            if key not in shadow_r or not t.get("risk_usd"):
                continue
            delta = t["pnl_usd"] / t["risk_usd"] - shadow_r[key]
            by_reason.setdefault(t.get("close_reason") or "?", []).append(delta)
    interventions = [
        {"exit": k, "trades": len(v), "avg_delta_r": round(sum(v) / len(v), 3), "total_delta_r": round(sum(v), 2)}
        for k, v in sorted(by_reason.items(), key=lambda kv: sum(kv[1]))
    ]
    return {
        "policies": exits.summarize(taken),
        "calibration": exits.claude_calibration(results),
        "interventions": interventions,
        "open_shadows": open_shadows,
        "trades_compared": len({r["trade_id"] for r in taken}),
        "descriptions": {
            "protect": "Live default since 2026-10-07: stop to breakeven+costs at +1R, then trail 0.5R behind the best price; out after 24h",
            "trail": "Default until 2026-10-07: stop to breakeven+costs at 1.5R, then trail 1R behind the best price; no fixed target",
            "trail_stale": "As trail, plus cut trades that haven't reached +0.5R by the stale deadline (3h–36h by timeframe)",
            "partial_trail": "Bank half at 1.5R, stop to breakeven+costs, trail the rest 1R behind",
            "fixed": "Stop to breakeven+costs at 1.5R, full exit at 2.5R",
            "legacy": "Previous rules: stop to exact entry at 1.5R, exit at 2.5R, hard expiry 4h–72h",
            "hold": "Baseline: original stop or 2.5R only, until max hold",
        },
    }


# ── Insights: which decisions pay ──────────────────────────────────────────────

CONVICTION_BUCKETS = [(0.45, "under 0.45"), (0.6, "0.45 – 0.6"), (float("inf"), "0.6 and up")]
ARMED_BUCKETS = [(1, "entered at once"), (15, "under 15m"), (60, "15m – 1h"), (240, "1 – 4h"), (float("inf"), "over 4h")]


def _group(trades: list[dict], key) -> list[dict]:
    """key(trade) → a label, a list of labels (counted in each), or None (skipped)."""
    groups: dict = {}
    for t in trades:
        keys = key(t)
        for k in (keys if isinstance(keys, list) else [keys]):
            if k is not None:
                groups.setdefault(k, []).append(t)
    out = []
    for k, g in groups.items():
        rs = [t["r_multiple"] for t in g if t["r_multiple"] is not None]
        out.append({"key": k, "trades": len(g), "win_rate": round(sum(1 for t in g if t["pnl_usd"] > 0) / len(g) * 100, 1),
                    "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "pnl": round(sum(t["pnl_usd"] for t in g), 2)})
    return sorted(out, key=lambda g: -(g["avg_r"] if g["avg_r"] is not None else -99))


def _bucket(value, buckets):
    if value is None:
        return None
    return next(label for limit, label in buckets if value < limit)


def _story(t: dict) -> dict:
    keys = ("id", "wallet", "symbol", "direction", "entry_price", "exit_price", "pnl_usd", "r_multiple", "close_reason",
            "opened_ts", "closed_ts", "duration_minutes", "claude_thesis", "oracle_reasoning", "exit_note",
            "setups_at_entry", "confirmed_by", "claude_confidence", "entry_type", "outcome")
    return {k: t.get(k) for k in keys}


def insights(wallet: str = "all", ledger: str = "both") -> dict:
    """What the closed swing trades say about the DECISIONS behind them — the entry inputs
    (setups, conviction, confirmation, FA, entry type, time armed) and the best and worst trades."""
    trades, _ = load(wallet, ledger)
    ranked = sorted((t for t in trades if t["r_multiple"] is not None), key=lambda t: t["r_multiple"])
    source = lambda t: {"claude": "Claude confirmed", "scanner_signal": "fresh scanner signal"}.get(
        t.get("confirmed_by"), "Trade tab / manual" if t.get("strategy") == "manual" else "signal (you)")
    return {
        "trades": len(trades),
        "by_setup": _group(trades, lambda t: t.get("setups_at_entry") or None),
        "by_conviction": _group(trades, lambda t: _bucket(abs(t["conviction_at_entry"]) if t.get("conviction_at_entry") is not None else None,
                                                          CONVICTION_BUCKETS)),
        "by_source": _group(trades, source),
        "by_fa": _group(trades, lambda t: t["fa_bucket"] if t["fa_bucket"] != "unknown" else None),
        "by_entry_type": _group(trades, lambda t: t["entry_type"]),
        "by_armed": _group(trades, lambda t: _bucket(t.get("armed_minutes"), ARMED_BUCKETS)),
        "by_timeframe": _group(trades, lambda t: t.get("timeframe")),
        # Stamped on trades opened from 2026-09-29 on; older trades have no regime recorded.
        "by_regime": _group(trades, lambda t: (t.get("regime_at_entry") or {}).get("label")),
        "best": [_story(t) for t in reversed(ranked[-3:])],
        "worst": [_story(t) for t in ranked[:3]],
    }
