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


def _weekly_exits(d: dict) -> str:
    b = d.get("baseline") or {}
    best = (d.get("variants") or [{}])[0]
    take = next((v for v in d.get("variants") or [] if v.get("take") == 0.15 and v.get("stop") is None and v.get("sizing") == "equal"), {})
    s10 = next((v for v in d.get("variants") or [] if v.get("stop") == 0.10 and v.get("take") is None and v.get("sizing") == "equal"), {})
    return (f"Baseline (30% stop, no take-profit): {b.get('net_week_pct')}%/week, legs win {b.get('leg_win_rate_pct')}% at avg +{b.get('avg_win_leg_pct')}% vs "
            f"{b.get('avg_loss_leg_pct')}% (payoff {b.get('payoff')}), stops hit {b.get('stops_hit_pct')}% of legs. A 15% take-profit collapses it to "
            f"{take.get('net_week_pct')}%/week — the return IS the winning legs running. A 10% stop lifts the payoff to {s10.get('payoff')} and the worst week to "
            f"{s10.get('worst_week_pct')}% but nets {s10.get('net_week_pct')}%/week with the halves split ({s10.get('first_half')} / {s10.get('second_half')}). "
            f"Best overall: no stop at all ({best.get('net_week_pct')}%/week). " + _weekly_trails(d) + f" — {d.get('verdict')}")


def _weekly_trails(d: dict) -> str:
    tr = d.get("trails") or []
    if not tr:
        return ""
    best, b = tr[0], d.get("baseline") or {}
    worse = sum(1 for v in tr if (v.get("net_week_pct") or 0) < (b.get("net_week_pct") or 0))
    a, dist = best["trail"]
    return (f"Trailing stops ({len(tr)} variants, 30% stop kept): {worse} of {len(tr)} earn less than the baseline; the best, a "
            f"{int(dist * 100)}% trail {'from entry' if not a else f'armed at +{int(a * 100)}%'}, nets {best.get('net_week_pct')}%/week "
            f"({best.get('first_half')} / {best.get('second_half')} by half) — trailing cuts the winners the basket lives on")


def _entry(d: dict) -> str:
    ic = (d.get("ic") or {}).get("xs_24h") or {}
    top = ", ".join(f"{k} {v['ic']:+.2f}" for k, v in list(ic.items())[:6])
    bt, yc = d.get("book_test") or {}, d.get("year_check") or {}
    return (f"{d.get('rows')} scans of {d.get('coins')} coins, {d.get('from')} → {d.get('to')}, one per coin per 4h. Against the next 24h vs the cross-section "
            f"the strongest readings are all NEGATIVE — {top} — i.e. what the scanner likes reverses over the next day in this period. A score of the "
            f"{len(d.get('chosen') or {})} readings that passed on the first half has IC {d.get('score_test_ic')} on the second half (t {d.get('score_test_t')}); "
            f"long the top 5 / short the bottom 5 every 4h for 24h nets {bt.get('avg_pct')}% a slot after costs ({bt.get('first_half_pct')} / {bt.get('second_half_pct')} by "
            f"quarter), where the scanner's own conviction nets {(d.get('book_conviction_test') or {}).get('avg_pct')}%. Over the year-long 4h panel the plain "
            f"24h-momentum fade loses {yc.get('avg_pct')}% a slot ({yc.get('first_half_pct')} / {yc.get('second_half_pct')}). {d.get('verdict')}")


