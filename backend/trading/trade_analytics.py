"""Charts and research for the Trades page — computed from the same rows and filters as its table.

Everything here answers "is this working, where, and can I trust it?":
  performance  cumulative and daily P&L, per system
  breakdowns   by coin, exit, side, hold time, hour and weekday (UTC) — where the money comes from
  shape        the R-multiple distribution; costs vs gross; how much of the best move exits kept
  stability    rolling expectancy and win rate — is the edge fading?
  confidence   a bootstrap interval on expectancy, t-stat, SQN, trades still needed to prove it
  risk         Monte Carlo of the same trades resampled: drawdown and outcome percentiles
Only closed trades count (an open trade's result isn't known yet).
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timezone

from trading import overview

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HOLD_BUCKETS = [(5, "< 5m"), (30, "5–30m"), (120, "30m–2h"), (480, "2–8h"), (1440, "8–24h"), (math.inf, "> 1d")]
R_EDGES = [x / 2 for x in range(-6, 11)]            # −3R … +5R in half-R bins, plus an overflow bin each side
SIMULATIONS = 1000
# Fee sensitivity: the same trades re-costed at other rates (per side, entry and exit each).
FEE_SCENARIOS = [
    ("As traded", None),
    ("BNB fee discount (−10%)", "discount"),
    ("Maker on both sides (0.02%)", 0.0002),
    ("VIP 1 maker (0.016%)", 0.00016),
    ("Zero fees", 0.0),
]


def _r(x, n=2):
    return None if x is None else round(x, n)


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def _group(rows: list[dict], key) -> list[dict]:
    groups: dict = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for k, g in groups.items():
        rs = [x["r"] for x in g if x["r"] is not None]
        wins = sum(1 for x in g if (x["net_pnl"] or 0) > 0)
        out.append({"key": k, "trades": len(g), "wins": wins, "win_rate": _r(wins / len(g) * 100, 1),
                    "net_pnl": _r(sum(x["net_pnl"] or 0 for x in g)),
                    "gross_pnl": _r(sum(x["gross_pnl"] or 0 for x in g)),
                    "avg_r": _r(sum(rs) / len(rs), 3) if rs else None})
    return out


def _thin(points: list, n: int = 400) -> list:
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n - 1)] + [points[-1]]


def _max_drawdown(pnls: list[float]) -> float:
    peak = cum = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _pct(sorted_xs: list[float], q: float) -> float:
    return sorted_xs[min(len(sorted_xs) - 1, int(q * len(sorted_xs)))]


def _mean_sd(xs: list[float]) -> tuple[float, float]:
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) if len(xs) > 1 else 0.0
    return m, sd


def _confidence(closed: list[dict], rng: random.Random) -> dict:
    """Is the edge real? Expectancy with a bootstrap 95% interval, the t-stat against zero,
    Van Tharp's SQN, and how many trades it would take to tell at this effect size."""
    pnl = [r["net_pnl"] or 0.0 for r in closed]
    rs = [r["r"] for r in closed if r["r"] is not None]
    out: dict = {"trades": len(pnl)}
    if len(pnl) < 5:
        return {**out, "enough": False}
    m, sd = _mean_sd(pnl)
    boot = sorted(sum(rng.choices(pnl, k=len(pnl))) / len(pnl) for _ in range(SIMULATIONS))
    t = m / (sd / math.sqrt(len(pnl))) if sd else None
    out.update(enough=True, expectancy_usd=_r(m), expectancy_ci=[_r(_pct(boot, 0.025)), _r(_pct(boot, 0.975))],
               t_stat=_r(t), p_value=_r(math.erfc(abs(t) / math.sqrt(2)), 4) if t is not None else None,
               positive_share=_r(sum(1 for b in boot if b > 0) / len(boot) * 100, 1))
    if len(rs) >= 5:
        mr, sdr = _mean_sd(rs)
        out.update(expectancy_r=_r(mr, 3), sd_r=_r(sdr, 3),
                   sqn=_r(math.sqrt(min(len(rs), 100)) * mr / sdr) if sdr else None,
                   # Trades for a 95% interval that excludes zero at today's mean and spread.
                   trades_needed=math.ceil((1.96 * sdr / mr) ** 2) if mr > 0 and sdr else None)
    wins = [p for p in pnl if p > 0]
    losses = [-p for p in pnl if p <= 0]
    if wins and losses:
        w, payoff = len(wins) / len(pnl), (sum(wins) / len(wins)) / (sum(losses) / len(losses))
        out["kelly_pct"] = _r((w - (1 - w) / payoff) * 100, 1)
    return out


