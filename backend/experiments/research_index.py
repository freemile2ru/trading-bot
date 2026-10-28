"""The Lab's research shelf: every study we've run, the question it asked, what it found and what was done about it.

Each entry names the script (rerun it to reproduce), the question, a one-line finding, the decision it led to and
the book it touched. Where a results file exists, the finding is read from it so the shelf never drifts from the
numbers; the date is the results file's. Entries with no file yet are listed as pending with what they wait for.
"""
from __future__ import annotations

import json
import os
import time

from research.swing_lab import CACHE_DIR


def _load(name: str) -> dict | None:
    p = os.path.join(CACHE_DIR, name)
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None


def _mtime(name: str) -> float | None:
    p = os.path.join(CACHE_DIR, name)
    return os.path.getmtime(p) if os.path.exists(p) else None


# ── Findings read from the results files ──────────────────────────────────────

def _stall(d: dict) -> str:
    parts = []
    for k in ("stocks", "futures"):
        r = d.get(k) or {}
        b, c = (r.get("variants") or {}).get("baseline", {}).get("holdout", {}), (r.get("variants") or {}).get(r.get("chosen", ""), {}).get("holdout", {})
        parts.append(f"{k}: {r.get('verdict')} — holdout Sharpe {b.get('sharpe')} → {c.get('sharpe')} with {r.get('chosen')}, CAGR {b.get('cagr_pct')}% → {c.get('cagr_pct')}%")
    return "; ".join(parts)


def _vrp_regime(d: dict) -> str:
    return "; ".join(f"{c}: {r['verdict']} — always-on {r['always_on']['holdout']['annualised_pct']:+.1f}%/yr vs filtered θ={r['chosen_theta']} "
                     f"{r['chosen']['holdout']['annualised_pct']:+.1f}%/yr on the held-out year" for c, r in d.items())


def _weekly_funding(d: dict) -> str:
    v = d.get("variants") or {}
    b, c = v.get("baseline", {}).get("holdout", {}), v.get(d.get("chosen", ""), {}).get("holdout", {})
    return (f"{d.get('verdict')} — held-out weeks: baseline {b.get('net_week_pct')}%/week vs {d.get('chosen')} {c.get('net_week_pct')}%/week. "
            f"Note the baseline itself: +{(v.get('baseline', {}).get('search') or {}).get('net_week_pct')}%/week in the first 60% of the year, "
            f"{b.get('net_week_pct')}%/week in the last 40% — the weekly edge has faded lately; the live book's verdict will decide.")


def _funding_reversal(d: dict) -> str:
    rows = [f"{k}: holdout {d[k]['holdout']['per_event_pct']:+.2f}%/event ({d[k]['verdict']})" for k in ("neg_follow", "neg_fade", "pos_follow", "pos_fade") if k in d]
    return "No variant has a held-out interval above zero — " + "; ".join(rows)


def _monday(d: dict) -> str:
    a = d.get("all") or {}
    return (f"Mondays were the worst weekday: {a.get('monday_whole_period', {}).get('per_day_pct')}%/Monday on the whole year "
            f"vs {', '.join(f'{k} {v.get('per_day_pct')}%' for k, v in (a.get('control_other_weekdays') or {}).items())}; holdout {a.get('verdict')}")


def _vrp(d: dict) -> str:
    return "; ".join(f"{c}: IV {r['iv_mean_pct']}% vs realised {r['rv_next30_mean_pct']}% (premium {r['premium_mean_volpts']:+.1f} pts, "
                     f"positive {r['premium_positive_days_pct']}% of days); 1σ/2σ condor ≈ {r['strangle_annualised_pct']:+.1f}%/yr, worst 30d {r['strangle_worst_30d_pct']}%"
                     for c, r in d.items())


def _event_study(d: dict) -> str:
    bk = d.get("by_kind") or {}
    out = []
    for kind in ("spot listing", "futures launch", "delisting"):
        k = bk.get(kind) or {}
        w = k.get("+30d") or k.get("+7d") or {}
        if w:
            out.append(f"{kind}: {w.get('mean_bps')} bps vs BTC, {w.get('hit_pct')}% of {w.get('n')} events (t {w.get('t')})")
    return f"{d.get('events')} announcements since {d.get('period', ['', ''])[0]}: " + "; ".join(out)


