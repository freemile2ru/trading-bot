"""Every trading book in one list, with an on/off switch each (the Books tab).

"Off" means NO NEW ENTRIES. Open positions keep being managed — stops, targets, trailing and
reconciliation — until they close; close them from the Trades tab if you want out now.

Where each switch acts:
  main           manual entries: the Trade tab's order form and the Signal tab's "execute"
  auto           the autotrader's own pause (also cancels its resting limit orders)
  auto_rules     the rules-only twin's entries
  signals_*      the signal books' entries
  claude_swing   Claude swing's reviews — no reviews, so no Claude calls and no new trades
  hold72         Hold-72's new pairs
Switch state lives in state.db, so it survives restarts. Live mode (real orders on the Binance
account) is separate and set on the Live tab; the switch here covers a book in either mode.
"""
from __future__ import annotations

import time

from storage import state as kv

BOOKS = [
    {"name": "main", "label": "Main wallet", "kind": "manual",
     "description": "Your own trades — placed from the Trade tab or by executing a Signal-tab analysis.",
     "off_means": "Order form and execute button refuse new trades."},
    {"name": "auto", "label": "Autotrader", "kind": "automated",
     "description": "Rules pick the moment on the watchlist; Claude confirms before any entry (market or limit).",
     "off_means": "Paused — no new entries, resting limit orders cancelled."},
    {"name": "auto_rules", "label": "Autotrader · no Claude", "kind": "experiment",
     "description": "The autotrader's exact rules on the same scans, entering wherever it would have asked Claude.",
     "off_means": "The twin stops entering; the with/without-Claude comparison pauses."},
    {"name": "signals_claude", "label": "Signals · Claude", "kind": "experiment",
     "description": "Takes every scanner signal Claude fires, at market with Claude's levels.",
     "off_means": "Fired signals are no longer taken."},
    {"name": "signals_rules", "label": "Signals · rules only", "kind": "experiment",
     "description": "Takes every scanner candidate sent to Claude, whatever Claude says, with rule-based levels.",
     "off_means": "Candidates are no longer taken."},
    {"name": "claude_swing", "label": "Claude swing", "kind": "experiment",
     "description": "Claude picks and manages its own swing trades in a review every few hours.",
     "off_means": "No reviews (no Claude calls); open trades keep their stops and targets."},
    {"name": "carry", "label": "Carry · BTC, ETH & gold", "kind": "experiment",
     "description": "Cash-and-carry: owns BTC, ETH and tokenized gold (PAXG) and shorts the same amount of their perpetuals, "
                    "collecting funding with the price risk cancelled. BTC/ETH passed an untouched holdout (≈ +2–4%/yr); "
                    "gold's perp paid ~7–13%/yr since listing (added 2026-10-27).",
     "off_means": "Both legs are unwound (a carry trade has no natural exit) and nothing is reopened."},
    *[{"name": f"{m}_{k}", "label": f"{ml} · {kl}", "kind": "experiment",
       "description": {"trend": f"{ml}: 6-month trend following on Binance's {m} perpetuals — long while trending up, flat otherwise.",
                       "rules": f"{ml}: our scanner + autotrader entry rules on Binance's {m} perpetuals, no Claude.",
                       "claude": f"{ml}: switched off 2026-10-19 — in 10 days and ~220 Claude calls it never traded: Claude's "
                                 f"confidence on these markets topped out at 52–57% against a 65% bar. Turn it back on only "
                                 f"with a lower bar (it still costs ~10 calls a day)."}[k],
       "off_means": "No new entries; open positions keep their stops (the trend book's close on a flip)."}
      for m, ml in (("stocks", "Stocks"), ("futures", "Futures"))
      for k, kl in (("trend", "Trend"), ("rules", "Scanner rules"), ("claude", "Claude-confirmed"))],
    {"name": "weekly_rs", "label": "Weekly near-high (long/short)", "kind": "experiment",
     "description": "Every Monday: long the 5 coins nearest their 7-day high, short the 5 furthest below, held a week "
                    "(roughly market-neutral). On a year of data: +1.36% a week after costs, both halves positive — "
                    "but luck reached +1.02%, so it's a forward test.",
     "off_means": "No more rebalances; open positions keep their 30% disaster stops until closed."},
    {"name": "copy_stocks", "label": "Copy trading · stock perps", "kind": "experiment",
     "description": "The same lead traders as the crypto copy book, but only their positions in Binance's stock perpetuals (TSLA, NVDA, "
                    "SK Hynix, Meta…), copied under the same too-late rule and exits, into their own wallet. Skips names inside an earnings blackout.",
     "off_means": "No new copies of stock positions; open ones follow their leader out as usual."},
    {"name": "copy_futures", "label": "Copy trading · commodity perps", "kind": "experiment",
     "description": "The same lead traders' positions in Binance's commodity perpetuals (gold, silver, platinum, copper, oil, gas), "
                    "copied under the same too-late rule and exits, into their own wallet.",
     "off_means": "No new copies of commodity positions; open ones follow their leader out as usual."},
    # Copy-trading variants (2026-10-30): three other readings of the same leaders' board, one wallet per market each,
    # judged separately — see experiments/copy_trading.VARIANTS.
    *[{"name": f"copy_{m}_{v}", "label": f"Copy · {ml} · {vl}", "kind": "experiment",
       "description": f"The {ml} copy book under the '{vl}' rule: " + desc,
       "off_means": "No new copies under this rule; open ones follow their leader out as usual."}
      for m, ml in (("crypto", "crypto"), ("stocks", "stock perps"), ("futures", "commodity perps"))
      for v, vl, desc in (("wide", "more leaders", "the same bar but 15 leaders a venue instead of 5, so more positions are seen opening."),
                          ("fast", "faster traders", "5 leaders a venue chosen among those whose median hold is a day or less, so they open more often."),
                          ("late", "late entries, half size", "the base leaders, but a position up to their whole median hold old is still copied at half size "
                                                              "when it has moved no more than a quarter of their typical win."))],
    {"name": "auction", "label": "Auction levels (mentor's NQ system, stock & index perps)", "kind": "experiment",
     "description": "A mentor's 5-minute NQ system, ported (trading/auction_levels.py): accept beyond a session level (prior day, overnight, "
                    "last week, opening range, initial balance), confirm on the next close, enter at the next open, stop under the acceptance "
                    "bar, target the next level — graded by the 15m/30m bias, momentum, volume and ATR. Runs the 'target50+grade7' variant "
                    "(target ≥ 50 bps away, grade ≥ 7) with calendar gates (no trade near a macro release or inside an earnings window) on "
                    "QQQ, NVDA, TSLA and AMD perps. The replay was NEGATIVE at our costs (−0.13% a trade); this is a measuring-size forward test.",
     "off_means": "No new setups; open ones run to their target, stop, a close back through the level or the session's end."},
    {"name": "multi_factor", "label": "Multi-factor basket (momentum + trend + funding)", "kind": "experiment",
     "description": "Daily: 30-day cross-sectional momentum, 10-day trend and funding carry held together, each scaled by its own "
                    "volatility (risk parity). On 4 years of 40 perps the combination's test Sharpe was 1.25 with a 26% drawdown — "
                    "but its 95% range touched zero, so it runs as a $1k forward test, not a promoted book.",
     "off_means": "No more rebalances; open positions keep their 30% disaster stops until closed."},
    {"name": "carry_q", "label": "Carry Q · quarterly basis", "kind": "experiment",
     "description": "Cash-and-carry against the quarterly future instead of the perpetual: owns BTC and ETH, shorts the "
                    "current-quarter contract, rolls a week before delivery. The return is locked at entry (the basis) "
                    "instead of floating with funding — and right now the quarterly pays more than funding is expected to.",
     "off_means": "Both legs are unwound (no natural exit) and nothing is reopened."},
    {"name": "cascade", "label": "Cascade fade (+1h, 4h)", "kind": "experiment",
     "description": "An hour after a liquidation cascade on one of the recorder's 12 coins (a 5-minute burst at its 99th percentile "
                    "over 30 days), takes the other side for 4 hours with an 8% disaster stop. Since 2026-10-30 — the first rule "
                    "(fade at once, 3% stop) lost in both halves of the recorder's replay; the delayed entry made +52 bps a trade "
                    "there on a flat first half, so this is a fresh forward test under the 40-trade verdict.",
     "off_means": "No new fades (queued ones are dropped); open ones run to their 4 hours or the 8% stop."},
    {"name": "events", "label": "Event shorts (listings & delistings)", "kind": "experiment",
     "description": "Shorts the coin Binance announces — a spot listing (where its perp already trades), a new perp's "
                    "first week, or a delisting — hedged with BTC, for 30 days (a delisting until its delist date). On ~200 "
                    "announcements since 2023 these coins fell 29–46% against BTC over the month, 68–86% of the time, in both halves. "
                    "Shorts pay funding (2.6–3.7% an event on average), so a position is killed once a settlement costs 0.5%.",
     "off_means": "No new event positions; open ones run to their deadline, the pair stop or the funding kill."},
    {"name": "copy_trading", "label": "Copy trading (Binance & Hyperliquid leaders)", "kind": "experiment",
     "description": "Mirrors consistent lead traders from Binance's copy-trading board and Hyperliquid's on-chain leaderboard, on paper — "
                    "chosen daily for drawdown, share of positive weeks, copier profit and tenure rather than headline ROI. Copies a position only while it is young and hasn't already "
                    "run (judged against that leader's own typical hold and winning move); exits when the leader exits, an 8% stop, "
                    "or three times their typical hold (a day at least, a week at most). Skipped entries are tracked to judge the too-late rule.",
     "off_means": "No new copies; open positions still follow their leader out, or hit the stop."},
    {"name": "unlocks", "label": "Unlock shorts (cliff unlocks vs BTC)", "kind": "experiment",
     "description": "Shorts a coin into a large cliff unlock (≥1% of supply, insider-heavy or ≥5%), hedged with BTC, from "
                    "14 days before until the unlock. Keyrock's 16,000-event study: ~90% of unlocks weigh on price and the "
                    "slide happens before the date (median −9% in the final two weeks, team/investor cliffs ≈ −25%). "
                    "No replay of our own — a forward test under the 40-trade verdict.",
     "off_means": "No new unlock shorts; open ones run to their unlock, the pair stop or the funding kill."},
    {"name": "vol_premium", "label": "Vol premium (weekly BTC/ETH condors)", "kind": "experiment",
     "description": "Sells a weekly iron condor on BTC and ETH (short the ±1σ strangle, long the ±2σ wings) off Deribit's "
                    "public quotes, sized so the worst case is 5% of equity, held to settlement. Two years of our data: "
                    "BTC implied vol ran 3 points above realised on 69% of days; the condor made ≈ +1.8% of notional a month. "
                    "The premium thinned in the second year, hence a paper test.",
     "off_means": "No new structures; open ones run to their expiry (the loss is capped by the wings)."},
    {"name": "fade24", "label": "Fade the scanner (24h vs BTC)", "kind": "experiment",
     "description": "Trades AGAINST strong scanner conviction (|conviction| ≥ 0.30) for 24h, hedged with an opposite "
                    "BTC position. Since 2026-10-24: conviction has pointed the wrong way for weeks (IC −0.26 at 24h); "
                    "fading it made ~+127 bps a pair over the last 15 days — but lost ~30 bps over the year's replay.",
     "off_means": "No new pairs; open pairs run to their 24 hours or the disaster stop."},
    {"name": "hold72", "label": "Hold vs BTC (24h)", "kind": "experiment",
     "description": "Follows high-conviction scanner signals (|conviction| ≥ 0.45), each hedged with an equal, opposite "
                    "BTC position. Since 2026-10-23: held at most 24h (was 72h), and closed early once the coin's "
                    "conviction fades below 0.25 — on our live pairs that cut the average loss from −7.9% to −2.3%.",
     "off_means": "No new pairs; open pairs run to their 24 hours, the watcher or the disaster stop."},
]
# Books switched off for good: kept out of the switch list (and always off), their records kept.
RETIRED_BOOKS = {
    "copy_futures": "Retired 2026-11-03 — zero copies in its week at $21.8k: every commodity-perp position the leaders held pre-dated "
                    "tracking and they open new ones too rarely for a verdict. Its capital went to the crypto and stock copy books; "
                    "a leader's commodity position is now recorded as a skip.",
    "claude_swing": "Retired 2026-11-02 — 12 trades in 24 days, −0.16R a trade, −$198 (−40%); nobody was reading its reviews and the "
                    "signal-rebuild lab had already shown the scanner it reads carries no information. Its $500 is parked in the main wallet.",
    **{f"copy_{m}_{v}": f"Folded 2026-11-02 into the {ml} copy book — the rule still runs there, tagged on each copy, judged by its own "
                        f"record; a $611 wallet with {n} trades in three days could not reach a verdict for months."
       for m, ml, n in (("crypto", "crypto", "0–15"), ("stocks", "stock-perp", "0–2"), ("futures", "commodity-perp", "0")) for v in ("wide", "fast", "late")},
    "stocks_trend": "Retired 2026-10-30 — failed its pre-set 40-trade verdict: −0.23R a trade (90% range −0.37…−0.10), 42% won, "
                    "−$202 net; stops and trend flips (−$269) cost more than every winner made. Its 14 open positions were closed "
                    "and its capital parked in the main wallet.",
    "fade24": "Retired 2026-10-28 — it was a control (does betting AGAINST the scanner work?) and the answer is no: "
              "−0.01R a trade on its first 10, −30 bps a pair on the year's replay. With the signal-rebuild lab showing the "
              "scanner's conviction carries no information either way, there is nothing left for it to test.",
    "auto_rules": "Retired 2026-10-17 — the autotrader's rules without Claude failed its pre-set verdict: 118 trades, "
                  "−0.26R a trade, 90% range entirely below zero.",
    "signals_rules": "Retired 2026-10-21 — failed its pre-set verdict: 88 trades, −0.31R a trade (range −0.54…−0.07).",
    "signals_claude": "Retired 2026-10-25 — still losing (−0.14R over 74 trades) and following the same scanner direction "
                      "every failed book followed; Fade-24 now tests the opposite side.",
    "hold72": "Retired 2026-10-24 — failed its pre-set verdict: 45 pairs, −0.28R a pair (range −0.47…−0.11), −20.7%.",
    "stocks_rules": "Retired 2026-10-12 — our scanner rules on stock perps lost as the research predicted (−0.32R).",
    "futures_rules": "Retired 2026-10-12 — our scanner rules on commodity perps lost (−0.83R over 6 trades).",
    "stocks_claude": "Retired 2026-10-19 — never traded in 10 days: Claude's confidence topped out at 52–57% vs a 65% bar.",
    "futures_claude": "Retired 2026-10-19 — never traded in 10 days: Claude's confidence topped out at 22–45%.",
}
RETIRED_BOOKS_ENFORCED = True                    # tests of the retired books' code turn this off
_RETIRED_DEFS = {b["name"]: b for b in BOOKS if b["name"] in RETIRED_BOOKS}
BOOKS = [b for b in BOOKS if b["name"] not in RETIRED_BOOKS]
NAMES = {b["name"] for b in BOOKS}
RETIRED = [*[{"name": n, "label": _RETIRED_DEFS[n]["label"], "kind": "retired", "description": why}
             for n, why in RETIRED_BOOKS.items() if n in _RETIRED_DEFS], {"name": "impulse", "label": "Impulse scalper", "kind": "retired",
            "description": "Retired 2026-10-06 — the extreme moves it rode didn't continue (traded events: within noise "
                           "at every horizon) and costs of ~10 bps a trade came on top. Its 1-second bar recorder was kept."},
           {"name": "scalper", "label": "Order-flow scalper", "kind": "retired",
            "description": "Retired 2026-09-29 — its edge (+1–2 bps) never covered its costs (7–11 bps). See the Retired tab."}]


