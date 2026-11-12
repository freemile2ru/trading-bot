"""Entry cells — the conditions at every crypto entry, scored forward (2026-11-12, pre-registered).

Why: the 2026-11-12 entry research (docs/research/entry-research-2026-11-12.md) scored every closed trade on 1h bars. Long
entries were random — mean favourable excursion ÷ mean adverse excursion (the Turtle E-ratio) 1.07 / 0.98 / 0.92 at
1h / 4h / 24h over 331 trades, −1.02% a trade — while shorts had an edge (1.31 / 1.09 / 1.47). The long losses sat in
four cells: the coin already up in the last 4h (−2.1%, t −4.3), BTC already up on the day (−1.9%, t −4.0), funding
being paid at ≥ 0.01%/8h (−1.1%, t −3.2) and entries at 12–16 UTC (−1.1%, t −3.4). The mirror cells were the best
shorts: alt shorts on a BTC +2% day (+2.4%, t 3.0, n 24) and shorts opened 16–20 UTC (+1.0%, t 2.3). Four cuts chosen on
the same sample is how overfitting happens, so none of it is a rule: this records the conditions at every entry and
judges them forward.

What is recorded, once a trade closes, for every crypto trade of BOOKS (stock perps are left out — BTC and funding are
crypto conditions): the funding rate last settled before the entry (% per 8h), BTC's move over the 24h before it, the
coin's move over the 4h before it in the trade's direction, the UTC hour, the trade's net % — and, for a long, which of
the four gate conditions it failed; for a short, which of the two cells it sits in.

Also the reversal lane's long leg, which the lane has not traded since 2026-11-02 (AUTOTRADER_REVERSAL_SIDES=short):
every 4h slot the top-REVERSAL_K coins of the score are written down with their price and scored 24h later, alone and
hedged with BTC, net of a round trip each leg. The literature (Fičura 2023) says liquid-coin returns sit in the short
leg and the long-side reversal only pays in illiquid coins; this measures whether our long leg would have paid.

Pre-registered verdicts, on trades (slots) opened on or after SINCE only — the backfill is shown, not scored:
  long gate     at VERDICT_N rejected longs: "gate works" when the kept longs beat the rejected by ≥ MIN_GAP_PCT a trade
                with Welch t ≥ 2 and both halves of the period agreeing; otherwise "no difference".
  short cells   at VERDICT_N shorts in the cell: "cell real" when the cell beats the shorts outside it on the same terms.
  reversal long at VERDICT_N settled slots: "long leg real" when the hedged return is ≥ MIN_GAP_PCT a slot (fees paid),
                t ≥ 2, both halves positive; otherwise "no edge".
Nothing here trades; a verdict is read by the user before anything changes.
"""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone

from storage import state as kv

logger = logging.getLogger("experiments.entry_cells")

BOOKS = [b.strip() for b in os.getenv("ENTRY_CELLS_BOOKS", "auto,main,auto_rules,copy_trading,copy_crypto_wide,cascade,weekly_rs,claude_swing").split(",") if b.strip()]
SINCE = datetime(2026, 11, 12, tzinfo=timezone.utc).timestamp()
VERDICT_N = 40
MIN_GAP_PCT = 0.5
GATE = {"funding_max_pct": 0.01, "btc24_max_pct": 0.0, "pre4_max_pct": 0.0, "hours_off": (12, 16)}
SHORT_CELLS = {"btc_up_day": "BTC ≥ +2% over the 24h before the entry", "late_us": "opened 16–20 UTC"}
SHADOW_HOLD_H = 24
FEE_RT_PCT = 0.10
EVERY_S = int(os.getenv("ENTRY_CELLS_EVERY_S", "900"))
BATCH = int(os.getenv("ENTRY_CELLS_BATCH", "20"))
STOCK_PERPS = {"SKHYNIXUSDT", "AMDUSDT", "NVDAUSDT", "TSLAUSDT", "QQQUSDT", "SPYUSDT", "INTCUSDT", "SAMSUNGUSDT", "MUUSDT", "CRCLUSDT", "COINUSDT",
               "HOODUSDT", "MSTRUSDT", "PLTRUSDT", "METAUSDT", "AAPLUSDT", "GOOGLUSDT", "AMZNUSDT", "MSFTUSDT", "GLDUSDT"}
V = 1
_last = {"at": 0.0}


def _k(*parts: str) -> str:
    return kv.key("entry_cells", *parts)


def is_crypto(symbol: str) -> bool:
    return symbol.endswith("USDT") and symbol not in STOCK_PERPS


# ── Trades ────────────────────────────────────────────────────────────────────

