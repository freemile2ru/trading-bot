"""The Lab tab: each side experiment, its paired comparison and its pre-registered verdict.

Verdict rules, fixed before any results came in:
  * standalone (Claude swing, Signals · Claude): after MIN_TRADES closed trades, the 95% bootstrap
    interval of the average net result per trade must sit above zero → "edge"; entirely below →
    "no edge"; straddling zero → "inconclusive".
  * paired (what does Claude add?): after MIN_TRADES closed trades in EACH book over the same
    period, the 95% interval of (with Claude − without) per-trade average must sit above zero →
    "Claude adds value"; below → "Claude costs money"; straddling → "no difference shown".
Before MIN_TRADES it's "collecting" — early numbers are shown but not judged.
"""
from __future__ import annotations

import random
import time

from experiments import books, claude_swing
from trading import capital, overview, trade_analytics

MIN_TRADES = 40
SIMULATIONS = 2000
_COPY_VARIANT_DESC = {
    "wide": "the same quality bar but 15 leaders a venue instead of 5 — more positions seen opening",
    "fast": "5 leaders a venue chosen among those whose median hold is a day or less — they open more often",
    "late": "the base leaders, with a position up to their whole median hold old still copied at half size when it has moved no more than a quarter of their typical win",
}

DESCRIPTIONS = {
    "auto_rules": "The autotrader's exact rules on the same scans, entering wherever the real autotrader asks Claude. "
                  "Compared with the real autotrader (paper and live) over the same period.",
    "signals_rules": "Every scanner candidate the rules send to Claude, taken whatever Claude says, with rule-based levels.",
    "signals_claude": "Only the signals Claude fires, at Claude's direction and levels.",
    "claude_swing": "Claude reviewing the market every few hours and picking its own 1–7 day trades, or none.",
    "cascade": "An hour after a liquidation cascade (a 5-minute burst at the coin's 99th percentile), take the other side for 4 hours "
               "with an 8% disaster stop. Rule since 2026-10-30: the first rule (fade at once, 3% stop) lost in both halves of the "
               "recorder's replay; the hour's delay made +52 bps a trade there, but on a flat first half — a fresh forward test, "
               "judged only on trades opened under this rule.",
    "weekly_rs": "Every Monday: long the 5 coins nearest their 7-day high, short the 5 furthest below, held a week — "
                 "the one input combination that survived costs on a year of data (research/signal_rebuild_lab.py).",
    "copy_stocks": "The copy leaders' positions in Binance's stock perpetuals only, in their own wallet: same too-late rule and exits as the "
                   "crypto copy book, plus the earnings blackout. Judged on its own 40 trades.",
    "copy_futures": "The copy leaders' positions in Binance's commodity perpetuals only (gold, silver, oil, gas, metals), in their own wallet, "
                    "same rules as the crypto copy book. Judged on its own 40 trades.",
    **{f"copy_{m}_{v}": f"Copy trading on {ml}, variant '{v}' (since 2026-10-30): {d}. Same exits and stop as the base book; judged on its own 40 trades."
       for m, ml in (("crypto", "crypto perps"), ("stocks", "stock perps"), ("futures", "commodity perps")) for v, d in _COPY_VARIANT_DESC.items()},
    "burst_up": "After a ≥5% five-minute UP-burst (≥4σ of the day's 5-minute moves, on 3× volume) on one of the 40 most-traded "
                "crypto perps: long 15 minutes later, held 4 hours, a 15% disaster stop only. research/spike_lab.py (45 days, "
                "219 such bursts): +165 bps a trade after costs but a −42 bps median and 48% winners — a few runners carry it. "
                "Chosen after looking at that data, so a forward test from 2026-11-06, judged on its own 40 trades.",
    "burst_dip": "After a ≥5% five-minute DOWN-burst (same filter): long 60 minutes later, held 4 hours, a 15% disaster stop only. "
                 "The lab's healthiest line (+112 bps a trade, median +44, 54% winners, both halves positive) and the cascade book's "
                 "rule on a wider, price-triggered universe. Chosen after looking at the data, so a forward test from 2026-11-06, "
                 "judged on its own 40 trades.",
    "auction": "The mentor's NQ auction-level system on QQQ, NVDA, TSLA and AMD perps, 5-minute bars, New York session: accept beyond a "
               "session level, confirm, enter, stop under the acceptance bar, target the next level ≥ 50 bps away, grade ≥ 7 only, no trade "
               "near a macro release or earnings. Replay (research/auction_lab.py): −0.13% a trade at our costs — a forward test the user asked "
               "for, judged on its own 40 trades.",
    "multi_factor": "Daily: 30-day cross-sectional momentum, 10-day trend and funding carry held together, each scaled by its own "
                    "volatility. On 4 years the combination's test Sharpe was 1.25 (26% drawdown) but its 95% range touched zero — "
                    "a $1k forward test, not a promoted book (research/multi_factor_lab.py).",
    "copy_trading": "Mirrors consistent lead traders from Binance's copy board and Hyperliquid's on-chain leaderboard (chosen daily "
                    "for drawdown, positive weeks, copier profit and tenure, not headline ROI). A trade is copied only if the leader's position is still young and hasn't "
                    "already run; skipped ones are tracked so the too-late rule can be judged. Exits when the leader exits.",
}
# Paired experiments: (the book WITH Claude, the book WITHOUT Claude).
# The "with vs without Claude" pairs are answered and their rules-only books retired (auto_rules 2026-10-17, signals
# 2026-10-21/25: no difference / both failing) — the Retired tab keeps the record; nothing is compared live any more.
COMPARISONS: list[dict] = []


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _stats(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["status"] == "closed"]
    pnl = [r["net_pnl"] or 0 for r in closed]
    rs = [r["r"] for r in closed if r["r"] is not None]
    return {"trades": len(closed), "open": sum(1 for r in rows if r["status"] == "open"),
            "net_pnl": round(sum(pnl), 2), "win_rate": round(sum(1 for p in pnl if p > 0) / len(pnl) * 100, 1) if pnl else None,
            "avg_pnl": round(_mean(pnl), 2) if pnl else None, "avg_r": round(_mean(rs), 3) if rs else None,
            "unrealized": round(sum(r["net_pnl"] or 0 for r in rows if r["status"] == "open"), 2)}


