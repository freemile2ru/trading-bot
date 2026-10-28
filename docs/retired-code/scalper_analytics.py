"""Scalper metrics — computed from the scalper's own databases."""

import json
import math
import time

from scalper.store import Store


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[len(xs) // 2], 3) if xs else None


def _group(rows: list[dict], key) -> list[dict]:
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = []
    for k, g in groups.items():
        out.append({
            "key": k, "trades": len(g),
            "win_rate": round(sum(1 for t in g if t["net_pnl"] > 0) / len(g) * 100, 1),
            "net_pnl": round(sum(t["net_pnl"] for t in g), 2),
            "avg_net_bps": _avg([t["net_bps"] for t in g]),
            "avg_r": _avg([t["r"] for t in g]),
            "avg_hold_s": _avg([t["hold_s"] for t in g]),
        })
    return sorted(out, key=lambda x: -x["trades"])


MODE_SQL = "COALESCE(mode, 'paper') = ?"          # rows written before modes existed are paper


def summary(store: Store, since: float | None = None, mode: str = "paper") -> dict:
    """Everything for one mode only — paper and live results are never mixed."""
    since = since or 0
    trades = store.query("trades", f"SELECT * FROM trades WHERE exit_ts >= ? AND {MODE_SQL} ORDER BY exit_ts",
                         (since, mode))
    orders = store.query("trades", f"SELECT * FROM orders WHERE placed_ts >= ? AND {MODE_SQL}", (since, mode))
    equity = store.query("trades", f"SELECT ts, equity, balance FROM equity WHERE ts >= ? AND {MODE_SQL} ORDER BY ts",
                         (since, mode))

    n = len(trades)
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    gross_before_costs = sum(t["gross_pnl"] for t in trades)
    fees = sum(t["fees"] for t in trades)
    funding = sum(t["funding"] or 0 for t in trades)
    positive_gross = sum(t["gross_pnl"] for t in trades if t["gross_pnl"] > 0)

    peak, max_dd = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e["equity"])
        max_dd = min(max_dd, e["equity"] - peak)

    entries = [o for o in orders if o["purpose"] == "entry"]
    filled = [o for o in entries if o["status"] == "filled"]
    resolved = [o for o in entries if o["status"] in ("filled", "cancelled")]
    cancel_reasons: dict[str, int] = {}
    for o in entries:
        if o["status"] == "cancelled":
            cancel_reasons[o["cancel_reason"]] = cancel_reasons.get(o["cancel_reason"], 0) + 1
    tps = [o for o in orders if o["purpose"] == "tp"]

    # Losers that had been at least half-way to target first — exits leaving money behind.
    gave_back = [t for t in losses if t["mfe_bps"] is not None and t["tp_bps"] and t["mfe_bps"] >= 0.5 * t["tp_bps"]]

    return {
        "generated_at": time.time(),
        "mode": mode,
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        # Before costs: how often the market moved our way at all. The gap to win_rate is
        # what fees and slippage take — trades that were right but still lost money.
        "gross_win_rate": round(sum(1 for t in trades if t["gross_pnl"] > 0) / n * 100, 1) if n else None,
        "won_before_costs": sum(1 for t in trades if t["gross_pnl"] > 0 and t["net_pnl"] <= 0),
        "net_pnl": round(sum(t["net_pnl"] for t in trades), 2),
        "gross_pnl": round(gross_before_costs, 2),
        "fees": round(fees, 2),
        "funding": round(funding, 2),
        "fees_share_of_gross_profit_pct": round(fees / positive_gross * 100, 1) if positive_gross else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy_usd": round(sum(t["net_pnl"] for t in trades) / n, 3) if n else None,
        "avg_r": _avg([t["r"] for t in trades]),
        "avg_net_bps": _avg([t["net_bps"] for t in trades]),
        "avg_win_bps": _avg([t["net_bps"] for t in wins]),
        "avg_loss_bps": _avg([t["net_bps"] for t in losses]),
        "avg_hold_s": _avg([t["hold_s"] for t in trades]),
        "max_drawdown_usd": round(max_dd, 2),
        "mfe_bps_avg": _avg([t["mfe_bps"] for t in trades]),
        "mae_bps_avg": _avg([t["mae_bps"] for t in trades]),
        "losers_that_were_halfway_to_target": len(gave_back),
        "execution": {
            "entries_placed": len(entries),
            "entries_filled": len(filled),
            "fill_rate_pct": round(len(filled) / len(resolved) * 100, 1) if resolved else None,
            "time_to_fill_s_median": _median([o["time_to_fill"] for o in filled]),
            "time_to_fill_s_avg": _avg([o["time_to_fill"] for o in filled]),
            "queue_ahead_avg": _avg([o["queue_ahead_at_place"] for o in filled]),
            "cancel_reasons": cancel_reasons,
            # Signed mid move after our fill, bps: negative = we were picked off (adverse selection).
            "adverse_1s_bps": _avg([o["adv_1"] for o in filled]),
            "adverse_5s_bps": _avg([o["adv_5"] for o in filled]),
            "adverse_30s_bps": _avg([o["adv_30"] for o in filled]),
            "tp_fill_rate_pct": round(sum(1 for o in tps if o["status"] == "filled") / len(tps) * 100, 1) if tps else None,
            "maker_exit_share_pct": round(sum(t["exit_maker"] for t in trades) / n * 100, 1) if n else None,
            "taker_exit_slippage_bps_avg": _avg([
                abs(t["exit_price"] - t["exit_trigger"]) / t["exit_trigger"] * 1e4
                for t in trades if not t["exit_maker"] and t["exit_trigger"]]),
            "spread_entry_bps_avg": _avg([t["spread_entry_bps"] for t in trades]),
        },
        "costs": {
            "avg_cost_bps": _avg([t.get("cost_bps") for t in trades]),
            "avg_entry_fee_bps": _avg([t["entry_fee"] / t["notional"] * 1e4 for t in trades if t.get("entry_fee") is not None and t["notional"]]),
            "avg_exit_fee_bps": _avg([t["exit_fee"] / t["notional"] * 1e4 for t in trades if t.get("exit_fee") is not None and t["notional"]]),
            "avg_slippage_bps": _avg([t["slippage_usd"] / t["notional"] * 1e4 for t in trades if t.get("slippage_usd") is not None and t["notional"]]),
            "passive_exits_attempted": sum(1 for o in orders if o["purpose"] == "exit"),
            "passive_exit_fill_rate_pct": (
                round(sum(1 for o in orders if o["purpose"] == "exit" and o["status"] == "filled")
                      / max(1, sum(1 for o in orders if o["purpose"] == "exit" and o["status"] in ("filled", "cancelled"))) * 100, 1)
                if any(o["purpose"] == "exit" for o in orders) else None),
            "exits_by_type": {
                "maker": sum(1 for t in trades if t["exit_maker"]),
                "taker": sum(1 for t in trades if not t["exit_maker"]),
            },
        },
        "by_exit": _group(trades, lambda t: t["exit_reason"]),
        "by_symbol": _group(trades, lambda t: t["symbol"]),
        "by_side": _group(trades, lambda t: t["side"]),
        "by_hour_utc": sorted(_group(trades, lambda t: time.gmtime(t["entry_ts"]).tm_hour), key=lambda x: x["key"]),
        "equity_curve": _downsample([{"ts": e["ts"], "equity": e["equity"]} for e in equity], 400),
    }


