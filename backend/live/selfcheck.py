"""Prove the live plumbing end to end on the Binance futures TESTNET before switching anything on.

    ./venv/bin/python -m live.selfcheck                  # uses BINANCE_TESTNET_API_KEY / _SECRET
    ./venv/bin/python -m live.selfcheck --system auto    # a system's own key, if it has one
    ./venv/bin/python -m live.selfcheck --symbol ETHUSDT

It does not need LIVE_TRADING_ALLOWED and never touches the app's ledgers. Steps:
  1. server time, key permissions, one-way position mode, balance
  2. symbol rules; isolated margin + leverage
  3. a minimum-size market BUY, a close-position stop below it, move that stop, then a
     reduce-only market SELL — checking the fills and commission come back
  4. a post-only limit far from the market, then cancel it
  5. user-data stream: get a listen key
and cleans up (cancel all, flatten) whatever happens. It refuses to run against mainnet.
"""

import argparse
import sys
import time

from live import config as LC
from live.binance import BinanceError, BinanceFutures, client_id, summarize_fills


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--system", default="main", choices=LC.SYSTEMS)
    ap.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args()
    if LC.ENV != "testnet":
        print("Refusing: BINANCE_ENV is not testnet. This check places real orders.")
        return 2
    keys = LC.keys(args.system)
    if not keys:
        print("No testnet key: set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET in .env "
              "(create them at https://testnet.binancefuture.com → API Key).")
        return 2
    log = []
    c = BinanceFutures(*keys, rest=LC.REST, audit=lambda r: log.append(r), system=args.system)
    sym = args.symbol
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            out = fn()
            print(f"  ✓ {name}" + (f" — {out}" if out else ""))
            return out
        except BinanceError as e:
            ok = False
            print(f"  ✗ {name} — {e}")
        return None

    print(f"Binance futures {LC.ENV} ({LC.REST}), key for '{args.system}', symbol {sym}\n")
    try:
        step("server time", lambda: c._sync_time() or f"clock offset {c._offset_ms} ms")
        hedge = step("position mode", lambda: "hedge mode — switch to one-way!" if c.hedge_mode() else "one-way")
        if hedge and hedge.startswith("hedge"):
            ok = False
        eq = step("balance", lambda: c.equity())
        rules = step("symbol rules", lambda: c.rules(sym))
        step("isolated margin, 5x", lambda: c.prepare(sym, 5))
        mark = float(c.request("GET", "/fapi/v1/premiumIndex", {"symbol": sym}, signed=False)["markPrice"])
        qty = c.qty(sym, max(rules["min_qty"], (rules["min_notional"] * 1.2) / mark)) if rules else 0
        if eq and qty * mark > eq["available"] * 5:
            print(f"  ✗ not enough testnet balance for {qty} {sym}")
            return 1
        r = step(f"market BUY {qty}", lambda: c.market(sym, "BUY", qty, cid=client_id("chk")))
        if r:
            time.sleep(1)
            f = summarize_fills(c.fills(sym, int(time.time() * 1000) - 60_000), r.get("orderId"))
            print(f"      filled {f['qty']} @ {f['price']}, commission ${f['commission']:.4f}")
            stp = step("close-position stop 2% below", lambda: c.stop_market(sym, "SELL", mark * 0.98, cid=client_id("stp")))
            if stp:
                print(f"      endpoint: {'algo order API' if stp['algo'] else 'classic order API'}")
                step("move the stop to 1.5% below", lambda: (c.cancel(sym, stp["cid"], stp["algo"]),
                                                             c.stop_market(sym, "SELL", mark * 0.985))[1] and None)
            step("positions", lambda: [(p["symbol"], p["positionAmt"]) for p in c.positions(sym)])
            step("reduce-only market SELL (close)", lambda: c.market(sym, "SELL", qty, reduce_only=True) and None)
        lim = step("post-only limit 5% below", lambda: c.limit(sym, "BUY", qty, mark * 0.95, post_only=True,
                                                                cid=client_id("chk")))
        if lim:
            step("cancel it", lambda: c.cancel(sym, lim["clientOrderId"]) and None)
        step("user-data listen key", lambda: c.listen_key()[:8] + "…")
    finally:
        try:
            c.cancel_all(sym)
            for p in c.positions(sym):
                amt = float(p["positionAmt"])
                c.market(sym, "SELL" if amt > 0 else "BUY", abs(amt), reduce_only=True)
            print("\n  cleanup: open orders cancelled, position flat")
        except BinanceError as e:
            ok = False
            print(f"\n  ✗ cleanup failed — check the testnet account by hand: {e}")
        c.close()
    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'} — {len(log)} requests made")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