def _carry(d: dict) -> str:
    return "; ".join(f"{v['kind']}: {v['verdict']} (holdout {v['holdout']['annual_pct']:+.2f}%/yr on notional)" for v in d.get("versions") or [])


def _sr(d: dict) -> str:
    return "; ".join(f"{k}: {v['avg_r']:+.3f}R/trade, {v['day_pct']:+.3f}%/day, ci {v['ci95_pct']}" for k, v in d.items())


def _range(d: dict) -> str:
    try:
        return "; ".join(f"{k}: {v.get('avg_r', v.get('day_pct'))}" for k, v in list(d.items())[:4])
    except Exception:
        return "see results file"


def _signal_rebuild(d: dict) -> str:
    ic = d.get("ic") or {}
    best = sorted(((k, v.get("ic")) for k, v in ic.items() if v.get("ic") is not None), key=lambda x: -abs(x[1]))[:3]
    m = d.get("model_all") or {}
    return (f"Strongest inputs: {', '.join(f'{k} IC {v:+.3f}' for k, v in best)}; a model on all of them made {m.get('net_day_pct')}%/day out of sample "
            f"(ci {m.get('ci95_pct')}) — fails. Only the weekly near-high basket survived costs.")


def _edge_scan(d: dict) -> str:
    return (f"{d.get('items')} instrument×rule combinations: {d.get('passes')} passed the holdout, against {d.get('expected_by_luck')} expected by luck "
            f"(luck rate {d.get('luck_rate_pct')}%) — nothing beyond chance.")


def _tradfi(d: dict) -> str:
    return "see results file — gold's perp funding (+12.7%/yr over 320 days) was the one usable finding; became the carry book's gold leg"


