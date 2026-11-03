"""The Retired tab: every experiment we stopped, why, and the evidence.

Each entry says what we believed, how it was tested, what the data showed, why it failed, what
we kept, and what code was removed. Both scalpers' numbers are computed live from their
trade and signal records (still on disk); research-only results cite the figures measured by the
named script on the date given — rerun the script to reproduce them.
"""
from __future__ import annotations

import math
import sqlite3
import time

from scalper import config as SC

# ── Registry ────────────────────────────────────────────────────────────────────
# `numbers` are (label, value, note) triples shown as the key figures.

RETIRED: list[dict] = [
    {
        "key": "copy_futures",
        "score_inputs": {"result_r": None, "result_note": "no closed copies", "t": None, "t_note": "nothing to test", "n": 0, "n_note": "copies in 7 days at $21.8k", "lost_pct": 0.0},
        "title": "Copy trading · commodity perps",
        "category": "Strategy",
        "active": "2026-10-29 → 2026-11-03",
        "retired": "2026-11-03",
        "hypothesis": "The same vetted leaders' positions in Binance's commodity perpetuals (gold, silver, oil, metals) can be copied like their crypto ones.",
        "method": "Its own $20k book (later $21.8k) fed by the shared leader selection on Binance, Hyperliquid and Lighter; the same entry rule, stop and sizing as the crypto book.",
        "numbers": [
            ("Copies", "0 in 7 days", "every commodity position the leaders held pre-dated tracking"),
            ("Flow", "leaders open a new commodity perp perhaps once a week across the whole pool", "a 40-trade verdict was a year away"),
        ],
        "why": ["Retired in the 2026-11-03 consolidation, with the user: idle capital that could not reach a verdict. The signal isn't disproven — "
                "it was never tested — but $21.8k waiting for one copy a week is not an experiment."],
        "kept": "The stock-perp copy book runs on. A leader's commodity position is still seen and recorded as a skip, so the flow stays measurable.",
        "removed": "Nothing deleted — the book is switched off, its capital moved to the crypto and stock copy books.",
        "evidence": "trading/book_switches.RETIRED_BOOKS",
    },
    {
        "key": "autotrader_ta_lane",
        "score_inputs": {"result_r": -0.13, "result_note": "−0.13R a trade over its last 20 (longs −0.17R)", "t": 1.2, "t_note": "t ≈ −1.2", "n": 20, "n_note": "closed trades on the ledger", "lost_pct": 1.4},
        "title": "Autotrader scanner lane (scanner conviction + Claude's confirmation)",
        "category": "Strategy",
        "active": "2026-09-25 → 2026-11-03",
        "retired": "2026-11-03",
        "hypothesis": "The scanner's multi-timeframe conviction, confirmed by Claude's review, picks swing trades that beat their costs.",
        "method": "The original autotrader path: scanner sweeps → candidates → Claude's veto → ATR stops and targets under the 'protect' exit policy; "
                  "later a daily-minimum entry and the lane allocator sizing it by its own record.",
        "numbers": [
            ("Ledger", "20 trades, −0.13R a trade; longs −0.17R over 20, shorts +0.39R over 3", "the allocator already had its longs at ¼ size"),
            ("Context", "signal-rebuild lab: the scanner's conviction has no predictive power; auto_rules, signals_rules, signals_claude all retired", "the same finding four times"),
            ("Claude's veto", "declined candidates averaged −0.14R", "the veto was right; there was nothing left to approve"),
        ],
        "why": ["Retired in the 2026-11-03 consolidation, with the user. The lane was the last consumer of the scanner's conviction as a trade signal; "
                "the autotrader is now purely the executor of the evidence books and the reversal score."],
        "kept": "The scanner keeps running — the reversal lane is built from its scans — and AUTOTRADER_TA_LANE=true brings the lane back.",
        "removed": "Nothing deleted — no candidates are considered and no daily-minimum entry is forced.",
        "evidence": "autotrader/engine.TA_LANE, autotrader/lanes.py",
    },
    {
        "key": "claude_swing",
        "score_inputs": {"result_r": -0.16, "result_note": "−0.16R a trade over 12 closed trades", "t": 1.1, "t_note": "t ≈ −1.1 (too few trades to say more)",
                         "n": 12, "n_note": "closed trades (paper) in 24 days", "lost_pct": 39.5},
        "title": "Claude swing (Claude picking its own 1–7 day trades)",
        "category": "Strategy",
        "active": "2026-10-09 → 2026-11-02",
        "retired": "2026-11-02",
        "hypothesis": "Given the scanner's full picture every few hours, Claude choosing a few swing trades (or none) with its own stop and "
                      "target beats the rules it reads from.",
        "method": "A $500 paper book; Claude reviewed the market every few hours and could open at most a handful of positions, each "
                  "validated for reward:risk and a written brief. Judged on the usual 40-trade verdict.",
        "numbers": [
            ("Live trades", "12 closed, 42% won", "−0.16R a trade, −$198 net (−40% of the book)"),
            ("Pace", "12 trades in 24 days", "a verdict was two months away at best"),
            ("Context", "signal-rebuild lab: the scanner's conviction carries no information", "Claude was reading the same inputs"),
        ],
        "why": [
            "Retired in the 2026-11-02 consolidation, with the user: it was the smallest book, losing, slow to produce evidence, and "
            "every other Claude-confirmed book (signals_claude, stocks_claude, futures_claude) had already been retired on the same "
            "finding — the inputs it reads have no edge for it to add judgement to.",
        ],
        "kept": "The autotrader's scanner lane still uses Claude as a veto (its declines averaged −0.14R — the veto is right).",
        "removed": "Nothing deleted — experiments/claude_swing.py and its reviews stay; the book is switched off for good and its $500 "
                   "parked in the main wallet.",
        "evidence": "trading/book_switches.RETIRED_BOOKS",
    },
    {
        "key": "stocks_trend",
        "score_inputs": {"result_r": -0.23, "result_note": "−0.23R a trade over its 40 closed trades (90% range −0.37 … −0.10)",
                         "t": 2.8, "t_note": "t ≈ −2.8", "n": 40, "n_note": "closed trades (paper)", "lost_pct": 3.7},
        "title": "Stocks trend (6-month trend following on stock perps)",
        "category": "Strategy",
        "active": "2026-10-12 → 2026-10-30",
        "retired": "2026-10-30",
        "hypothesis": "Time-series momentum on equities: be long a stock perp while its underlying's 126-trading-day return is "
                      "positive, flat otherwise — the classic trend rule, sized to ~1% daily volatility a name with a 15% disaster stop.",
        "method": "Paper book on Binance's stock perpetuals, rebalanced daily after the US close from Yahoo history; closed on a "
                  "flip (with a 5% band), the stop, or a week-and-25% profit take. Judged on the usual 40-trade verdict.",
        "numbers": [
            ("Live trades", "40 closed, 42% won", "−0.23R a trade, −$202 net"),
            ("Exits", "20 expired (+$45), 10 flips (−$92), 8 stops (−$177), 2 profit takes (+$22)", "the losing exits outweighed every winner"),
            ("Verdict", "90% range −0.37 … −0.10R", "entirely below zero — the pre-set retirement bar"),
            ("Stall-exit lab (2026-10-28)", "6-month rule ≈ 0 on 20 years of sector ETFs", "CAGR 0.5%, Sharpe 0.1: the rule had little to give"),
        ],
        "why": [
            "Equity trend following works, when it works, over years and across many markets; on 18 names over 18 days of "
            "a choppy tape it bought every bounce and was flipped or stopped out of most of them. The research never showed "
            "an edge at this horizon — the 2026-10-28 ETF replay put the rule near zero — so the forward test confirmed the prior.",
        ],
        "kept": "The futures trend book (5 trades, positive so far) runs on, on notice. The stock perps stay in the copy-stocks book.",
        "removed": "Nothing deleted — markets/engine.py still runs the stocks market's scans; the trend book is switched off for good, "
                   "its 14 open positions were closed at retirement and its $5,500 parked in the main wallet.",
        "evidence": "trading/book_switches.RETIRED_BOOKS, research/stall_exit_lab.py (2026-10-28)",
    },
    {
        "key": "impulse_scalper",
        "title": "Impulse scalper",
        "category": "Strategy",
        "active": "2026-09-28 → 2026-10-06",
        "retired": "2026-10-06",
        "hypothesis": "Extreme moves keep going: when a coin moves several standard deviations in a minute on heavy "
                      "volume (or 150+ bps in five minutes), riding it at market with a stop sized from the move and "
                      "trailed behind the best price catches the continuation.",
        "method": "Paper engine on Binance's live futures feed for 12 liquid perpetuals. An event = the last 60 s moved "
                  "≥ 6σ of the past hour's 1-second volatility and ≥ 50 bps on ≥ 3× normal traded value, or the last "
                  "300 s moved ≥ 150 bps; one event per coin per burst. Taker entry in the move's direction, stop = "
                  "half the move (60–200 bps) trailed from the best price, 10-minute time stop, 0.25% risk per trade. "
                  "Every event was logged with its forward moves (5 s – 10 min) whether traded or not.",
        "why": [
            "The moves didn't continue. For the events it traded, the price move over the next 5 seconds to 10 minutes "
            "is indistinguishable from zero at every horizon (t below 1). Across all logged events the best reading "
            "was about +7 bps at 5 minutes (t ≈ 1.9) — not reliable, and still smaller than the cost of a trade.",
            "It lost before costs, then paid ~10 bps a trade on top: −5.4 bps per trade gross, −15 bps net.",
            "Stops did the damage: half the trades (102) were stopped out for −1,734 USDT; trailing stops (+585) and "
            "time stops (+128) couldn't make it back. Violent moves whipsaw — the stop sized from the move was hit by "
            "the next swing more often than the move extended.",
            "Same lesson as the order-flow scalper: short-horizon taker strategies need an edge several times their "
            "~10 bps round-trip cost, and neither scalper found one.",
        ],
        "kept": "The 1-second market recorder (scalper/recorder.py) — the same feed now only records bars and "
                "liquidations for the research labs. The trade and event records stay in impulse_trades.db (and on "
                "the Trades page, with 'include retired' on).",
        "removed": "scalper/impulse.py (detection, entries, trailing stop; kept in docs/retired-code/impulse.py), the "
                   "Impulse tab and /api/impulse routes, and its switch on the Books tab.",
        "evidence": "Live records: impulse_trades.db (trades; signals = every extreme move with forward returns).",
        "dynamic": "impulse",
    },
    {
        "key": "order_flow_scalper",
        "title": "Order-flow scalper",
        "category": "Strategy",
        "active": "2026-09-26 → 2026-09-29",
        "retired": "2026-09-29",
        "hypothesis": "A burst of one-sided aggressive trading (30-second trade-flow z-score ≥ 2) backed by order-book "
                      "imbalance and a 1-minute EMA trend predicts the next few minutes, and resting post-only (maker) "
                      "entries capture it cheaply enough to profit after fees.",
        "method": "Live engine on Binance's order-book and trade streams for 12 of the most liquid perpetuals: queue-modelled "
                  "maker entries, maker take-profit, market stop / time / flow-reversal exits, 0.25% risk per scalp. Ran on "
                  "paper, then live on the Binance testnet. Every candidate signal was logged with its forward price moves "
                  "(5 s – 2 min) whether or not it was traded.",
        "why": [
            "The signal was real but tiny. After a flow burst, price kept going the signal's way by about 1–2 bps over "
            "the next 5 seconds to 2 minutes — statistically clear (t ≈ 4–7 on ~1,550 traded signals, t ≈ 30 on 43,000 "
            "candidates), but a round trip costs roughly 7–11 bps. The edge was about a fifth of what it cost to trade it.",
            "Execution then turned that small plus into a loss before fees. Resting maker entries mostly fill when price "
            "comes back through them (≈2.6 bps adverse selection), and the flow-reversal exit — 227 of 355 trades — "
            "closed positions on noise: −1,333 USDT, versus +781 USDT from the 32 trades that reached their target.",
            "The entry filters (order-book imbalance, EMA trend, spread and VWAP guards) didn't pick better signals: at "
            "60–120 seconds the traded signals moved no more than raw flow bursts (2.0 vs 2.0 bps; 1.6 vs 2.6 bps).",
            "Tuning didn't rescue it: none of 30 stop/target combinations was positive out of sample "
            "(research/scalper_lab.py, 2026-09-27).",
            "Lesson: a 1–2 bps edge can only be harvested with near-zero trading costs (a market maker earning rebates), "
            "not by taking directional positions that pay fees on both sides.",
        ],
        "kept": "The engine base, order book with queue model, cost model and 1-second market recorder — the impulse "
                "scalper ran on them until it was retired too (2026-10-06); the recorder still runs. The trade and signal "
                "records stay in scalper_trades.db (and on the Trades page).",
        "removed": "scalper/strategy.py (entry/exit rules), the order-flow engine instance, the live scalper "
                   "(live/scalper.py — Binance user-data stream book), its history-reconcile tool, the Scalper tab and "
                   "/api/scalper routes, and the scalper's slot in the live controls and kill switch.",
        "evidence": "Live records: scalper_trades.db (trades, signals). Research: python -m research.scalper_lab",
        "dynamic": "scalper",
    },
    {
        "key": "fa_signal",
        "score_inputs": {"result_r": 0.0, "result_note": "no effect — its inputs didn't predict returns (IC ≈ 0)",
                         "t": None, "n": 14600, "n_note": "coin-days (1 year × 40 coins)", "lost_pct": 0.0},
        "title": "Fundamentals as a trading signal",
        "category": "Signal input",
        "active": "2026-09-26 → 2026-09-27 (off since), removed 2026-09-29",
        "retired": "2026-09-29",
        "hypothesis": "A fundamentals score — relative strength vs BTC, momentum shape, funding, stablecoin growth, liquidity "
                      "and supply — tells which way a coin will go, so it should veto trades against it and scale signal "
                      "strength with it.",
        "method": "research/swing_lab.py over one year of 40 perpetuals: rank correlation (information coefficient) of each "
                  "input with the next 24–72 h returns, with a planted-signal control to prove the test can find an effect.",
        "numbers": [
            ("Predictive power (IC) of the live FA inputs", "≈ 0", "relative strength, momentum shape, funding, stablecoin growth"),
            ("Overlap with the chart trend", "0.82 correlation", "FA's price part mostly repeated the TA"),
            ("Planted-signal control", "found at t = 9", "the test works — the inputs simply had nothing"),
            ("Live trades with FA agreeing", "11 trades, −0.33R", "vs 3 with FA neutral at +0.55R (too few to lean on)"),
        ],
        "why": [
            "Its inputs didn't predict returns over a year of data, and the part that did move with price was the same "
            "trend the technical analysis already measured — so it added noise, not information.",
            "As a veto it blocked trades at random; as a conviction multiplier it amplified the TA it duplicated.",
        ],
        "kept": "FA is still computed, shown to Claude as context, recorded on every trade (fa_at_entry) and in scan_features. "
                "The positioning and usage inputs (open interest, long/short ratios, taker flow, TVL, fees) are still being "
                "recorded to be tested once there are weeks of data.",
        "removed": "The autotrader's FA veto (AUTOTRADER_FA_VETO), the scanner's FA conviction weight "
                   "(SCANNER_FA_CONVICTION_WEIGHT) and the position manager's FA check before averaging down.",
        "evidence": "python -m research.swing_lab (2026-09-27)",
    },
    {
        "key": "fade24",
        "score_inputs": {"result_r": -0.01, "result_note": "−0.01R a trade on its first 10 pairs; −30 bps a pair on the year's replay",
                         "t": None, "t_note": None, "n": 10, "n_note": "pairs (paper)", "lost_pct": 0.5},
        "title": "Fade the scanner (24h vs BTC)",
        "category": "Control",
        "active": "2026-10-24 → 2026-10-28",
        "retired": "2026-10-28",
        "hypothesis": "If the scanner's conviction is reliably wrong at 24 hours (IC −0.26 when it was built), betting AGAINST it — "
                      "short what it likes, long what it hates, hedged with BTC — should pay.",
        "method": "Paper pair book: every |conviction| ≥ 0.30 reading taken the opposite way for 24h against an equal BTC leg, "
                  "12% disaster stop. Judged like any book (40 trades), alongside a replay of the same rule on the year of data.",
        "numbers": [
            ("Live pairs", "10 closed, 50% won", "−0.01R a trade, −$17 net"),
            ("Year's replay", "−30 bps a pair", "the reversal that looked strong over 15 days didn't exist over 12 months"),
            ("Signal-rebuild lab", "IC −0.02 at 24h", "the scanner's conviction carries no information in either direction"),
        ],
        "why": [
            "It was a control, and the control answered: neither following the scanner nor fading it makes money. The 'IC −0.26' "
            "that motivated it was a two-week fluke; over a year the conviction is noise, so its opposite is noise too.",
        ],
        "kept": "The pair-ledger machinery (events, unlocks reuse it). Open pairs run to their 24 hours or the stop.",
        "removed": "Nothing deleted — experiments/fade24.py stays for the record; the book is off the Lab and the Books tab.",
        "evidence": "trading/book_switches.RETIRED_BOOKS, research/signal_rebuild_lab.py (2026-10-24)",
    },
    {
        "key": "hedge_straddle",
        "score_inputs": {"result_r": -0.13, "result_note": "least-bad end of −0.13 … −0.40R",
                         "t": 3.0, "t_note": "least-bad end of t −3 … −8", "n": None, "lost_pct": 0.0},
        "title": "Hedge straddle",
        "category": "Strategy",
        "active": "research only, 2026-09-28",
        "retired": "2026-09-28",
        "hypothesis": "Opening a long and a short at the same time and cutting whichever side loses lets the winner run, so "
                      "you profit from a move in either direction.",
        "method": "Backtest over one year of 40 perpetuals, compared with the equivalent breakout bracket (enter only once "
                  "price breaks out either way), costs included.",
        "numbers": [
            ("Hedge straddle", "−0.13 … −0.40R per trade", "t-stats −3 … −8 across settings"),
            ("Equivalent breakout bracket", "≈ 0R", "at every time of day"),
            ("After tight ranges", "worse", "false breakouts dominate exactly where a straddle is meant to shine"),
        ],
        "why": [
            "Mathematically the hedge is a breakout bracket plus two extra market fills: the losing leg's entry and exit. "
            "The breakout itself had no edge, so the straddle is that zero edge minus two sets of fees.",
        ],
        "kept": "Nothing live was ever built.",
        "removed": "research/straddle_lab.py (an earlier version is archived in docs/retired-code/).",
        "evidence": "research/straddle_lab.py (2026-09-28)",
    },
    {
        "key": "stale_exit",
        "score_inputs": {"result_r": 0.2, "result_note": "still +0.2R — retired for being far worse than trail (+1.21R), not for losing",
                         "t": None, "n": 78, "n_note": "replayed signals", "lost_pct": 0.0},
        "title": "\"Stale\" and legacy exits",
        "category": "Exit rule",
        "active": "until 2026-09-26",
        "retired": "2026-09-26",
        "hypothesis": "Cutting trades that haven't made progress by a deadline (and closing everything at a hard expiry) "
                      "frees capital and avoids slow losers.",
        "method": "Replayed 78 real signals through the production exit code under each policy, costs included.",
        "numbers": [
            ("Trail (now the default)", "+1.21R per trade", ""),
            ("Hold to stop or 2.5R", "+1.16R", ""),
            ("Legacy (stop to entry, hard expiry)", "+1.15R", ""),
            ("Anything with the stale cut", "≈ +0.2R", "the cut threw away most of the winners"),
        ],
        "why": [
            "Good trades often sit still before they move; the stale cut closed them just before they paid. My first "
            "prototype favoured it only because it was compared against a flat 24-hour expiry.",
        ],
        "kept": "Both still run as shadow policies on every trade (History → Which exit works best), so the comparison "
                "keeps updating on new trades.",
        "removed": "They are no longer the live exit rule.",
        "evidence": "trading/exits.py shadow book; replay 2026-09-26",
    },
    {
        "key": "left_watchlist_exit",
        "title": "Close when a coin leaves the watchlist",
        "category": "Exit rule",
        "active": "2026-09-25 → 2026-09-26",
        "retired": "2026-09-26",
        "hypothesis": "If a coin drops off the watchlist the setup is gone, so its position should be closed.",
        "method": "Watched the autotrader's first trades.",
        "numbers": [("Autotrader trades closed by this rule", "5 of the first 5", "none reached its stop or target")],
        "why": [
            "The watchlist is an attention filter (which coins to look at), not a thesis. Coins rotate out for reasons "
            "unrelated to the trade, so the rule closed every trade early, paying fees on each.",
        ],
        "kept": "Leaving the watchlist now tightens the stop once instead of closing.",
        "removed": "The close-on-leave exit.",
        "evidence": "Autotrader trade log, 2026-09-26",
        "dynamic": "left_watchlist",
    },
    {
        "key": "onchain_score",
        "score_inputs": {"result_r": None, "result_note": "not measurable on its own — removed as a built-in long bias",
                         "t": None, "n": 79, "n_note": "oracle signals, all BUY", "lost_pct": 0.0},
        "title": "On-chain score in the composite signal",
        "category": "Signal input",
        "active": "until 2026-09-27",
        "retired": "2026-09-27",
        "hypothesis": "On-chain activity (fees, mempool, network use) adds a fundamentals view to the composite score.",
        "method": "Audit of the oracle's output after noticing every signal was a long.",
        "numbers": [("Oracle signals", "79 BUY / 0 SELL", "78% of them on BTC")],
        "why": [
            "The on-chain inputs could only push the score up — busy networks read as bullish, quiet ones as neutral — so it "
            "was a built-in long bias rather than information. (Two other long-bias bugs were fixed at the same time: "
            "Fibonacci ties resolved to support, and a flat +0.2 for Bollinger squeezes.)",
        ],
        "kept": "Mirror tests (tests/test_symmetry.py) now check that a mirrored market gives the mirrored signal.",
        "removed": "The on-chain score and its collector.",
        "evidence": "Oracle signal history audit, 2026-09-27",
    },
    {
        "key": "timing_edges",
        "score_inputs": {"result_r": -0.24, "result_note": "≈1 bps edge minus ≥7 bps costs = −6 bps, converted at the scalper's 25 bps stop",
                         "t": None, "n": None, "lost_pct": 0.0},
        "title": "BTC lead-lag and time-of-day timing",
        "category": "Research",
        "active": "research only, 2026-09-27",
        "retired": "2026-09-27",
        "hypothesis": "Altcoins follow BTC's moves with a short delay, and some hours of the day trade better than others — "
                      "either could be timed.",
        "method": "research/scalper_lab.py --leadlag and --sessions on the recorded 1-second bars.",
        "numbers": [
            ("BTC → altcoin lead-lag", "real, ≈ 1 bps", "far below the ~7–11 bps it costs to trade"),
            ("Hour-of-day effect", "none", "no hour stood out once costs were included"),
        ],
        "why": ["Both effects were either absent or far too small to pay for a round trip."],
        "kept": "The recorded bars; the studies can be rerun as data accumulates.",
        "removed": "Nothing live was built.",
        "evidence": "python -m research.scalper_lab --leadlag / --sessions (2026-09-27)",
    },
]


