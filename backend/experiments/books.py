"""Paper books for the side experiments — what does Claude add, and can it trade on its own?

  auto_rules      the autotrader's exact rules on the same scans, entering wherever it would
                  have asked Claude — paired with the real autotrader ("auto")
  signals_claude  every scanner signal Claude fires, taken at market with Claude's levels
  signals_rules   every scanner candidate the rules send to Claude, taken whatever Claude says,
                  with the autotrader's rule-based levels — paired with signals_claude
  claude_swing    Claude picking its own swing trades every few hours (experiments/claude_swing.py)

All are paper only, live in swing_trading.db under their own labels, use the same default exit
policy as the autotrader, and never block other systems' coins (they aren't real exposure).
"""
import logging
import os
import time

from storage import state as kv
from storage.db import SWING_DB
from trading.paper_trader import PaperWallet

ENABLED = os.getenv("EXPERIMENTS_ENABLED", "true").lower() != "false"
RISK_PCT = float(os.getenv("EXPERIMENT_RISK_PCT", "3.0"))        # signals books: % of equity at the stop (3 since 2026-10-29)

LABELS = {
    "auto_rules": "Autotrader · no Claude",
    "signals_claude": "Signals · Claude",
    "signals_rules": "Signals · rules only",
    "claude_swing": "Claude swing",
    "weekly_rs": "Weekly near-high (long/short)",
    "cascade": "Cascade fade (+1h, 4h)",
    "copy_trading": "Copy trading (Binance & Hyperliquid leaders)",
    "copy_stocks": "Copy trading · stock perps",
    "copy_futures": "Copy trading · commodity perps",
    "multi_factor": "Multi-factor basket (momentum + trend + funding)",
    "auction": "Auction levels (mentor's NQ system, stock & index perps)",
    "burst_up": "Burst · ride the up-burst (+15m, 4h)",
    "burst_dip": "Burst · buy the dip (+60m, 4h)",
    "prop": "Prop · Propr Classic 1-Step $25K",
    # Copy-trading variants (2026-10-30): the same leaders' board read three other ways, one wallet per market each.
    **{f"copy_{m}_{v}": f"Copy · {ml} · {vl}" for m, ml in (("crypto", "crypto"), ("stocks", "stock perps"), ("futures", "commodity perps"))
       for v, vl in (("wide", "more leaders"), ("fast", "faster traders"), ("late", "late entries, half size"))},
}
# Each experiment and what it's compared with (None: judged on its own against the criterion).
PAIRS = {"auto_rules": "auto", "signals_rules": "signals_claude", "signals_claude": None, "claude_swing": None, "weekly_rs": None, "cascade": None,
         "copy_trading": None, "copy_stocks": None, "copy_futures": None, "multi_factor": None, "auction": None, "burst_up": None, "burst_dip": None, "prop": None,
         **{f"copy_{m}_{v}": None for m in ("crypto", "stocks", "futures") for v in ("wide", "fast", "late")}}

WALLETS: dict[str, PaperWallet] = {
    name: PaperWallet(SWING_DB, label=name, history_cap=3000, min_snapshot_seconds=120) for name in LABELS
}


def started_at(name: str) -> float:
    """When an experiment began (first seen) — the paired comparison only counts trades after it."""
    k = kv.key("experiment", "started", name)
    v = kv.get_sync(k)
    if v is None:
        v = time.time()
        kv.set_sync(k, v)
    return float(v)