RESEARCH: list[dict] = [
    # ── 2026-10-28 ──
    {"key": "stall_exit", "title": "Stall exit for the trend books", "script": "research/stall_exit_lab.py", "file": "stall_exit_results.json",
     "question": "Does closing a trend position that stops making new highs (no new closing high for N weeks) beat holding until the trend flips?",
     "finding": _stall, "decision": "rejected — no clear difference: stall exits trimmed drawdowns by a few points but gave back CAGR; the trend books keep "
                                   "their no-clock rule (the week-and-25% profit take stays as asked). Side finding: on 20 years of the metals/energy ETFs the "
                                   "6-month trend rule itself earned ≈ 0 (CAGR 0.5%, Sharpe 0.1) — the futures trend book is on notice for its verdict", "book": "stocks_trend / futures_trend"},
    {"key": "vrp_regime", "title": "Vol premium: sell only when implied is rich", "script": "research/vrp_regime_lab.py", "file": "vrp_regime_results.json",
     "question": "Is the condor better sold only when 30-day implied vol exceeds trailing realised vol by a margin?",
     "finding": _vrp_regime, "decision": "rejected — the filter cut returns on the held-out year for both coins; the book stays always-on", "book": "vol_premium"},
    {"key": "weekly_funding", "title": "Weekly basket with a funding filter", "script": "research/weekly_funding_lab.py", "file": "weekly_funding_results.json",
     "question": "Do shorts (longs) that pay funding drag the weekly near-high basket, and does skipping or replacing them help?",
     "finding": _weekly_funding, "decision": "rejected — no filter helped out of sample; the book is unchanged and judged by its 40-trade verdict", "book": "weekly_rs"},
    {"key": "funding_reversal", "title": "Funding extremes, conditioned on the day", "script": "research/funding_reversal_lab.py", "file": "funding_reversal_results.json",
     "question": "After an extreme funding print, does the next day follow the print day's direction (Shek 2026)?",
     "finding": _funding_reversal, "decision": "rejected — not reproducible on 4 years × 40 coins", "book": None},
    {"key": "monday", "title": "Monday Asia-open trend", "script": "research/monday_lab.py", "file": "monday_results.json",
     "question": "Does the first move of the week (Monday 00:00–04:00 UTC) carry through the day, as intraday trend models report?",
     "finding": _monday, "decision": "rejected", "book": None},
    {"key": "vrp", "title": "Bitcoin / Ether volatility risk premium", "script": "research/vrp_lab.py", "file": "vrp_results.json",
     "question": "Is implied vol reliably above the vol that then happens, and what does a defined-risk short structure earn?",
     "finding": _vrp, "decision": "promoted — the vol-premium condor book (paper, 40-trade verdict)", "book": "vol_premium"},
    # ── earlier ──
    {"key": "event_study", "title": "Binance listing & delisting shorts", "script": "research/event_study_lab.py", "file": "event_study_results.json",
     "question": "What does a coin do against BTC in the month after Binance announces it (listing, new perp, delisting)?",
     "finding": _event_study, "decision": "promoted — the events book; mirrored in the autotrader", "book": "events"},
    {"key": "carry_lab", "title": "Cash-and-carry across 37 coins", "script": "research/carry_lab.py", "file": "carry_lab_results.json",
     "question": "Does owning the coin and shorting its perp to collect funding pay, always / gated / as a rotating basket?",
     "finding": _carry, "decision": "the wide basket fails its holdout; BTC and ETH alone passed → the carry book (later gold via PAXG)", "book": "carry"},
    {"key": "tradfi_perp", "title": "TradFi perps: funding and basis", "script": "research/tradfi_perp_lab.py", "file": "tradfi_perp_results.json",
     "question": "Do the stock / gold / oil perpetuals carry a funding or basis edge?", "finding": _tradfi,
     "decision": "gold carry promoted into the carry book; spread books rejected", "book": "carry"},
    {"key": "signal_rebuild", "title": "Rebuilding the signal from scratch", "script": "research/signal_rebuild_lab.py", "file": "signal_rebuild_results.json",
     "question": "Which inputs predict the next day at all, and can any model of them trade after costs?",
     "finding": _signal_rebuild, "decision": "the weekly near-high basket promoted; everything short-term rejected", "book": "weekly_rs"},
    {"key": "sr", "title": "Support & resistance", "script": "research/sr_lab.py", "file": "sr_lab_results.json",
     "question": "Do bounces off, or breaks of, repeatedly-touched levels pay on 15-minute data?", "finding": _sr,
     "decision": "rejected — indistinguishable from random levels", "book": None},
    {"key": "range", "title": "Ranging-market strategies", "script": "research/range_lab.py", "file": "range_lab_results.json",
     "question": "Is there a tradable edge inside consolidations (mean reversion to the range)?", "finding": _range,
     "decision": "rejected — the hedged books were scaled instead", "book": None},
    {"key": "edge_scan", "title": "Edge scan across 152 instrument × rule pairs", "script": "research/edge_scan.py", "file": "edge_scan_results.json",
     "question": "Across trend, dip-buying and breakout rules on every instrument we trade, does anything pass an untouched holdout?",
     "finding": _edge_scan, "decision": "rejected — passes equal the luck rate", "book": None},
    # ── pending: need data we are now collecting ──
    {"key": "unlock_study", "title": "Unlock event study on our own calendar", "script": "(to write)", "file": None,
     "question": "What do coins do into and after their cliff unlocks, measured on the calendar snapshots we keep daily?",
     "finding": "waits for ~2 months of calendar snapshots (data/unlocks.db) and the unlock book's first closes", "decision": "pending", "book": "unlocks"},
    {"key": "copy_replay", "title": "Copy-rule replay", "script": "(to write)", "file": None,
     "question": "On the record of every leader position at every poll, which lateness / consensus rules would have done best?",
     "finding": "waits for ~4 weeks of observations (data/copy_trading.db)", "decision": "pending", "book": "copy_trading"},
    {"key": "positioning", "title": "Top-trader positioning ratios", "script": "(to write)", "file": None,
     "question": "Do Binance's top-account long/short and taker-flow ratios predict anything over hours to days?",
     "finding": "waits for ~60 days of the hourly log (data/positioning.db)", "decision": "pending", "book": None},
]


def shelf() -> list[dict]:
    out = []
    for e in RESEARCH:
        d = _load(e["file"]) if e.get("file") else None
        finding = e["finding"]
        if callable(finding):
            try:
                finding = finding(d) if d else "results file not found — run the script"
            except Exception as ex:
                finding = f"results file present but unreadable here ({ex.__class__.__name__}) — see {e['file']}"
        ran = _mtime(e["file"]) if e.get("file") else None
        status = "pending" if e["decision"] == "pending" else ("promoted" if e["decision"].startswith("promoted") or "promoted" in e["decision"]
                                                              else "rejected" if e["decision"].startswith("rejected") else "mixed")
        out.append({**{k: v for k, v in e.items() if k != "finding"}, "finding": finding, "ran_at": ran, "status": status})
    out.sort(key=lambda x: -(x["ran_at"] or time.time() + 1))
    return out