# ── Failure score ───────────────────────────────────────────────────────────────
#
# 0–100, higher = worse, so experiments that failed in different ways can be ranked:
#   performance  up to 60 — average result per trade after costs, in R: 0R or better → 0, −0.5R or worse → 60
#   certainty    up to 40 — how conclusive the failure is: |t| ≥ 5 → 40; without a t-stat, sample size
#                           at a discount (n ≥ 300 → 24), since size alone doesn't prove the result
# Money lost isn't scored: every book traded paper or the Binance testnet, so it measures nothing real.
# Where a study reported a range, the least-bad end is used, so no score is inflated.

WEIGHTS = {"performance": 60, "certainty": 40}


def score(inp: dict) -> dict:
    r, t, n = inp.get("result_r"), inp.get("t"), inp.get("n")
    perf = WEIGHTS["performance"] * min(1.0, max(0.0, -r) / 0.5) if r is not None else 0.0
    if t is not None:
        cert = WEIGHTS["certainty"] * min(1.0, abs(t) / 5)
    elif n:
        cert = WEIGHTS["certainty"] * 0.6 * min(1.0, n / 300)
    else:
        cert = 0.0
    total = perf + cert
    return {"score": round(total), "performance": round(perf, 1), "certainty": round(cert, 1),
            "partial": r is None,
            "grade": "severe" if total >= 60 else "clear failure" if total >= 35 else "mild" if total >= 15 else "harmless"}