def _auction(d: dict) -> str:
    q = (d.get("symbols") or {}).get("QQQUSDT") or {}
    g = q.get("geometry") or {}
    bc = q.get("by_cost") or {}
    def at(c):
        b = (bc.get(c) or {}).get("high_grade") or {}
        return f"{b.get('avg_net_pct')}% a trade, {b.get('win_pct')}% won"
    others = ", ".join(f"{s.replace('USDT', '')} {((r.get('high_grade') or {}).get('all') or {}).get('avg_net_pct')}%"
                       for s, r in (d.get("symbols") or {}).items() if s != "QQQUSDT" and r.get("trades"))
    return (f"On QQQ (the Nasdaq proxy the study was written for): {q.get('trades')} trades since {q.get('from')}, {g.get('trades_per_day')} a day; the median "
            f"target sits {g.get('median_target_bps')} bps from entry with {g.get('median_risk_bps')} bps of risk — NQ-sized geometry. Grade ≥6 trades: "
            f"gross {at('0.0')}; at CME micro-futures costs (0.5 bp a side) {at('0.5')}; at 2 bps {at('2.0')}; at Binance perp costs (8 bps) {at('8.0')}. "
            f"Other perps at Binance costs: {others}. {d.get('verdict')}")


def _cascade(d: dict) -> str:
    live, ch = d.get("live") or {}, d.get("chosen") or {}
    return (f"The live rule (fade the minute after the burst, 3% stop) replayed on the recorder's minute bars: {live.get('avg_bps')} bps a trade, "
            f"{live.get('win_pct')}% winners, halves {live.get('first_half')} / {live.get('second_half')} bps, the stop closed {live.get('stops_hit_pct')}% "
            f"of legs — the cascade keeps running for about an hour (every 1h and 2h hold lost 32–50 bps). Entering 60 minutes later with no stop: "
            f"{ch.get('avg_bps')} bps a trade, {ch.get('win_pct')}% winners, t {ch.get('t')}, halves {ch.get('first_half')} / {ch.get('second_half')} bps "
            f"({ch.get('events')} events); an 8% disaster stop costs 1 bps of that. Three days carry the gain. {d.get('verdict')}")


def _multi_factor(d: dict) -> str:
    fam = "; ".join(f"{k} Sharpe {v['test'].get('sharpe')}" for k, v in (d.get("families") or {}).items())
    best = max((d.get("combinations") or []), key=lambda c: c["test"].get("sharpe") or -99, default=None)
    chosen = next((c for c in d.get("combinations") or [] if c["label"] == d.get("chosen")), None)
    return (f"Single families on the test half: {fam}. Train correlations ≈ 0 (−0.2…+0.1). Train-chosen combination "
            f"'{d.get('chosen')}': test Sharpe {chosen['test'].get('sharpe') if chosen else None}, 95% range "
            f"{chosen['test'].get('mean_daily_ci_bps') if chosen else None} bps/day; best on test was '{best['label'] if best else None}' "
            f"(Sharpe {best['test'].get('sharpe') if best else None}, max DD {best['test'].get('max_drawdown_pct') if best else None}%) — {d.get('status')}")


def _top_traders(d: dict) -> str:
    c = d.get("cohorts") or {}
    w, l = c.get("winners") or {}, c.get("losers") or {}
    if not w.get("episodes"):
        return "no cohort data"
    return (f"Winners ({w['episodes']} round trips, {len(w.get('wallets', []))} wallets): median hold {w.get('median_hold_h')}h, "
            f"{w.get('win_rate_pct')}% won, avg {w.get('avg_move_pct')}% a trade, {w.get('long_share_pct')}% long, "
            f"{w.get('continuation_share_pct')}% entered with the prior 24h move, exits left {w.get('avg_signed_after_exit_24h_pct')}% on the table. "
            f"Losers ({l.get('episodes')}): median hold {l.get('median_hold_h')}h, {l.get('win_rate_pct')}% won, avg {l.get('avg_move_pct')}%, "
            f"{l.get('continuation_share_pct')}% continuation.")


def _tradfi(d: dict) -> str:
    return "see results file — gold's perp funding (+12.7%/yr over 320 days) was the one usable finding; became the carry book's gold leg"


