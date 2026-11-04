"""Close and erase every trade of the books NOT promoted to this instance (trading/book_switches.promoted).

Experiments run on the local instance; production runs only its promoted books. When a book is taken off a
production instance, its open positions are closed at the last mark, every closed row of the book is erased
(with its debriefs and shadow results), the autotrader's legs that mirrored it go the same way, and its capital
— seed and plan money — is withdrawn, not reallocated: the book is not in this instance's plan. Nothing is learned from a book that cannot run here.

Run with the backend STOPPED (the live process caches wallet state), from the compose directory:

  docker compose run --rm --no-deps -T --entrypoint python backend scripts/close_unpromoted.py           # dry run
  docker compose run --rm --no-deps -T --entrypoint python backend scripts/close_unpromoted.py --apply

Locally, `PROMOTED_BOOKS=main,auto APP_ENV=production python scripts/close_unpromoted.py` shows what would go.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REASON = "not_promoted_here"
UNWOUND = "switched_off"            # the carry books unwind themselves on their first tick after the gate lands


def _say(*a):
    print(*a, flush=True)


async def _pair_book(name: str, mod, apply: bool) -> None:
    """A pair-ledger book: close at market through its own close path, then erase both reasons."""
    from trading import monitor
    opened = mod.open_positions()
    _say(f"{name}: {len(opened)} open, {len(mod.closed_positions())} closed")
    if not apply:
        return
    now = time.time()
    for p in opened:
        if name in ("carry", "carry_q"):
            done = await mod._close(p, REASON, now)
        elif name == "vol_premium":
            done = mod.close_at_mark(p, REASON, now)
        else:                                                   # events, unlocks: coin vs BTC
            px, btc = await monitor.live_price(p["symbol"]), await monitor.live_price(mod.HEDGE)
            done = await mod.close(p, px, btc, REASON, now) if px and btc else None
        _say(f"  {'closed' if done else 'NO PRICE — left open'}: {p.get('symbol') or p.get('coin')}")
    n = mod.erase_closed(REASON) + mod.erase_closed(UNWOUND)
    _say(f"  erased {n} closed rows")


def _wallet_book(name: str, w, apply: bool) -> None:
    from storage import signals
    p = w.get_portfolio(closed_limit=1, history_limit=1)
    closed_ids = {t["id"] for t in w.all_trades()["closed"]}
    pending = w.get_orders(1)["pending"]
    _say(f"{name}: {len(p['open_trades'])} open, {len(closed_ids)} closed, {len(pending)} resting orders")
    if not apply:
        return
    for o in pending:
        w.cancel_order(o["id"], REASON)
    for t in p["open_trades"]:
        w.close_trade_manually(t["id"], t.get("current_price") or t["entry_price"], REASON)
        closed_ids.add(t["id"])
    if closed_ids:
        gone = w.erase_closed_trades(closed_ids, REASON)
        signals.delete_debriefs(name, gone.get("ids", []))
        _say(f"  erased {gone['removed']} trades, P&L {gone['pnl']:+.2f} reversed")


def _autotrader_legs(apply: bool) -> None:
    """The autotrader's legs that mirrored an unpromoted signal book."""
    from autotrader import lanes
    from live import swing as live_swing
    from storage import signals
    from trading import book_switches
    for w in live_swing.wallets("auto"):
        gone: set[str] = set()
        for t in w.get_portfolio(closed_limit=1, history_limit=1)["open_trades"] + w.all_trades()["closed"]:
            lane = lanes.lane_of(t)
            book = lanes.LANE_BOOK.get(lane, "auto")
            if book_switches.promoted(book):
                continue
            _say(f"auto leg {t['symbol']} {t['direction']} ({lane} lane, {t.get('status')}) mirrors {book}")
            if apply:
                if t.get("status") == "open":
                    w.close_trade_manually(t["id"], t.get("current_price") or t["entry_price"], REASON)
                gone.add(t["id"])
        if gone:
            out = w.erase_closed_trades(gone, REASON)
            signals.delete_debriefs("auto", out.get("ids", []))
            _say(f"  erased {out['removed']} autotrader legs")


def _capital(name: str, apply: bool) -> None:
    """Take every dollar out — the seed and whatever the plan moved in. The money is not reallocated: an unpromoted book
    is not in this instance's plan (trading/capital.plan), so nothing refills it."""
    from trading import capital
    held = capital.BASE + capital.added(name)
    if held > 0.5:
        _say(f"{name}: ${held:,.0f} of capital to remove")
        if apply:
            entry = capital.withdraw(name, held, "not promoted to this instance — capital removed")
            _say(f"  withdrew ${-(entry or {}).get('amount', 0):,.0f}")


async def main(apply: bool) -> None:
    from experiments import books, carry, carry_q, events, unlocks, vol_premium
    from markets import engine as markets
    from trading import book_switches
    pair = {"carry": carry, "carry_q": carry_q, "events": events, "unlocks": unlocks, "vol_premium": vol_premium}
    shut = [b["name"] for b in book_switches.BOOKS if not book_switches.promoted(b["name"])]
    _say(f"{'APPLYING' if apply else 'DRY RUN'} — promoted here: {sorted(book_switches.PROMOTED) if book_switches.PRODUCTION else 'everything (not production)'}")
    _say(f"closing and erasing: {shut}")
    for name in shut:
        if name in pair:
            await _pair_book(name, pair[name], apply)
        elif name in books.WALLETS:
            _wallet_book(name, books.WALLETS[name], apply)
        elif name in markets.WALLETS:
            _wallet_book(name, markets.WALLETS[name], apply)
        else:
            _say(f"{name}: no wallet here")
        _capital(name, apply)
    _autotrader_legs(apply)
    _say("done" if apply else "dry run only — add --apply")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