async def monitor_all() -> None:
    """Fills, stops and targets for the experiment books (auto_rules runs with the autotrader)."""
    from trading import monitor
    if not ENABLED:
        return
    for name in ("weekly_rs", "cascade", "copy_trading", "copy_stocks", "multi_factor", "auction", "burst_up", "burst_dip", "prop"):
        try:
            await monitor.run(WALLETS[name])
        except Exception as e:
            logging.getLogger("experiments").warning("Monitoring %s failed: %s", name, e)
    try:
        from experiments import weekly_rs
        await weekly_rs.rebalance()                 # a cheap check; acts once each Monday
    except Exception as e:
        logging.getLogger("experiments").warning("Weekly RS rebalance failed: %s", e)
    try:
        from experiments import auction
        await auction.tick()                        # every minute; acts on each closed 5-minute bar in the New York session
    except Exception as e:
        logging.getLogger("experiments").warning("Auction levels failed: %s", e)
    try:
        from experiments import multi_factor
        await multi_factor.rebalance()              # a cheap check; acts once a day after 00:10 UTC
    except Exception as e:
        logging.getLogger("experiments").warning("Multi-factor rebalance failed: %s", e)
    try:
        from experiments import hold72
        await hold72.monitor_pairs()
    except Exception as e:
        logging.getLogger("experiments").warning("Monitoring hold72 failed: %s", e)
    try:
        from experiments import cascade
        await cascade.tick()
    except Exception as e:
        logging.getLogger("experiments").warning("Cascade fade failed: %s", e)
    try:
        from experiments import burst
        await burst.tick()                          # every minute: the last closed minute of each coin in the universe
    except Exception as e:
        logging.getLogger("experiments").warning("Burst book failed: %s", e)
    try:
        from experiments import prop
        await prop.tick()                           # every minute: the prop account against Propr's floors (entries come from the lanes)
    except Exception as e:
        logging.getLogger("experiments").warning("Prop book failed: %s", e)
    try:
        from experiments import control
        await control.tick()                        # every 15 minutes: the controls for the newest closed trades of each book
    except Exception as e:
        logging.getLogger("experiments").warning("Control data failed: %s", e)
    try:
        from experiments import index_shadow
        await index_shadow.tick()                   # every 15 minutes: the reversal and cascade signals scored on the alt index
    except Exception as e:
        logging.getLogger("experiments").warning("Index shadow failed: %s", e)
    try:
        from experiments import stock_flow
        await stock_flow.tick()                     # every 4h mark: US stock perps' positioning recorded, two scores judged forward
    except Exception as e:
        logging.getLogger("experiments").warning("Stock flow failed: %s", e)
    try:
        from experiments import entry_cells
        await entry_cells.tick()                    # every 15 minutes: the conditions at each entry (long gate, short cells, the reversal long leg)
    except Exception as e:
        logging.getLogger("experiments").warning("Entry cells failed: %s", e)
    try:
        from experiments import events
        await events.tick()
    except Exception as e:
        logging.getLogger("experiments").warning("Event shorts failed: %s", e)
    try:
        from experiments import copy_trading
        await copy_trading.tick()
    except Exception as e:
        logging.getLogger("experiments").warning("Copy trading failed: %s", e)
    try:
        from experiments import unlocks
        await unlocks.tick()
    except Exception as e:
        logging.getLogger("experiments").warning("Unlock shorts failed: %s", e)
    try:
        from experiments import vol_premium
        await vol_premium.tick()
    except Exception as e:
        logging.getLogger("experiments").warning("Vol premium book failed: %s", e)
    try:
        from experiments import fade24
        await fade24.monitor_pairs()
    except Exception as e:
        logging.getLogger("experiments").warning("Monitoring fade24 failed: %s", e)
    try:
        from experiments import carry_q
        await carry_q.tick()
    except Exception as e:
        logging.getLogger("experiments").warning("Carry Q book failed: %s", e)
    try:
        from experiments import carry
        await carry.tick()
    except Exception as e:
        logging.getLogger("experiments").warning("Carry book failed: %s", e)


def open_symbols() -> set[str]:
    """Coins the experiment books hold — kept in the scan so reviews can see them."""
    try:
        from experiments import fade24, hold72
        from experiments import events, unlocks
        return ({t["symbol"] for w in WALLETS.values() for t in w.get_portfolio()["open_trades"]}
                | {p["symbol"] for p in hold72.open_pairs()} | {p["symbol"] for p in fade24.open_pairs()}
                | {p["symbol"] for p in events.open_positions()} | {p["symbol"] for p in unlocks.open_positions()})
    except Exception:
        return set()