RESEARCH: list[dict] = [
    # ── 2026-10-31 ──
    {"key": "entry_edge", "title": "Entry edge: what the scanner's own readings say about the next day", "script": "research/entry_lab.py",
     "file": "entry_results.json",
     "question": "The autotrader's exits earn and its entries lose. Of everything the scanner records every few minutes — positioning, flow, funding, "
                 "news, fundamentals, its own scores — which readings predict the next 4h / 24h, and in which direction?",
     "finding": _entry,
     "decision": "regime-dependent, run as a lane — the five-week reversal score is strong out of sample but the year-long panel says fading momentum "
                 "loses in trending markets; the autotrader's new 'reversal' lane trades the score at measuring size and the lane allocator sizes it "
                 "by its own record per regime (down/calm vs up/wild), which is exactly the question left open. 2026-11-02: split by side on the "
                 "lab's 190 slots, the SHORT side (sell what the scanner likes most) earned +1.7%/slot vs BTC after costs (t 4.9, both halves "
                 "positive) and the LONG side (buy what it likes least) lost −0.4%/slot in both halves and under both BTC trends; the lane now "
                 "runs short-only (AUTOTRADER_REVERSAL_SIDES)", "book": "auto"},
    {"key": "auction_levels", "title": "Auction-level acceptance (a mentor's NQ thinkScript), ported and replayed", "script": "research/auction_lab.py",
     "file": "auction_results.json",
     "question": "The mentor's 5-minute NQ system — accept beyond PDH/ONH/ORB/IB-type levels, confirm on the next close, trade to the next level with "
                 "the stop under the acceptance bar, graded by HTF bias, momentum, volume and ATR — does it earn money on our stock and index perps?",
     "finding": _auction,
     "decision": "rejected — ported in full (trading/auction_levels.py) and replayed on 7 months of 5-minute bars: before any costs the system is "
                 "breakeven (QQQ −0.006%, NVDA +0.011%, TSLA +0.013% a trade, 44% won); its targets and stops sit ~12 bps from entry on QQQ, so "
                 "Binance's 8 bps a side turns every trade into −0.17%. No paper book; the port stays for the user's own experiments and for an "
                 "NQ-futures feed, where costs are ~0.5 bp and the geometry was designed. Variants built around the weaknesses (minimum target "
                 "distance, grade ≥ 7, continuation only, reward ≥ 1.5× risk) were all still negative at Binance costs; the least bad, target ≥ 50 bps "
                 "and grade ≥ 7, lost 0.13% a trade. The mentor's own reading — enter on the RETEST after confirmation, aim at the level beyond the "
                 "nearest — was then tested (2026-11-01): at Binance costs it loses like the rest (−0.13%), but at futures costs it is the one "
                 "arrangement positive in both halves (+0.017% a trade, 500 trades). The 'auction' book runs retest + second level with the "
                 "target-distance and grade gates at $1,000, judged on its own 40 trades", "book": "auction"},
    # ── 2026-10-30 ──
    {"key": "copy_venues", "title": "Copy trading: which other platforms expose their leaders' positions?", "script": "data/lighter_client.py",
     "question": "Beyond Binance's copy board and Hyperliquid, which venues let us read a top trader's live positions without a key, so the copy "
                 "books see more positions opening?",
     "finding": "Probed 2026-10-30 from this machine. Readable: Lighter (zkLighter) — a 100k-account P&L leaderboard with 24h/7d/30d windows, any "
                "account's positions with average entry and liquidation, its full trade stream (one market id returns every market) and a daily P&L "
                "series; 246 perp markets including US stocks (AAPL, AMD, MU, QQQ, SAMSUNG) and commodities (XAG, XAU, WTI, NATGAS, copper). GMX "
                "— its public GraphQL (gmx.squids.live) ranks accounts by P&L per period and lists open positions with entry and leverage. Not "
                "readable: OKX blanks a lead trader's current instrument, entry and size (only closed history is visible); Bitget's copy-trading "
                "endpoints are all private; Bybit's and Gate's leader pages sit behind Akamai bot protection; Aster needs a key for another "
                "trader's positions; dYdX's indexer gives positions by address but has no leaderboard; Paradex needs a JWT; Drift's data hosts "
                "did not answer. Follow-up the same day: Hyperliquid's own builder market (HIP-3 'xyz') lists stock, commodity, index "
                "and FX perps (xyz:TSLA, xyz:GOLD, xyz:SILVER, xyz:CL…) and three of its top-25 accounts hold them — positions sit in a separate "
                "clearinghouse state (dex='xyz') that the client had not been reading; and Binance's own exchange info classifies every perp "
                "(underlyingType EQUITY / COMMODITY / COIN), which now routes each copy to its market instead of a hand-kept list.",
     "decision": "promoted — Lighter is a third venue in the copy books from 2026-10-30 (same bar as Hyperliquid: $50k–$20M equity, month ROI "
                 "5–300%, turnover 2–300× a month, then the profile and curve tests on its trades); GMX is the next candidate", "book": "copy_trading"},
    {"key": "cascade_replay", "title": "Cascade fade: the live rule replayed, with and without its stop", "script": "research/cascade_lab.py",
     "file": "cascade_results.json",
     "question": "The cascade book is 36% winners live, most closed by its 3% stop — is the stop, the hold, the threshold or the entry timing wrong?",
     "finding": _cascade,
     "decision": "rule changed (2026-10-30) — the immediate entry with a 3% stop lost in both halves, so it is off; the book now enters an hour after "
                 "the burst with an 8% disaster stop, as a fresh forward test judged only on trades opened under the new rule (its replay result "
                 "rests on three days and a flat first half, so it is not treated as proven)", "book": "cascade"},
    # ── 2026-10-29 ──
    {"key": "weekly_exits", "title": "Weekly basket: stops, take-profits and volatility sizing", "script": "research/weekly_exits_lab.py",
     "file": "weekly_exits_results.json",
     "question": "Do the 30% disaster stop and the absence of a take-profit make the weekly basket 'huge losses, small wins', and would tighter "
                 "stops, take-profits, trailing stops or volatility-scaled legs fix it?",
     "finding": _weekly_exits,
     "decision": "rejected (kept the baseline) — under the written rule no variant beat the 30%-stop baseline on both halves and the worst week; "
                 "take-profits destroy the edge, a 10% stop trades return for a smoother worst week, and trailing stops (added the same day, "
                 "16 arm × distance settings) all earned less than holding to Monday. The live book is unchanged", "book": "weekly_rs"},
    {"key": "multi_factor", "title": "Multi-factor portfolio: momentum + trend + funding carry together", "script": "research/multi_factor_lab.py",
     "file": "multi_factor_results.json",
     "question": "Do the three systematic families that quant desks run — held together, risk-weighted, vol-targeted — clear the holdout where each alone did not?",
     "finding": _multi_factor,
     "decision": "mixed — the combination diversifies (test Sharpe up to 1.25 with a smaller drawdown than any family alone) but the train-chosen "
                 "version's 95% range still includes zero, so it was not promoted; like Hold-72 it runs as a $1k forward test "
                 "(the multi_factor book, risk parity, no vol targeting) judged by the 40-trade verdict", "book": "multi_factor"},
    {"key": "top_traders", "title": "What the top Hyperliquid wallets actually do", "script": "research/top_traders_lab.py", "file": "top_traders_results.json",
     "question": "From their fills: do the most profitable wallets trade continuation or reversal, how long do they hold, and how does that differ from the losing cohort?",
     "finding": _top_traders, "decision": "descriptive — read alongside the copy-trading book's leader bar", "book": "copy_trading"},
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
    {"key": "news_fade", "title": "Fade the headline (coins in the news over the next day)", "script": "research/news_fade_lab.py", "file": "news_fade_results.json",
     "question": "Coins in the news underperformed over the next 24 hours in the reaction log (−0.25% on average, worse for upgrades and partnerships): does shorting after an item, or fading the called direction, pay after costs?",
     "finding": lambda d: f"{d.get('n_items', 0)} coin-day items; " + "; ".join(
         f"{rule} @ {h}: {v[h]['all'].get('avg_pct', 0):+.2f}% a trade after costs, second half {v[h]['second'].get('avg_pct', 0):+.2f}% (95% {v[h]['second'].get('ci95_sum')})"
         for rule, v in (d.get("rules") or {}).items() for h in ("r_24h",)),
     "decision": "rejected (2026-11-04) — no rule's held-out interval clears zero: shorting every coin in the news earns +0.05% a trade on the second half with an interval of ±700; fading Claude's called direction is the one arrangement that leans positive (+0.9% a trade on 52 held-out items, interval −20 … +120) and is re-run once 200 strongly-called items exist. Nothing is built", "book": None},
    {"key": "continuation", "title": "One-hour continuation (the scanner's one positive input)", "script": "research/continuation_lab.py", "file": "continuation_results.json",
     "question": "ret_1h was the only scanner input with a positive coefficient against the 4-hour forward move (IC +0.12, t 6.2 over 145,000 readings): does buying the hour's strongest coins and selling its weakest pay after costs?",
     "finding": lambda d: f"{d.get('readings', 0):,} readings in {d.get('slots', 0)} 4h slots; deciles of ret_1h run from {d['deciles'][0]['fwd_4h_bps']:+.0f} to {d['deciles'][-1]['fwd_4h_bps']:+.0f} bps over the next 4h (monotonic) — but the top-{d.get('top_k')}/bottom-{d.get('top_k')} rule earned " + "; ".join(
         f"{k}: {v['all'].get('avg_leg_pct', 0):+.2f}% a leg, second half {v['second'].get('avg_leg_pct', 0):+.2f}% (95% {v['second'].get('ci95_sum')})" for k, v in (d.get("rules") or {}).items() if k in ("top_bottom_raw", "top_long_raw", "bottom_short_raw")),
     "decision": "rejected (2026-11-04) — the signal is real in the deciles (−89 bps for the weakest tenth, +56 bps for the strongest, monotonic in between) but the tradeable extremes are not the deciles: the three strongest coins of a sweep are spikes that give it back, and every rule lost after the 16 bps round trip, the long/short pair conclusively (held-out interval −173 … −64). The moderate deciles (8–9, +6 … +21 bps) are below costs. Nothing is built", "book": None},
    {"key": "funding_venues", "title": "Cross-venue funding arbitrage (Binance vs Hyperliquid)", "script": "(to write)", "file": None,
     "question": "Does the funding gap between the two venues on the same coin clear a delta-neutral round trip, and how long does it last?",
     "finding": "waits for ~4 weeks of the hourly log (data/funding_venues.db, started 2026-10-29)", "decision": "pending", "book": "carry"},
    {"key": "exchange_events", "title": "Upbit listing & delisting events", "script": "(to write)", "file": None,
     "question": "Do Upbit's trade notices move the named coin against BTC the way Binance's do (the Korean listing pump-and-fade)?",
     "finding": "waits for Upbit notices in news.db (source added 2026-10-29) plus the archive to replay", "decision": "pending", "book": "events"},
]


# Research runs on the research machine; a production instance ships no results files and never will.
NO_RESULTS_HERE = ("results not on this instance — the study was run on the research machine"
                   if os.getenv("APP_ENV") == "production" else "results file not found — run the script")


def shelf() -> list[dict]:
    out = []
    for e in RESEARCH:
        d = _load(e["file"]) if e.get("file") else None
        finding = e["finding"]
        if callable(finding):
            try:
                finding = finding(d) if d else NO_RESULTS_HERE
            except Exception as ex:
                finding = f"results file present but unreadable here ({ex.__class__.__name__}) — see {e['file']}"
        ran = _mtime(e["file"]) if e.get("file") else None
        status = "pending" if e["decision"] == "pending" else ("promoted" if e["decision"].startswith("promoted") or "promoted" in e["decision"]
                                                              else "rejected" if e["decision"].startswith("rejected") else "mixed")
        out.append({**{k: v for k, v in e.items() if k != "finding"}, "finding": finding, "ran_at": ran, "status": status})
    out.sort(key=lambda x: -(x["ran_at"] or time.time() + 1))
    return out