def closed_trades(book: str) -> list[dict]:
    """The book's closed crypto trades, oldest first, with opened_ts / closed_ts and the autotrader's lane."""
    from experiments import control
    if book in ("auto", "main"):
        from trading import history
        rows = history.WALLETS[book].all_trades()["closed"]
    else:
        from experiments import books
        if book not in books.WALLETS:
            return []
        rows = books.WALLETS[book].all_trades()["closed"]
    out = []
    for t in rows:
        o, c = t.get("opened_ts") or control._ts(t.get("opened_at")), control._ts(t.get("closed_at"))
        if o and c and is_crypto(t.get("symbol") or "") and t.get("entry_price"):
            lane = None
            if book == "auto":
                from autotrader import lanes
                lane = lanes.lane_of(t)
            out.append({**t, "opened_ts": o, "closed_ts": c, "lane": lane})
    return sorted(out, key=lambda t: t["opened_ts"])


# ── The conditions at an entry ────────────────────────────────────────────────

async def conditions(symbol: str, direction: str, ts: float, entry_price: float) -> dict:
    """Funding last settled before `ts` (% per 8h), BTC's 24h move into `ts`, the coin's 4h move into the entry in the
    trade's direction, the UTC hour. Each is None when Binance has no data for it."""
    from data.binance_client import FAPI
    from data.http import get_json
    from experiments.index_shadow import price_at
    s = 1 if direction == "long" else -1
    end = int(ts * 1000)
    out: dict = {"fund_pct": None, "btc24_pct": None, "pre4_pct": None, "hour": datetime.fromtimestamp(ts, timezone.utc).hour}
    try:
        fr = await get_json(f"{FAPI}/fundingRate", params={"symbol": symbol, "endTime": end, "limit": 1})
        if fr:
            out["fund_pct"] = round(float(fr[-1]["fundingRate"]) * 100, 4)
    except Exception as e:
        logger.debug("funding for %s: %s", symbol, e)
    try:
        k = await get_json(f"{FAPI}/klines", params={"symbol": symbol, "interval": "1h", "endTime": end - 1, "limit": 5})
        if k and len(k) == 5:                                           # k[-1] is the entry's own hour; k[0] closed 4h before it
            out["pre4_pct"] = round(s * (entry_price / float(k[0][4]) - 1) * 100, 3)
    except Exception as e:
        logger.debug("coin bars for %s: %s", symbol, e)
    try:
        b = await get_json(f"{FAPI}/klines", params={"symbol": "BTCUSDT", "interval": "1h", "endTime": end - 1, "limit": 25})
        now_px = await price_at("BTCUSDT", ts)
        if b and len(b) == 25 and now_px:
            out["btc24_pct"] = round((now_px / float(b[0][4]) - 1) * 100, 3)
    except Exception as e:
        logger.debug("BTC bars: %s", e)
    return out


def gate_flags(c: dict) -> list[str]:
    """Which of the long gate's conditions an entry fails (an unmeasured condition does not fail)."""
    flags = []
    if c.get("fund_pct") is not None and c["fund_pct"] >= GATE["funding_max_pct"]:
        flags.append("funding_paid")
    if c.get("btc24_pct") is not None and c["btc24_pct"] >= GATE["btc24_max_pct"]:
        flags.append("btc_up")
    if c.get("pre4_pct") is not None and c["pre4_pct"] > GATE["pre4_max_pct"]:
        flags.append("chasing_4h")
    lo, hi = GATE["hours_off"]
    if lo <= c["hour"] < hi:
        flags.append("us_open")
    return flags


def short_cells(c: dict) -> list[str]:
    cells = []
    if c.get("btc24_pct") is not None and c["btc24_pct"] >= 2.0:
        cells.append("btc_up_day")
    if 16 <= c["hour"] < 20:
        cells.append("late_us")
    return cells


async def record(book: str, trade: dict) -> dict | None:
    notional = trade.get("notional_usd") or (trade.get("quantity") or 0) * (trade.get("entry_price") or 0)
    if not notional:
        return None
    c = await conditions(trade["symbol"], trade["direction"], trade["opened_ts"], float(trade["entry_price"]))
    rec = {"v": V, "book": book, "lane": trade.get("lane"), "trade_id": trade["id"], "symbol": trade["symbol"], "direction": trade["direction"],
           "opened_ts": trade["opened_ts"], "closed_ts": trade["closed_ts"], "actual_pct": round(100 * (trade.get("pnl_usd") or 0) / notional, 4), **c}
    if trade["direction"] == "long":
        rec["flags"] = gate_flags(c)
        rec["kept"] = not rec["flags"]
    else:
        rec["cells"] = short_cells(c)
    kv.set_sync(_k(trade["id"]), rec)
    idx = kv.get_sync(_k("index", book)) or []
    if trade["id"] not in idx:
        kv.set_sync(_k("index", book), (idx + [trade["id"]])[-3000:])
    return rec