def _verdict_single(closed: list[dict], rng: random.Random) -> dict:
    c = trade_analytics._confidence(closed, rng)
    n = c["trades"]
    if n < MIN_TRADES or not c.get("enough"):
        return {"status": "collecting", "label": f"Collecting — {n}/{MIN_TRADES} trades", **c}
    lo, hi = c["expectancy_ci"]
    status, label = ("edge", "Edge shown") if lo > 0 else ("no_edge", "No edge") if hi < 0 else ("inconclusive", "Inconclusive")
    return {"status": status, "label": label, **c}


def _verdict_paired(a: list[dict], b: list[dict], rng: random.Random) -> dict:
    pa, pb = [r["net_pnl"] or 0 for r in a], [r["net_pnl"] or 0 for r in b]
    out = {"with_trades": len(pa), "without_trades": len(pb)}
    if len(pa) < 5 or len(pb) < 5:
        return {**out, "status": "collecting", "label": f"Collecting — {min(len(pa), len(pb))}/{MIN_TRADES} trades each"}
    diff = _mean(pa) - _mean(pb)
    boot = sorted(_mean(rng.choices(pa, k=len(pa))) - _mean(rng.choices(pb, k=len(pb))) for _ in range(SIMULATIONS))
    lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot))]
    out.update(diff_per_trade=round(diff, 2), diff_ci=[round(lo, 2), round(hi, 2)],
               claude_better_share=round(sum(1 for x in boot if x > 0) / len(boot) * 100, 1))
    if min(len(pa), len(pb)) < MIN_TRADES:
        return {**out, "status": "collecting", "label": f"Collecting — {min(len(pa), len(pb))}/{MIN_TRADES} trades each"}
    if lo > 0:
        return {**out, "status": "adds", "label": "Claude adds value"}
    if hi < 0:
        return {**out, "status": "costs", "label": "Claude costs money"}
    return {**out, "status": "no_difference", "label": "No difference shown"}


def _curve(closed: list[dict]) -> list[dict]:
    cum, out = 0.0, []
    for r in sorted(closed, key=lambda r: r["closed_ts"]):
        cum += r["net_pnl"] or 0
        out.append({"ts": r["closed_ts"], "pnl": round(cum, 2)})
    return trade_analytics._thin(out, 300)