def _key(name: str) -> str:
    return kv.key("book", "off", name)


def enabled(name: str) -> bool:
    """Whether a book may open new trades. Cheap and synchronous — called on every entry path."""
    if name in RETIRED_BOOKS and RETIRED_BOOKS_ENFORCED:
        return False
    if name == "auto":
        from autotrader.engine import PAUSED_KEY
        return not kv.get_sync(PAUSED_KEY)
    return kv.get_sync(_key(name)) is None


def unavailable(name: str) -> str | None:
    """Why a book can't be switched at all (turned off in .env), or None."""
    if name in ("auto_rules", "signals_claude", "signals_rules", "claude_swing", "hold72", "carry", "fade24", "weekly_rs", "events", "cascade", "carry_q", "unlocks", "vol_premium", "copy_trading", "copy_stocks", "copy_futures", "multi_factor", "auction") or name.startswith("copy_"):
        from experiments import books
        if not books.ENABLED:
            return "EXPERIMENTS_ENABLED=false in .env"
    if name == "auto":
        from autotrader import engine
        if not engine.ENABLED:
            return "AUTOTRADER_ENABLED=false in .env"
    if name.startswith(("stocks_", "futures_")):
        from markets import engine as markets
        if not markets.ENABLED:
            return "MARKETS_ENABLED=false in .env"
    return None