def _monte_carlo(closed: list[dict], initial: float, rng: random.Random) -> dict | None:
    """Resample the same trades (with replacement) into many alternative histories of the same
    length: how bad could the drawdown have been, and how often does it end in a loss?"""
    pnl = [r["net_pnl"] or 0.0 for r in closed][-3000:]
    if len(pnl) < 10:
        return None
    finals, dds = [], []
    for _ in range(SIMULATIONS):
        path = rng.choices(pnl, k=len(pnl))
        finals.append(sum(path))
        dds.append(_max_drawdown(path))
    finals.sort()
    dds.sort()
    thresholds = [0.05, 0.10, 0.20]
    return {
        "simulations": SIMULATIONS, "trades": len(pnl), "initial_balance": _r(initial),
        "actual_final": _r(sum(pnl)), "actual_max_drawdown": _r(_max_drawdown(pnl)),
        "final": {q: _r(_pct(finals, p)) for q, p in (("p5", .05), ("p25", .25), ("p50", .5), ("p75", .75), ("p95", .95))},
        "max_drawdown": {q: _r(_pct(dds, p)) for q, p in (("p50", .5), ("p90", .9), ("p99", .99))},
        "loss_probability_pct": _r(sum(1 for f in finals if f < 0) / len(finals) * 100, 1),
        "drawdown_odds": [{"pct": int(t * 100), "odds_pct": _r(sum(1 for d in dds if d >= t * initial) / len(dds) * 100, 1)}
                          for t in thresholds] if initial else [],
    }


def _fee_scenarios(closed: list[dict]) -> list[dict]:
    """What the same trades would have netted at other fee rates — fees dominate short-hold
    results, so this shows how much of the loss is the fee schedule rather than the calls."""
    gross = sum(r["gross_pnl"] or 0 for r in closed)
    funding = sum(r["funding"] or 0 for r in closed)
    fees = sum(r["fees"] or 0 for r in closed)
    turnover = sum(2 * (r["notional"] or 0) for r in closed)
    out = []
    for label, rate in FEE_SCENARIOS:
        f = fees if rate is None else fees * 0.9 if rate == "discount" else turnover * rate
        net_each = [(r["gross_pnl"] or 0) - (r["funding"] or 0) - (
            (r["fees"] or 0) if rate is None else (r["fees"] or 0) * 0.9 if rate == "discount" else 2 * (r["notional"] or 0) * rate)
            for r in closed]
        out.append({"label": label, "fees": _r(f), "net_pnl": _r(gross - funding - f),
                     "win_rate": _r(sum(1 for x in net_each if x > 0) / len(net_each) * 100, 1) if net_each else None})
    return out