def _downsample(points: list[dict], n: int) -> list[dict]:
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n - 1)] + [points[-1]]


def _bucket_stats(rows: list[dict], label: str) -> dict:
    out = {"bucket": label, "signals": len(rows)}
    for h in (5, 15, 30, 60, 120):
        vals = [r[f"fwd_{h}"] for r in rows if r[f"fwd_{h}"] is not None]
        out[f"fwd_{h}_bps"] = _avg(vals)
        out[f"hit_{h}_pct"] = round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None
    return out


def signal_research(store: Store, since: float | None = None) -> dict:
    """Do the features predict short-horizon moves — regardless of whether we got filled?

    Forward returns are signed in the signal's direction (+ = the signal was right).
    """
    rows = store.query("trades", "SELECT * FROM signals WHERE ts >= ?", (since or 0,))
    z_buckets = [(1.5, 2.0, "|z| 1.5–2"), (2.0, 3.0, "|z| 2–3"), (3.0, 99, "|z| 3+")]
    obi_buckets = [(-9, 0.0, "book against"), (0.0, 0.2, "book neutral"), (0.2, 9, "book supportive")]

    def signed_obi(r):
        return (r["obi"] or 0) * (1 if r["side"] == "long" else -1)

    return {
        "signals": len(rows),
        "by_decision": [_bucket_stats([r for r in rows if r["decision"] == d], d)
                        for d in ("entered", "blocked", "skipped")],
        "by_flow_z": [_bucket_stats([r for r in rows if lo <= abs(r["flow_z"] or 0) < hi], lbl)
                      for lo, hi, lbl in z_buckets],
        "by_book": [_bucket_stats([r for r in rows if lo <= signed_obi(r) < hi], lbl)
                    for lo, hi, lbl in obi_buckets],
        "by_symbol": [_bucket_stats([r for r in rows if r["symbol"] == s], s)
                      for s in sorted({r["symbol"] for r in rows})],
        "skip_reasons": _count([r["reason"].split(" — ")[0] if r["reason"] else "?" for r in rows
                                if r["decision"] != "entered"]),
    }


def _reason_key(reason: str) -> str:
    """Group reasons by kind, not by the live number in them:
    'flow z +1.57 below 2.0' and 'flow z -1.51 below 2.0' are the same reason."""
    import re
    r = re.sub(r"\s[+\-−]\d+(\.\d+)?", "", reason)            # signed readings
    r = re.sub(r"spread [\d.]+bps wide vs median [\d.]+", "spread too wide", r)
    r = re.sub(r"[+\-−]?[\d.]+ ATR from 15m VWAP", "too far from 15m VWAP", r)
    return r.strip()


def _count(xs: list[str]) -> dict:
    out: dict[str, int] = {}
    for x in xs:
        x = _reason_key(x)
        key = x if len(x) < 60 else x[:57] + "…"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:12])


def trades_page(store: Store, page: int, page_size: int, symbol: str | None = None, mode: str = "paper") -> dict:
    where, params = f"WHERE {MODE_SQL}", [mode]
    if symbol:
        where, params = where + " AND symbol = ?", params + [symbol]
    total = store.query("trades", f"SELECT COUNT(*) AS n FROM trades {where}", params)[0]["n"]
    rows = store.query("trades", f"SELECT * FROM trades {where} ORDER BY exit_ts DESC LIMIT ? OFFSET ?",
                       params + [page_size, (page - 1) * page_size])
    for r in rows:
        r["features"] = json.loads(r["features"] or "{}")
    return {"rows": rows, "total": total, "page": page, "pages": max(1, -(-total // page_size))}