async def run(book: str, limit: int = BATCH) -> dict:
    done = set(kv.get_sync(_k("index", book)) or [])
    todo = [t for t in closed_trades(book) if t["id"] not in done]
    got = 0
    for t in todo[-limit:]:
        try:
            if await record(book, t):
                got += 1
        except Exception as e:
            logger.warning("Entry cells for %s %s failed: %s", book, t.get("symbol"), e)
    return {"book": book, "recorded": got, "pending": max(0, len(todo) - got)}


def records(book: str | None = None) -> list[dict]:
    books = [book] if book else BOOKS
    return [r for b in books for r in (kv.get_sync(_k(tid)) for tid in (kv.get_sync(_k("index", b)) or [])) if r]


# ── The reversal lane's long leg, written down and scored ─────────────────────

async def shadow_reversal_longs(scores: dict[str, float], k: int, bucket: int, now: float) -> int:
    """The top-k coins of the reversal score this slot — the longs the lane does not take — with their price now."""
    from experiments.index_shadow import price_at
    top = sorted(scores.items(), key=lambda kv_: kv_[1])[-k:]
    btc = await price_at("BTCUSDT", now)
    n = 0
    for sym, sc in top:
        key = _k("revlong", f"{bucket}:{sym}")
        if kv.get_sync(key):
            continue
        px = await price_at(sym, now)
        if not px or not btc:
            continue
        kv.set_sync(key, {"bucket": bucket, "symbol": sym, "score": round(sc, 4), "opened_ts": now, "price": px, "btc_price": btc, "result": None})
        idx = kv.get_sync(_k("revlong", "index")) or []
        kv.set_sync(_k("revlong", "index"), (idx + [f"{bucket}:{sym}"])[-2000:])
        n += 1
    return n


async def settle_shadow(now: float | None = None) -> int:
    """Score every written-down long whose 24 hours have passed: the coin alone and hedged with BTC, fees paid."""
    from experiments.index_shadow import price_at
    now = now or time.time()
    done = 0
    for key in kv.get_sync(_k("revlong", "index")) or []:
        rec = kv.get_sync(_k("revlong", key))
        if not rec or rec.get("result") or rec["opened_ts"] + SHADOW_HOLD_H * 3600 + 300 > now:
            continue
        t1 = rec["opened_ts"] + SHADOW_HOLD_H * 3600
        p1, b1 = await price_at(rec["symbol"], t1), await price_at("BTCUSDT", t1)
        if not p1 or not b1:
            continue
        coin = (p1 / rec["price"] - 1) * 100 - FEE_RT_PCT
        btc = (b1 / rec["btc_price"] - 1) * 100
        rec["result"] = {"coin_pct": round(coin, 4), "btc_pct": round(btc, 4), "hedged_pct": round(coin - btc - FEE_RT_PCT, 4)}
        kv.set_sync(_k("revlong", key), rec)
        done += 1
    return done


def shadow_records() -> list[dict]:
    return [r for r in (kv.get_sync(_k("revlong", key)) for key in (kv.get_sync(_k("revlong", "index")) or [])) if r]


# ── Loop ──────────────────────────────────────────────────────────────────────

async def tick(now: float | None = None) -> None:
    now = now or time.time()
    if now - _last["at"] < EVERY_S:
        return
    _last["at"] = now
    for book in BOOKS:
        try:
            await run(book)
        except Exception as e:
            logger.warning("Entry cells tick for %s failed: %s", book, e)
    try:
        await settle_shadow(now)
    except Exception as e:
        logger.warning("Reversal long shadow settle failed: %s", e)


# ── Statistics and verdicts ───────────────────────────────────────────────────

def _stats(xs: list[float]) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean_pct": round(m, 3), "t": round(m / sd * math.sqrt(n), 2) if sd else None,
            "pos_pct": round(100 * sum(1 for x in xs if x > 0) / n), "sum_pct": round(sum(xs), 2)}


def welch_t(a: list[float], b: list[float]) -> float | None:
    """Welch's t for mean(a) − mean(b)."""
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return round((ma - mb) / se, 2) if se else None