def analytics(filters: dict) -> dict:
    rows, initial = overview.all_rows()
    f = {**filters, "status": "closed"}
    closed = sorted((r for r in rows if overview.matches(r, f) and r["closed_ts"]), key=lambda r: r["closed_ts"])
    systems = sorted({r["system"] for r in closed})
    base = sum(initial.get(s, 0) for s in systems)
    rng = random.Random(7)                   # the same trades always give the same simulation

    # ── Performance over time
    cum, series = defaultdict(float), defaultdict(list)
    total, total_series = 0.0, []
    for r in closed:
        cum[r["system"]] += r["net_pnl"] or 0
        total += r["net_pnl"] or 0
        series[r["system"]].append({"ts": r["closed_ts"], "pnl": _r(cum[r["system"]])})
        total_series.append({"ts": r["closed_ts"], "pnl": _r(total)})
    days: dict = defaultdict(lambda: {"net_pnl": 0.0, "trades": 0, "wins": 0})
    for r in closed:
        d = days[_utc(r["closed_ts"]).strftime("%Y-%m-%d")]
        d["net_pnl"] += r["net_pnl"] or 0
        d["trades"] += 1
        d["wins"] += (r["net_pnl"] or 0) > 0
    daily = [{"day": k, "net_pnl": _r(v["net_pnl"]), "trades": v["trades"], "wins": v["wins"]} for k, v in sorted(days.items())]

    # ── Shape
    rs = [r["r"] for r in closed if r["r"] is not None]
    bins = [{"label": f"< {R_EDGES[0]:g}R", "lo": None, "hi": R_EDGES[0], "count": 0}]
    bins += [{"label": f"{lo:+g}R", "lo": lo, "hi": hi, "count": 0} for lo, hi in zip(R_EDGES, R_EDGES[1:])]
    bins += [{"label": f"≥ {R_EDGES[-1]:g}R", "lo": R_EDGES[-1], "hi": None, "count": 0}]
    for x in rs:
        i = 0 if x < R_EDGES[0] else len(bins) - 1 if x >= R_EDGES[-1] else 1 + int((x - R_EDGES[0]) // 0.5)
        bins[i]["count"] += 1
    gross = sum(r["gross_pnl"] or 0 for r in closed)
    fees = sum(r["fees"] or 0 for r in closed)
    funding = sum(r["funding"] or 0 for r in closed)
    costs = {"gross_pnl": _r(gross), "fees": _r(fees), "funding": _r(funding), "net_pnl": _r(gross - fees - funding),
             "won_before_costs": sum(1 for r in closed if (r["gross_pnl"] or 0) > 0 >= (r["net_pnl"] or 0)),
             "gross_wins": _r(sum(r["gross_pnl"] for r in closed if (r["gross_pnl"] or 0) > 0))}

    eff_rows = [r for r in closed if r.get("mfe_r") is not None and r["r"] is not None]
    reached = [r for r in eff_rows if r["mfe_r"] >= 1]
    efficiency = {
        "points": _thin([{"mfe_r": _r(r["mfe_r"], 2), "r": _r(r["r"], 2), "system": r["system"], "symbol": r["symbol"]}
                         for r in eff_rows], 600),
        "trades": len(eff_rows),
        "avg_best_r": _r(sum(r["mfe_r"] for r in eff_rows) / len(eff_rows), 2) if eff_rows else None,
        "avg_kept_r": _r(sum(r["r"] for r in eff_rows) / len(eff_rows), 2) if eff_rows else None,
        # Trades that were +1R or better at some point but closed at a loss.
        "reached_1r": len(reached), "reached_1r_then_lost": sum(1 for r in reached if r["r"] <= 0),
    }

    # ── Stability: rolling window over trade order
    window = max(5, min(30, len(closed) // 5)) if len(closed) >= 10 else 0
    rolling = []
    if window:
        for i in range(window, len(closed) + 1):
            chunk = closed[i - window:i]
            crs = [r["r"] for r in chunk if r["r"] is not None]
            rolling.append({"n": i, "ts": chunk[-1]["closed_ts"],
                            "avg_r": _r(sum(crs) / len(crs), 3) if crs else None,
                            "win_rate": _r(sum(1 for r in chunk if (r["net_pnl"] or 0) > 0) / window * 100, 1),
                            "expectancy": _r(sum(r["net_pnl"] or 0 for r in chunk) / window)})

    # ── Where: breakdowns
    def hold_bucket(r):
        m = r["hold_min"] or 0
        return next(label for limit, label in HOLD_BUCKETS if m < limit)

    by_hold = {g["key"]: g for g in _group(closed, hold_bucket)}
    heat: dict = defaultdict(lambda: {"net_pnl": 0.0, "trades": 0})
    for r in closed:
        t = _utc(r["opened_ts"] or r["closed_ts"])
        cell = heat[(t.weekday(), t.hour)]
        cell["net_pnl"] += r["net_pnl"] or 0
        cell["trades"] += 1

    streak = best_w = best_l = 0
    for r in closed:
        win = (r["net_pnl"] or 0) > 0
        streak = (streak + 1 if streak >= 0 else 1) if win else (streak - 1 if streak <= 0 else -1)
        best_w, best_l = max(best_w, streak), min(best_l, streak)

    return {
        "trades": len(closed), "systems": systems, "initial_balance": _r(base),
        "cumulative": {"total": _thin(total_series), "by_system": {s: _thin(v) for s, v in series.items()}},
        "daily": daily[-180:],
        "r_distribution": [{k: v for k, v in b.items()} for b in bins],
        "costs": costs,
        "efficiency": efficiency,
        "rolling": {"window": window, "points": _thin(rolling)},
        "by_symbol": sorted(_group(closed, lambda r: r["symbol"]), key=lambda g: -g["net_pnl"]),
        "by_exit": sorted(_group(closed, lambda r: r["exit_reason"] or "unknown"), key=lambda g: -g["trades"]),
        "by_side": _group(closed, lambda r: r["direction"]),
        "by_system": _group(closed, lambda r: r["system"]),
        "by_hold": [by_hold[label] for _, label in HOLD_BUCKETS if label in by_hold],
        "by_hour": sorted(_group(closed, lambda r: _utc(r["opened_ts"] or r["closed_ts"]).hour), key=lambda g: g["key"]),
        "by_weekday": [{**g, "key": WEEKDAYS[g["key"]]} for g in sorted(
            _group(closed, lambda r: _utc(r["opened_ts"] or r["closed_ts"]).weekday()), key=lambda g: g["key"])],
        "heatmap": [{"weekday": d, "hour": h, "net_pnl": _r(v["net_pnl"]), "trades": v["trades"]}
                    for (d, h), v in sorted(heat.items())],
        "streaks": {"current": streak, "longest_win": best_w, "longest_loss": -best_l},
        "fee_scenarios": _fee_scenarios(closed),
        "confidence": _confidence(closed, rng),
        "monte_carlo": _monte_carlo(closed, base, rng),
    }