async def set_enabled(name: str, on: bool) -> None:
    if name not in NAMES and not (name in RETIRED_BOOKS and not RETIRED_BOOKS_ENFORCED):
        raise KeyError(name)
    why = unavailable(name)
    if why:
        raise PermissionError(why)
    if name == "auto":
        from autotrader import engine
        await engine.set_paused(not on)
        return
    if on:
        kv.delete_sync(_key(name))
    else:
        kv.set_sync(_key(name), {"at": time.time()})


def _live_mode(name: str) -> str | None:
    if name not in ("main", "auto"):
        return None
    from live import control
    return "live" if control.is_live(name) else "paper"


def _wallets(name: str) -> list[tuple[str, object]]:
    """A book's wallets: (system key, wallet). Paper books each own one simulated wallet; main and
    auto also have a live ledger mirroring the Binance account."""
    if name in ("main", "auto"):
        from live import swing as live_swing
        return [(name, live_swing.paper(name)), (f"{name}_live", live_swing.LIVE[name])]
    if name in ("hold72", "carry", "fade24", "events", "carry_q", "unlocks", "vol_premium"):
        return [(name, None)]
    if name.startswith(("stocks_", "futures_")):
        from markets import engine as markets
        return [(name, markets.WALLETS[name])]
    from experiments import books
    return [(name, books.WALLETS[name])]


