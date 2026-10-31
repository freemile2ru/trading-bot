"""What the top Hyperliquid wallets actually do — read from their fills, not from what anyone says about them.

Two cohorts from the public leaderboard, both restricted to equity $50k–$20M (bigger is a market maker, smaller
is noise): WINNERS = the top N by month ROI among wallets positive over the week, month and all time; LOSERS = the
N worst month ROI among wallets with a negative month. For each wallet the last DAYS of fills are rebuilt into
round-trip episodes (data/hyperliquid_client.episodes), and each episode is placed against the coin's own price:

  prior_24h   the coin's move over the 24h before entry, signed by the side: > 0 means the trade went WITH the
              prior move (continuation), < 0 against it (reversal)
  after_24h   the coin's move over the 24h after exit, signed by the side: > 0 means they left money on the table

Per cohort: hold time, win rate, average move, long share, continuation share, trades a day, and how
concentrated they are. The point is to see which strategy family the profitable cohort resembles — momentum,
reversal, or neither — and how that differs from the losing cohort.

    ./venv/bin/python -m research.top_traders_lab --n 25 --days 30
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import time

from data import hyperliquid_client as hl

RESULTS = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "top_traders_results.json")
EQUITY_MIN, EQUITY_MAX = 50_000, 20_000_000


def cohorts(rows: list[dict], n: int) -> tuple[list[dict], list[dict]]:
    ok = [r for r in rows if EQUITY_MIN <= (r.get("equity") or 0) <= EQUITY_MAX and r.get("month") and r.get("all") and r.get("week")]
    winners = sorted((r for r in ok if r["week"]["pnl"] > 0 and r["month"]["pnl"] > 0 and r["all"]["pnl"] > 0),
                     key=lambda r: -r["month"]["roi"])[:n]
    losers = sorted((r for r in ok if r["month"]["pnl"] < 0), key=lambda r: r["month"]["roi"])[:n]
    return winners, losers


async def _klines(symbol: str, start_ms: int, end_ms: int) -> dict[int, float]:
    from data.derivatives_client import fetch_klines_range
    rows = await fetch_klines_range(symbol, "1h", start_ms, end_ms)
    return {int(r[0] // 3_600_000): r[4] for r in rows}


def _ret(closes: dict[int, float], t0: float, t1: float) -> float | None:
    a, b = closes.get(int(t0 // 3600)), closes.get(int(t1 // 3600))
    return (b / a - 1) if a and b else None


def describe(episodes: list[dict], ctx: dict[str, dict[int, float]]) -> dict:
    """Cohort-level statistics over every episode with price context."""
    if not episodes:
        return {"episodes": 0}
    holds = [e["hold_h"] for e in episodes]
    moves = [e["move_pct"] for e in episodes]
    prior, after = [], []
    for e in episodes:
        c = ctx.get(e["coin"])
        if not c:
            continue
        s = 1 if e["side"] == "long" else -1
        p = _ret(c, e["opened_ts"] - 86400, e["opened_ts"])
        a = _ret(c, e["closed_ts"], e["closed_ts"] + 86400)
        if p is not None:
            prior.append(p * s)
        if a is not None:
            after.append(a * s)
    by_coin: dict[str, int] = {}
    for e in episodes:
        by_coin[e["coin"]] = by_coin.get(e["coin"], 0) + 1
    top3 = sorted(by_coin.values(), reverse=True)[:3]
    return {
        "episodes": len(episodes), "median_hold_h": round(statistics.median(holds), 2),
        "hold_under_1h_pct": round(100 * sum(h < 1 for h in holds) / len(holds), 1),
        "hold_over_24h_pct": round(100 * sum(h > 24 for h in holds) / len(holds), 1),
        "win_rate_pct": round(100 * sum(m > 0 for m in moves) / len(moves), 1),
        "avg_move_pct": round(statistics.mean(moves), 3), "median_move_pct": round(statistics.median(moves), 3),
        "avg_win_pct": round(statistics.mean([m for m in moves if m > 0]), 3) if any(m > 0 for m in moves) else None,
        "avg_loss_pct": round(statistics.mean([m for m in moves if m <= 0]), 3) if any(m <= 0 for m in moves) else None,
        "long_share_pct": round(100 * sum(e["side"] == "long" for e in episodes) / len(episodes), 1),
        "with_prior_context": len(prior),
        "continuation_share_pct": round(100 * sum(p > 0 for p in prior) / len(prior), 1) if prior else None,
        "avg_signed_prior_24h_pct": round(100 * statistics.mean(prior), 3) if prior else None,
        "avg_signed_after_exit_24h_pct": round(100 * statistics.mean(after), 3) if after else None,
        "top3_coin_share_pct": round(100 * sum(top3) / len(episodes), 1),
        "coins_traded": len(by_coin),
    }


async def main(n: int = 25, days: int = 30) -> dict:
    from data.feeds import cached_perpetual_symbols
    board = await hl.leaderboard()
    winners, losers = cohorts(board, n)
    perps = await cached_perpetual_symbols()
    now = time.time()
    start_ms = int((now - days * 86400) * 1000)
    out = {"generated_at": now, "days": days, "n": n, "board": len(board), "cohorts": {}}
    ctx: dict[str, dict[int, float]] = {}
    for label, rows in (("winners", winners), ("losers", losers)):
        eps, per_wallet = [], []
        for i, r in enumerate(rows):
            try:
                f = await hl.fills(r["address"], start_ms, max_pages=3)
            except Exception as e:
                print(f"  {label} {i + 1}/{len(rows)} {r['address'][:10]}: fills failed ({e})")
                continue
            e = hl.episodes(f)
            eps += e
            per_wallet.append({"address": r["address"][:10], "equity": r["equity"], "month_roi_pct": round(r["month"]["roi"] * 100, 1),
                               "episodes": len(e), "median_hold_h": round(statistics.median([x["hold_h"] for x in e]), 2) if e else None})
            print(f"  {label} {i + 1}/{len(rows)} {r['address'][:10]}: {len(e)} episodes, month ROI {r['month']['roi'] * 100:.0f}%")
            await asyncio.sleep(0.5)
        coins = {e["coin"] for e in eps}
        for c in sorted(coins):
            sym = hl.binance_symbol(c, perps)
            if sym and c not in ctx:
                try:
                    ctx[c] = await _klines(sym, start_ms - 2 * 86400 * 1000, int(now * 1000))
                except Exception:
                    ctx[c] = {}
        out["cohorts"][label] = {"wallets": per_wallet, **describe(eps, ctx)}
    json.dump(out, open(RESULTS, "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    rep = asyncio.run(main(a.n, a.days))
    for label, c in rep["cohorts"].items():
        print(f"\n■ {label}: {c.get('episodes')} episodes from {len(c.get('wallets', []))} wallets")
        for k, v in c.items():
            if k != "wallets":
                print(f"  {k:32} {v}")
    print(f"\nWritten to {RESULTS}")