def summary(ledger: str = "both") -> dict:
    """`ledger` picks the real autotrader's side of the paired test: its paper wallet, its live
    ledger, or both. The experiment books are paper-only, so it doesn't touch them."""
    from trading.history import in_ledger
    rows, _ = overview.all_rows()
    rng = random.Random(11)
    by_system: dict[str, list[dict]] = {}
    for r in rows:
        by_system.setdefault(r["system"], []).append(r)

    experiments = []
    for name, label in books.LABELS.items():
        started = books.started_at(name)
        mine = by_system.get(name, [])
        if name == "cascade":
            from experiments import cascade
            mine = [r for r in mine if (r.get("opened_ts") or 0) >= cascade.RULE_SINCE]      # the old rule's trades stay on record, not in the verdict
        closed = [r for r in mine if r["status"] == "closed"]
        experiments.append({
            "key": name, "label": label, "description": DESCRIPTIONS[name], "started_at": started,
            "running_days": round((time.time() - started) / 86400, 1), **_stats(mine),
            "pending_orders": len(books.WALLETS[name].get_orders(1)["pending"]),
            "verdict": _verdict_single(closed, rng), "curve": _curve(closed),
        })
        if name == "copy_trading":
            from experiments import copy_trading
            experiments[-1]["copy_trading"] = copy_trading.status()

    # Hold-72: its own pair ledger; judged on its own, next to the replay it is testing.
    from experiments import hold72
    mine = by_system.get("hold72", [])
    closed = [r for r in mine if r["status"] == "closed"]
    rep = hold72.replay()
    experiments.append({
        "key": "hold72", "label": hold72.LABEL, "started_at": books.started_at("hold72"),
        "description": "Follows scanner signals with |conviction| ≥ 0.45 for 72 hours, each against an equal, opposite "
                       "BTC position, with only a 12% disaster stop. Tests the year-long replay below going forward.",
        "running_days": round((time.time() - books.started_at("hold72")) / 86400, 1), **_stats(mine),
        "pending_orders": 0, "verdict": _verdict_single(closed, rng), "curve": _curve(closed),
        "config": hold72.config(), "replay": rep["book_rule"] if rep else None,
        "by_side": {side: _stats([r for r in mine if r["direction"] == side]) for side in ("long", "short")},
    })

    # Event shorts and Fade-24: pair ledgers of their own, judged like any trade book.
    from experiments import events, fade24, unlocks, vol_premium
    for mod, key, desc in ((events, "events", "Shorts the coin Binance announces — a spot listing, a new perp's first week or a "
                                              "delisting — hedged with BTC, for 30 days. ~200 announcements since 2023: −29–46% "
                                              "vs BTC over the month, 68–86% of the time, both halves of the sample."),
                           (unlocks, "unlocks", "Shorts a coin into a large cliff unlock (≥1% of supply, insider-heavy or ≥5%), hedged "
                                                "with BTC, from 14 days before until the unlock. Keyrock, 16,000 events: ~90% weigh on "
                                                "price and the slide comes before the date (median −9% in the last two weeks). Forward test."),
                           (vol_premium, "vol_premium", "Sells a weekly iron condor on BTC and ETH off Deribit's public quotes (short ±1σ, "
                                                        "long ±2σ), worst case 5% of equity, held to settlement. Our two years: BTC implied vol "
                                                        "3 points over realised on 69% of days; ≈ +1.8% of notional a month. Thinner lately."),
                           (fade24, "fade24", "Trades against strong scanner conviction for 24h, hedged with BTC — the reversal "
                                              "that has been beating the scanner's own direction.")):
        mine = by_system.get(key, [])
        closed = [r for r in mine if r["status"] == "closed"]
        card = {"key": key, "label": mod.LABEL, "started_at": books.started_at(key), "description": desc,
                "running_days": round((time.time() - books.started_at(key)) / 86400, 1), **_stats(mine),
                "pending_orders": 0, "verdict": _verdict_single(closed, rng), "curve": _curve(closed), "config": mod.config()}
        if key == "events":
            st_ = events.status()
            card["events"] = {"pending": st_["pending"], "open": [{"symbol": p["symbol"], "kind": p["kind"], "mark_pct": p["mark_pct"],
                                                                   "deadline_ts": p["deadline_ts"]} for p in events.open_positions()]}
        if key == "unlocks":
            card["unlocks"] = unlocks.status()
        if key == "vol_premium":
            card["vol_premium"] = vol_premium.status()
        experiments.append(card)

    # Retired books (trading/book_switches.RETIRED_BOOKS) keep their records but drop off the Lab.
    from trading.book_switches import RETIRED_BOOKS
    experiments[:] = [e for e in experiments if e["key"] not in RETIRED_BOOKS]

    # Carry Q: the quarterly-basis variant — judged on the basis it locks against what funding would have paid.
    from experiments import carry_q
    mine = by_system.get("carry_q", [])
    stq = carry_q.status()
    days_q = max((time.time() - books.started_at("carry_q")) / 86400, 1e-9)
    experiments.append({
        "key": "carry_q", "label": carry_q.LABEL, "started_at": books.started_at("carry_q"),
        "description": "Owns BTC and ETH and shorts their current-quarter futures instead of the perpetuals: the return is "
                       "locked at entry (the basis) rather than floating with funding, and rolls a week before delivery.",
        "running_days": round(days_q, 1), **_stats(mine), "pending_orders": 0,
        "verdict": {"status": "collecting", "label": "Running — judged on the basis locked vs the perp's funding"},
        "curve": _curve([r for r in mine if r["status"] == "closed"]),
        # Returns are against the book's capital after programme deposits (trading/capital.py), not the seed balance.
        "carry_q": {**stq, "initial": capital.initial("carry_q", carry_q.INITIAL_BALANCE),
                    "return_pct": round((stq["equity"] / capital.initial("carry_q", carry_q.INITIAL_BALANCE) - 1) * 100, 3)},
    })

    # Carry: positions stay open for weeks, so it's judged on equity and funding, not closed trades.
    from experiments import carry
    mine = by_system.get("carry", [])
    st = carry.status()
    days = max((time.time() - books.started_at("carry")) / 86400, 1e-9)
    experiments.append({
        "key": "carry", "label": carry.LABEL, "started_at": books.started_at("carry"),
        "description": "Owns BTC, ETH and tokenized gold (PAXG) and shorts the same amount of their perpetuals: price risk "
                       "cancels, funding is collected. BTC/ETH passed an untouched holdout year; gold's perp has paid the most.",
        "running_days": round(days, 1), **_stats(mine), "pending_orders": 0,
        "verdict": {"status": "collecting", "label": "Running — judged on funding earned vs the holdout year"},
        "curve": _curve([r for r in mine if r["status"] == "closed"]),
        "carry": {**st, "initial": capital.initial("carry", carry.INITIAL_BALANCE),
                  "return_pct": round((st["equity"] / capital.initial("carry", carry.INITIAL_BALANCE) - 1) * 100, 3),
                  "annualised_pct": round((st["equity"] / capital.initial("carry", carry.INITIAL_BALANCE) - 1) * 100 * 365 / days, 2) if days >= 3 else None},
        "research": carry.research(),
    })

    comparisons = []
    for c in COMPARISONS:
        since = books.started_at(c["since_book"])
        pick = lambda systems: [r for s in systems if in_ledger(s, ledger) or s in books.LABELS
                                for r in by_system.get(s, []) if (r["opened_ts"] or 0) >= since]
        with_rows, without_rows = pick(c["with"]), pick(c["without"])
        wc = [r for r in with_rows if r["status"] == "closed"]
        nc = [r for r in without_rows if r["status"] == "closed"]
        comparisons.append({
            "key": c["key"], "title": c["title"], "since": since,
            "with": {"systems": [s for s in c["with"] if in_ledger(s, ledger) or s in books.LABELS],
                     **_stats(with_rows), "curve": _curve(wc)},
            "without": {"systems": c["without"], **_stats(without_rows), "curve": _curve(nc)},
            "verdict": _verdict_paired(wc, nc, rng),
        })

    from experiments import research_index
    # Rejected studies move to the Retired tab (experiments/retired.py); the Lab keeps what is in use or still open.
    shelf = [e for e in research_index.shelf() if e["status"] != "rejected"]
    return {"experiments": experiments, "research_shelf": shelf, "comparisons": comparisons, "min_trades": MIN_TRADES,
            "claude_swing": {**claude_swing.status(), "reviews": claude_swing.reviews()[:20]},
            "enabled": books.ENABLED, "ledger": ledger, "updated_at": time.time()}