def _wallet_view(system: str, w) -> dict:
    live = system.endswith("_live")
    if w is None:                                          # hold72 / carry keep their own ledgers
        from experiments import carry, carry_q, events, fade24, hold72, unlocks, vol_premium
        book = {"hold72": hold72, "carry": carry, "fade24": fade24, "events": events, "carry_q": carry_q,
                "unlocks": unlocks, "vol_premium": vol_premium}[system]
        from trading import capital
        bal, eq, init = book.balance(), book.equity(), capital.initial(system, book.INITIAL_BALANCE)   # programme deposits count as capital
        return {"system": system, "ledger": "paper", "label": "Paper wallet", "balance": round(bal, 2),
                "equity": round(eq, 2), "initial": init, "return_pct": round((eq / init - 1) * 100, 2) if init else None}
    p = w.get_portfolio(closed_limit=1, history_limit=1)
    bal, eq, init = p["balance"], p.get("total_equity") or p["balance"], p["initial_balance"]
    return {"system": system, "ledger": "live" if live else "paper",
            "label": "Live ledger (Binance account)" if live else "Paper wallet",
            "balance": round(bal, 2), "equity": round(eq, 2), "initial": round(init, 2),
            # A live ledger re-syncs to the Binance balance (deposits, other activity), so a return
            # against its "initial" balance means nothing — its trades' P&L is the record.
            "return_pct": None if live else (round((eq / init - 1) * 100, 2) if init else None)}