NO_RECORD_INPUTS = {"result_r": None, "result_note": "no trade record on this instance", "t": None,
                    "t_note": "nothing to test", "n": 0, "n_note": "closed trades found here", "lost_pct": 0.0}


def _trade_inputs(rs: list[float], pnl: float, capital: float, note: str) -> dict:
    n = len(rs)
    m = sum(rs) / n if n else None
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (n - 1)) if n > 1 else 0
    return {"result_r": round(m, 3) if m is not None else None, "result_note": note,
            "t": round(m / (sd / math.sqrt(n)), 2) if sd and n >= 10 else None, "n": n, "n_note": "trades",
            "lost_pct": round(max(0.0, -pnl) / capital * 100, 2) if capital else 0.0}


def _left_watchlist_inputs() -> dict | None:
    import json
    from storage.db import SWING_DB, connect
    try:
        con = connect(SWING_DB)
        try:
            trades = [json.loads(r[0]) for r in con.execute("SELECT data FROM trades WHERE label='auto' AND status='closed'").fetchall()]
        finally:
            con.close()
    except Exception:
        return None
    hit = [t for t in trades if t.get("close_reason") == "left_watchlist"]
    rs = [t["pnl_usd"] / t["risk_usd"] for t in hit if t.get("risk_usd")]
    return _trade_inputs(rs, sum(t["pnl_usd"] for t in hit), 10_000, "average R of the trades it closed") if rs else None


