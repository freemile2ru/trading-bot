"""Index shadow — the reversal and cascade signals expressed in the market instead of the picked coin (2026-11-11, pre-registered).

The 2026-11-11 edge review (683 closed trades, local and prod) adjusted every trade for an equal-weight index of 20 large
alts. Two signals carried their information in the *timing*, not in the coin:
  * reversal-lane shorts: over 33 entry episodes the alt index moved +1.05% our way against +0.44% for a random 9h
    short in the same days, while the coins the score picked made +0.77% net — the pick gave ~0.3% back;
  * cascade-book longs: over 19 episodes the index rebounded +0.48% against −0.18% for a random 4h long, while the
    cascaded coin lagged the index.

This module scores, for every closed trade of those books, what the same window paid in three expressions, each net of
a round-trip taker fee: the picked coin, the equal-weight alt index (INDEX), and BTC+ETH half and half (MAJORS). It
places no trade. Entries within EPISODE_GAP_S of each other are one episode (a cascade or a reversal slot fires several
coins at once, which would otherwise count one market move many times). Every report carries a baseline: the index
held for the signal's median hold from every hour of the same period, in the signal's direction.

Pre-registered verdict, on episodes that open on or after SINCE only (the backfill is shown, not scored), once a signal
has VERDICT_EPISODES of them:
  * "index better": the index beats the coin by ≥ MIN_GAP_PCT a episode, t ≥ 2, and both halves agree;
  * "coin better": the coin beats the index on the same terms;
  * otherwise "no difference".
A signal's timing is "real" when the index's mean beats the baseline by ≥ MIN_GAP_PCT with t ≥ 2 on the same episodes.
"""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone

from storage import state as kv

logger = logging.getLogger("experiments.index_shadow")

SIGNALS = {"reversal": "auto:reversal", "cascade": "cascade"}           # signal → the book whose closed trades carry it
INDEX = ("ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT", "NEARUSDT", "UNIUSDT",
         "AAVEUSDT", "LTCUSDT", "DOTUSDT", "BNBUSDT", "ENAUSDT", "ARBUSDT", "WLDUSDT", "ZECUSDT", "TAOUSDT", "BCHUSDT")
MAJORS = ("BTCUSDT", "ETHUSDT")
FEE_RT_PCT = 0.10                                                     # 5 bps a side, taker, as the ledgers charge
EPISODE_GAP_S = 2 * 3600
SINCE = datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp()
VERDICT_EPISODES = 40
MIN_GAP_PCT = 0.2
EVERY_S = int(os.getenv("INDEX_SHADOW_EVERY_S", "900"))
BATCH = int(os.getenv("INDEX_SHADOW_BATCH", "10"))
V = 1
_last = {"at": 0.0}
_px_cache: dict[tuple[str, int], float | None] = {}


def _k(*parts: str) -> str:
    return kv.key("index_shadow", *parts)