def _empty() -> dict:
    return {"open": 0, "closed": 0, "wins": 0, "net_pnl": 0.0, "trades_24h": 0, "trades_7d": 0,
            "last_opened": None, "live_trades": 0}


def catalog(ledger: str = "both") -> dict:
    """Every book with its switch, its wallets and a short record — `ledger` (both/paper/live)
    picks which wallets the record counts. Reads the database: call from a thread."""
    from trading import overview, verdicts
    from trading.history import in_ledger
    rows, _ = overview.all_rows()
    judged = verdicts.for_books([r for r in rows if in_ledger(r["system"], ledger)])
    now, day = time.time(), 86400
    stats: dict[str, dict] = {}
    for r in rows:
        if not in_ledger(r["system"], ledger):
            continue
        book = r["system"].removesuffix("_live")          # a live ledger belongs to its book
        s = stats.setdefault(book, _empty())
        if r.get("status") == "open":
            s["open"] += 1
        else:
            s["closed"] += 1
            s["net_pnl"] += r.get("net_pnl") or 0.0
            s["wins"] += (r.get("net_pnl") or 0) > 0
        o = r.get("opened_ts")
        if o:
            s["last_opened"] = max(s["last_opened"] or 0, o)
            s["trades_24h"] += now - o <= day
            s["trades_7d"] += now - o <= 7 * day
        if r["system"].endswith("_live"):
            s["live_trades"] += 1
    out = []
    for b in BOOKS:
        s = stats.get(b["name"], _empty())
        s["net_pnl"] = round(s["net_pnl"], 2)
        s["win_rate"] = round(s["wins"] / s["closed"] * 100, 1) if s["closed"] else None
        why = unavailable(b["name"])
        off = kv.get_sync(_key(b["name"])) or {}
        wallets = []
        for system, w in _wallets(b["name"]):
            try:
                wallets.append(_wallet_view(system, w))
            except Exception:
                continue
        out.append({**b, "enabled": False if why else enabled(b["name"]), "unavailable": why,
                    "mode": _live_mode(b["name"]), "switched_at": off.get("at"), "wallets": wallets,
                    "has_live": any(x["ledger"] == "live" for x in wallets), **s,
                    "verdict": None if b["name"] in verdicts.NO_VERDICT else judged.get(b["name"]) or verdicts.judge([])})
    retired = []
    for b in RETIRED:
        s = stats.get(b["name"], {})
        retired.append({**b, "closed": s.get("closed", 0), "net_pnl": round(s.get("net_pnl", 0.0), 2)})
    return {"books": out, "retired": retired, "ledger": ledger, "updated_at": now}