# ── The order-flow scalper, from its own records ───────────────────────────────

_EVIDENCE = {
    # (trades DB, starting balance, which signals rows, forward horizons in seconds, round-trip cost in bps)
    "scalper": (lambda: SC.TRADES_DB, SC.INITIAL_BALANCE, "move_bps IS NULL", (5, 15, 30, 60, 120), 7.0),     # maker in, taker out
    "impulse": (lambda: SC.IMPULSE_TRADES_DB, SC.IMPULSE_BALANCE, "move_bps IS NOT NULL", (5, 30, 60, 300, 600), 10.0),  # taker both ways
}


def _scalper_evidence(which: str = "scalper") -> dict | None:
    """A retired scalper's record, computed from its database: trades, costs, exits and the signal's
    own forward moves."""
    path, initial, where, hs, round_trip = _EVIDENCE[which]
    try:
        con = sqlite3.connect(f"file:{path()}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        trades = [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY exit_ts")]
        cols = ", ".join(f"fwd_{h}" for h in hs)
        sig = [dict(r) for r in con.execute(f"SELECT side, decision, {cols} FROM signals WHERE {where}")]
    except sqlite3.Error:
        return None
    if not trades:
        return None

    def book(rows: list[dict]) -> dict:
        n = len(rows)
        notional = sum(r["notional"] or 0 for r in rows) or 1
        gross, fees, net = (sum(r[k] or 0 for r in rows) for k in ("gross_pnl", "fees", "net_pnl"))
        wins = sum(1 for r in rows if (r["net_pnl"] or 0) > 0)
        return {"trades": n, "win_rate": round(wins / n * 100, 1) if n else None, "gross": round(gross, 2),
                "fees": round(fees, 2), "net": round(net, 2),
                "gross_bps": round(gross / notional * 1e4, 2), "cost_bps": round((fees + sum(r["funding"] or 0 for r in rows)) / notional * 1e4, 2),
                "net_bps": round(net / notional * 1e4, 2),
                "avg_mfe_bps": round(sum(r["mfe_bps"] or 0 for r in rows) / n, 1) if n else None,
                "avg_mae_bps": round(sum(r["mae_bps"] or 0 for r in rows) / n, 1) if n else None,
                "won_before_costs": sum(1 for r in rows if (r["gross_pnl"] or 0) > 0 >= (r["net_pnl"] or 0))}

    paper = [t for t in trades if (t.get("mode") or "paper") == "paper"]
    live = [t for t in trades if t.get("mode") == "live"]
    exits: dict[str, list[dict]] = {}
    for t in paper:
        exits.setdefault((t["exit_reason"] or "?").split(" (")[0], []).append(t)
    by_exit = sorted(({"exit": k, "trades": len(v), "net": round(sum(x["net_pnl"] for x in v), 2),
                       "win_rate": round(sum(1 for x in v if x["net_pnl"] > 0) / len(v) * 100, 1)} for k, v in exits.items()),
                     key=lambda g: g["net"])
    curve, cum = [], 0.0
    for t in paper:
        cum += t["net_pnl"] or 0
        curve.append({"ts": t["exit_ts"], "pnl": round(cum, 2)})
    step = max(1, len(curve) // 300)
    curve = curve[::step] + ([curve[-1]] if curve and curve[-1] is not curve[::step][-1] else [])

    # The signal itself: forward mid-price moves after each logged candidate, in the signal's direction.
    horizons = []
    for h in hs:
        for label, rows in (("entered", [s for s in sig if s["decision"] == "entered"]), ("all candidates", sig)):
            xs = [s[f"fwd_{h}"] for s in rows if s[f"fwd_{h}"] is not None]
            if len(xs) < 20:
                continue
            m = sum(xs) / len(xs)
            sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
            horizons.append({"horizon_s": h, "set": label, "n": len(xs), "avg_bps": round(m, 2),
                             "t": round(m / (sd / math.sqrt(len(xs))), 1) if sd else None,
                             "share_up_pct": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1)})
    rs = [t["r"] for t in paper if t.get("r") is not None]
    score_inputs = _trade_inputs(rs, sum(t["net_pnl"] or 0 for t in paper), initial,
                                 "average R per paper trade after costs")
    return {"paper": book(paper), "live": book(live) if live else None, "by_exit": by_exit, "curve": curve,
            "score_inputs": score_inputs,
            "signal_forward": horizons, "signals_logged": len(sig), "round_trip_bps": round_trip,
            "period": [paper[0]["entry_ts"], paper[-1]["exit_ts"]] if paper else None}