async def price_at(symbol: str, ts: float) -> float | None:
    """The perpetual's price at `ts`: the open of the 5-minute bar that contains it (cached)."""
    bucket = int(ts // 300) * 300
    ck = (symbol, bucket)
    if ck in _px_cache:
        return _px_cache[ck]
    from data.binance_client import FAPI
    from data.http import get_json
    rows = await get_json(f"{FAPI}/klines", params={"symbol": symbol, "interval": "5m", "startTime": bucket * 1000, "limit": 1})
    px = float(rows[0][1]) if rows else None
    if len(_px_cache) > 20_000:
        _px_cache.clear()
    _px_cache[ck] = px
    return px


async def basket_move(symbols: tuple[str, ...], t0: float, t1: float) -> float | None:
    """Equal-weight % move of a basket from t0 to t1 (members without a price on either side are left out)."""
    import asyncio
    px = await asyncio.gather(*(price_at(s, t) for s in symbols for t in (t0, t1)), return_exceptions=True)
    moves = []
    for i in range(0, len(px), 2):
        a, b = px[i], px[i + 1]
        if isinstance(a, float) and isinstance(b, float) and a and b:
            moves.append((b / a - 1) * 100)
    return sum(moves) / len(moves) if len(moves) >= max(1, len(symbols) // 2) else None


def score(trade: dict, coin: float, index: float, majors: float | None) -> dict:
    """One trade's three expressions, in the trade's direction, net of a round trip."""
    s = 1 if trade["direction"] == "long" else -1
    notional = trade.get("notional_usd") or (trade.get("quantity") or 0) * (trade.get("entry_price") or 0)
    return {"coin_pct": round(s * coin - FEE_RT_PCT, 4), "index_pct": round(s * index - FEE_RT_PCT, 4),
            "majors_pct": None if majors is None else round(s * majors - FEE_RT_PCT, 4),
            "actual_pct": round(100 * (trade.get("pnl_usd") or 0) / notional, 4) if notional else None}


async def record(signal: str, trade: dict) -> dict | None:
    t0, t1 = trade["opened_ts"], trade["closed_ts"]
    coin = await basket_move((trade["symbol"],), t0, t1)
    index = await basket_move(INDEX, t0, t1)
    if coin is None or index is None:
        return None
    majors = await basket_move(MAJORS, t0, t1)
    rec = {"v": V, "signal": signal, "trade_id": trade["id"], "symbol": trade["symbol"], "direction": trade["direction"],
           "opened_ts": t0, "closed_ts": t1, "hold_h": round((t1 - t0) / 3600, 2), **score(trade, coin, index, majors)}
    kv.set_sync(_k(trade["id"]), rec)
    idx = kv.get_sync(_k("index", signal)) or []
    if trade["id"] not in idx:
        kv.set_sync(_k("index", signal), (idx + [trade["id"]])[-2000:])
    return rec


def _trades(signal: str) -> list[dict]:
    from experiments import control
    return control.closed_trades(SIGNALS[signal])


async def baseline(direction: str, start: float, end: float, hold_h: float) -> dict | None:
    """The index held `hold_h` hours from every hour in [start, end], in `direction`, net of a round trip."""
    from data.binance_client import FAPI
    from data.http import get_json
    hold = max(1, round(hold_h))
    series: dict[int, list[float]] = {}
    for s in INDEX:
        cursor, rows = int(start * 1000), []
        for _ in range(4):
            got = await get_json(f"{FAPI}/klines", params={"symbol": s, "interval": "1h", "startTime": cursor, "endTime": int(end * 1000), "limit": 1000})
            if not got:
                break
            rows += got
            if len(got) < 1000:
                break
            cursor = got[-1][0] + 1
        for r in rows:
            series.setdefault(int(r[0] // 3_600_000), []).append(float(r[1]))
    full = {h: v for h, v in series.items() if len(v) == len(INDEX)}
    sgn = 1 if direction == "long" else -1
    moves = [sgn * (sum(b / a for a, b in zip(full[h], full[h + hold])) / len(INDEX) - 1) * 100 - FEE_RT_PCT
             for h in sorted(full) if h + hold in full]
    if not moves:
        return None
    return {"mean_pct": round(sum(moves) / len(moves), 4), "n_starts": len(moves), "hold_h": hold,
            "from": start, "to": end, "at": time.time()}


async def run(signal: str, limit: int = BATCH) -> dict:
    """Score the signal's closed trades that have no record yet; refresh its baselines when something new landed."""
    done = set(kv.get_sync(_k("index", signal)) or [])
    todo = [t for t in _trades(signal) if t["id"] not in done]
    got = 0
    for t in todo[-limit:]:
        try:
            if await record(signal, t):
                got += 1
        except Exception as e:
            logger.warning("Index shadow for %s %s failed: %s", signal, t.get("symbol"), e)
    if got or not kv.get_sync(_k("baseline", signal)):
        recs = records(signal)
        out = {}
        for side in ("long", "short"):
            rs = [r for r in recs if r["direction"] == side]
            if len(rs) < 3:
                continue
            hold = sorted(r["hold_h"] for r in rs)[len(rs) // 2]
            try:
                b = await baseline(side, min(r["opened_ts"] for r in rs), max(r["closed_ts"] for r in rs), hold)
            except Exception as e:
                logger.warning("Index shadow baseline for %s %s failed: %s", signal, side, e)
                b = None
            if b:
                out[side] = b
        if out:
            kv.set_sync(_k("baseline", signal), out)
    return {"signal": signal, "recorded": got, "pending": max(0, len(todo) - got)}


async def tick(now: float | None = None) -> None:
    now = now or time.time()
    if now - _last["at"] < EVERY_S:
        return
    _last["at"] = now
    for signal in SIGNALS:
        try:
            await run(signal)
        except Exception as e:
            logger.warning("Index shadow tick for %s failed: %s", signal, e)


def records(signal: str) -> list[dict]:
    return [r for r in (kv.get_sync(_k(tid)) for tid in (kv.get_sync(_k("index", signal)) or [])) if r]


def episodes(rows: list[dict]) -> list[dict]:
    """Entries within EPISODE_GAP_S of the previous one join its episode; each episode is the mean of its trades."""
    eps: list[list[dict]] = []
    for r in sorted(rows, key=lambda r: r["opened_ts"]):
        if eps and r["opened_ts"] - eps[-1][-1]["opened_ts"] < EPISODE_GAP_S:
            eps[-1].append(r)
        else:
            eps.append([r])

    def mean(rs, k):
        v = [r[k] for r in rs if r.get(k) is not None]
        return sum(v) / len(v) if v else None
    return [{"opened_ts": e[0]["opened_ts"], "n": len(e), "symbols": [r["symbol"] for r in e],
             **{k: mean(e, k) for k in ("coin_pct", "index_pct", "majors_pct", "actual_pct")}} for e in eps]


def _stats(xs: list[float]) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean_pct": round(m, 3), "t": round(m / sd * math.sqrt(n), 2) if sd else None,
            "pos_pct": round(100 * sum(1 for x in xs if x > 0) / n)}


def _compare(diffs: list[float]) -> tuple[dict, bool]:
    s = _stats(diffs)
    half = len(diffs) // 2
    halves = (sum(diffs[:half]) / half if half else 0.0, sum(diffs[half:]) / (len(diffs) - half) if len(diffs) - half else 0.0)
    ok = s["n"] >= 2 and s["mean_pct"] >= MIN_GAP_PCT and (s["t"] or 0) >= 2 and min(halves) > 0
    return {**s, "halves_pct": [round(h, 3) for h in halves]}, ok


def verdict(eps: list[dict], base: float | None) -> dict:
    """The pre-registered verdict on forward episodes (see the module docstring)."""
    pair = [e for e in eps if e["coin_pct"] is not None and e["index_pct"] is not None]
    if len(pair) < VERDICT_EPISODES:
        return {"verdict": "measuring", "episodes": len(pair), "needed": VERDICT_EPISODES}
    idx_vs_coin, idx_better = _compare([e["index_pct"] - e["coin_pct"] for e in pair])
    coin_vs_idx, coin_better = _compare([e["coin_pct"] - e["index_pct"] for e in pair])
    out = {"verdict": "index better" if idx_better else "coin better" if coin_better else "no difference",
           "episodes": len(pair), "index_minus_coin": idx_vs_coin}
    if base is not None:
        timing, real = _compare([e["index_pct"] - base for e in pair])
        out["timing_vs_baseline"] = {**timing, "real": real}
    return out


def report(signal: str) -> dict:
    rows = records(signal)
    base = kv.get_sync(_k("baseline", signal)) or {}
    sides = {}
    for side in ("long", "short"):
        rs = [r for r in rows if r["direction"] == side]
        if not rs:
            continue
        eps = episodes(rs)
        fwd = [e for e in eps if e["opened_ts"] >= SINCE]
        b = (base.get(side) or {}).get("mean_pct")

        def block(es):
            return {k: _stats([e[k] for e in es if e[k] is not None]) for k in ("actual_pct", "coin_pct", "index_pct", "majors_pct")}
        sides[side] = {"trades": len(rs), "all": {"episodes": len(eps), **block(eps)}, "forward": {"episodes": len(fwd), **block(fwd)},
                       "baseline": base.get(side), "verdict": verdict(fwd, b),
                       "recent": sorted(eps, key=lambda e: -e["opened_ts"])[:30]}
    return {"signal": signal, "book": SIGNALS[signal], "since": SINCE, "verdict_episodes": VERDICT_EPISODES, "min_gap_pct": MIN_GAP_PCT,
            "fee_rt_pct": FEE_RT_PCT, "index": list(INDEX), "sides": sides,
            "pending": sum(1 for t in _trades(signal) if t["id"] not in {r["trade_id"] for r in rows})}