def two_groups(a: list[dict], b: list[dict], need_in: str) -> dict:
    """Does group a beat group b by MIN_GAP_PCT a trade, t ≥ 2, in both halves of the period? `need_in` names the group
    whose count must reach VERDICT_N ("a" or "b")."""
    xa, xb = [r["actual_pct"] for r in a], [r["actual_pct"] for r in b]
    out = {"a": _stats(xa), "b": _stats(xb)}
    n_need = len(a) if need_in == "a" else len(b)
    if n_need < VERDICT_N:
        return {**out, "verdict": "measuring", "have": n_need, "needed": VERDICT_N}
    both = sorted(a + b, key=lambda r: r["opened_ts"])
    half = len(both) // 2
    halves = []
    for part in (both[:half], both[half:]):
        pa, pb = [r["actual_pct"] for r in part if r in a], [r["actual_pct"] for r in part if r in b]
        halves.append(round((sum(pa) / len(pa) if pa else 0.0) - (sum(pb) / len(pb) if pb else 0.0), 3))
    gap = round(out["a"]["mean_pct"] - out["b"]["mean_pct"], 3)
    t = welch_t(xa, xb)
    ok = gap >= MIN_GAP_PCT and (t or 0) >= 2 and all(h > 0 for h in halves)
    return {**out, "gap_pct": gap, "t": t, "halves_pct": halves, "verdict": "works" if ok else "no difference", "have": n_need, "needed": VERDICT_N}


def report() -> dict:
    rows = records()
    longs = [r for r in rows if r["direction"] == "long"]
    shorts = [r for r in rows if r["direction"] == "short"]

    def gate_block(rs: list[dict]) -> dict:
        kept, rej = [r for r in rs if r.get("kept")], [r for r in rs if not r.get("kept")]
        by_flag = {f: _stats([r["actual_pct"] for r in rs if f in (r.get("flags") or [])]) for f in ("funding_paid", "btc_up", "chasing_4h", "us_open")}
        return {"kept": _stats([r["actual_pct"] for r in kept]), "rejected": _stats([r["actual_pct"] for r in rej]), "by_flag": by_flag}
    fwd_l = [r for r in longs if r["opened_ts"] >= SINCE]
    long_out = {"gate": GATE, "all": gate_block(longs), "forward": gate_block(fwd_l),
                "verdict": {k: v for k, v in two_groups([r for r in fwd_l if r.get("kept")], [r for r in fwd_l if not r.get("kept")], "b").items() if k not in ("a", "b")}}
    long_out["verdict"]["verdict"] = {"works": "gate works"}.get(long_out["verdict"]["verdict"], long_out["verdict"]["verdict"])
    cells = {}
    fwd_s = [r for r in shorts if r["opened_ts"] >= SINCE]
    for cell, label in SHORT_CELLS.items():
        inside = lambda rs: [r for r in rs if cell in (r.get("cells") or [])]
        outside = lambda rs: [r for r in rs if cell not in (r.get("cells") or [])]
        v = two_groups(inside(fwd_s), outside(fwd_s), "a")
        cells[cell] = {"label": label, "all": {"in": _stats([r["actual_pct"] for r in inside(shorts)]), "out": _stats([r["actual_pct"] for r in outside(shorts)])},
                       "forward": {"in": v["a"], "out": v["b"]}, "verdict": {**{k: x for k, x in v.items() if k not in ("a", "b")},
                                                                               "verdict": {"works": "cell real"}.get(v["verdict"], v["verdict"])}}
    sh = shadow_records()
    settled = [r for r in sh if r.get("result")]
    fwd_sh = [r for r in settled if r["opened_ts"] >= SINCE]
    hedged = [r["result"]["hedged_pct"] for r in fwd_sh]
    half = len(hedged) // 2
    sh_halves = [round(sum(hedged[:half]) / half, 3) if half else None, round(sum(hedged[half:]) / (len(hedged) - half), 3) if len(hedged) - half else None]
    st = _stats(hedged)
    if len(hedged) < VERDICT_N:
        sh_verdict = "measuring"
    else:
        sh_verdict = "long leg real" if st["mean_pct"] >= MIN_GAP_PCT and (st["t"] or 0) >= 2 and all(h is not None and h > 0 for h in sh_halves) else "no edge"
    shadow = {"hold_h": SHADOW_HOLD_H, "written": len(sh), "settled": len(settled),
              "coin": _stats([r["result"]["coin_pct"] for r in settled]), "hedged": _stats([r["result"]["hedged_pct"] for r in settled]),
              "forward": {"n": len(fwd_sh), "hedged": st, "halves_pct": sh_halves}, "verdict": sh_verdict, "needed": VERDICT_N,
              "recent": sorted(sh, key=lambda r: -r["opened_ts"])[:20]}
    return {"since": SINCE, "verdict_n": VERDICT_N, "min_gap_pct": MIN_GAP_PCT, "books": BOOKS, "recorded": len(rows),
            "long": long_out, "short_cells": cells, "reversal_long_shadow": shadow,
            "recent": sorted(rows, key=lambda r: -r["opened_ts"])[:40],
            "pending": sum(len([t for t in closed_trades(b) if t["id"] not in {r["trade_id"] for r in rows}]) for b in BOOKS)}