def summary() -> dict:
    out = []
    for e in RETIRED:
        item = {k: v for k, v in e.items() if k != "dynamic"}
        if e.get("dynamic") in ("scalper", "impulse"):
            ev = _scalper_evidence(e["dynamic"])
            item["data"] = ev
            if ev and e["dynamic"] == "impulse":
                p = ev["paper"]
                item["numbers"] = [
                    ("Paper trades", f"{p['trades']}, {p['win_rate']}% won", f"net {p['net']:+,.2f} USDT"),
                    ("Before costs (gross)", f"{p['gross']:+,.2f} USDT", f"{p['gross_bps']:+.2f} bps per trade — the calls themselves lost"),
                    ("Costs", f"{p['fees']:,.2f} USDT", f"{p['cost_bps']:.2f} bps per trade"),
                    ("Extreme moves logged", f"{ev['signals_logged']}", "every one with its forward moves, traded or not"),
                    ("Right on price, lost to fees", f"{p['won_before_costs']} trades", ""),
                ]
            elif ev:
                p = ev["paper"]
                item["numbers"] = [
                    ("Paper trades", f"{p['trades']}, {p['win_rate']}% won", f"net {p['net']:+,.2f} USDT"),
                    ("Before costs (gross)", f"{p['gross']:+,.2f} USDT", f"{p['gross_bps']:+.2f} bps per trade — the calls themselves lost"),
                    ("Costs", f"{p['fees']:,.2f} USDT", f"{p['cost_bps']:.2f} bps per trade"),
                    ("Best move reached, on average", f"{p['avg_mfe_bps']} bps", "vs a 40 bps target and 25 bps stop"),
                    ("Right on price, lost to fees", f"{p['won_before_costs']} trades", ""),
                ] + ([("Live on testnet", f"{ev['live']['trades']} trades", f"net {ev['live']['net']:+,.2f} USDT — too few to judge")]
                     if ev.get("live") else [])
        if e.get("dynamic") == "left_watchlist":
            item["score_inputs"] = _left_watchlist_inputs() or {"result_r": None, "n": 5, "lost_pct": 0.0}
        elif e.get("dynamic") in ("scalper", "impulse"):
            # A fresh instance (production starts with empty data) has no scalper record: the entry
            # still needs score inputs, or the page has nothing to render for it.
            item["score_inputs"] = item["data"]["score_inputs"] if item.get("data") else NO_RECORD_INPUTS
        item["score"] = score(item.get("score_inputs") or {})
        item["numbers"] = [{"label": a, "value": b, "note": c} for a, b, c in item.get("numbers", [])]
        out.append(item)
    out.sort(key=lambda x: -x["score"]["score"])
    for i, item in enumerate(out, 1):
        item["rank"] = i
    # Studies the research shelf rejected live here too, so the Lab shows only what is still open or in use.
    from experiments import research_index
    research = [e for e in research_index.shelf() if e["status"] == "rejected"]
    return {"retired": out, "research": research, "weights": WEIGHTS, "updated_at": time.time()}
