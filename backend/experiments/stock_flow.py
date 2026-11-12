"""Stock flow shadow — a recorder of positioning on Binance's US stock perps, and two scores judged forward (2026-11-12).

Why: the user asked why the stock side failed while crypto looked promising, and to close the gap.
research/stock_reversal_lab.py found that the two were never asked the same question, and that the stock side lacks
the one thing the crypto score was built from — a recorded history of positioning. Binance keeps 30 days of open
interest, taker flow and top-trader share; our scanner has recorded those for coins since 2026-09-27, but never for
stocks. On the 30 days there are:
  * the crypto reversal score has no edge on the 40 most-traded stock perps (IC +0.003) — nor, in the same 30 days, on
    the 40 most-traded coins (IC −0.02);
  * on stocks, crowding reverses like crypto (open-interest growth, funding), but flow and price CONTINUE (taker buying
    IC +0.11, 24h return +0.04) — the opposite of the crypto signs, so the crypto score cancels itself out;
  * a stock-signed score chosen on the first half made +0.5%/slot on the second, but with t ≈ 0.5 on eleven days.

So this records, every 4 hours, each of the STOCKS most-traded US stock perps' readings (as in the lab), and once a
snapshot is 24 hours old scores two pre-registered long/short books (top 5 / bottom 5, 24h, 8 bps a side, funding):
  crypto   the crypto lab's signs (autotrader/reversal.py)
  stock    the signs chosen on the lab's first half: ret_24h +, oi_change_24h_pct +, funding_rate −, taker_buy_sell_24h +
Nothing trades. Verdict, fixed now: at VERDICT_DAYS of scored marks, a score "works" when its mean slot return is
positive after costs in both halves and the one-slot-a-day t ≥ 2. The recorded readings also let the lab be rerun later
on a history longer than Binance's 30 days.
"""
from __future__ import annotations

import logging
import math
import os
import time

from storage import state as kv

logger = logging.getLogger("experiments.stock_flow")

N_NAMES = int(os.getenv("STOCK_FLOW_NAMES", "40"))
TOP_K = 5
COST_BPS = 8.0
MARK_S = 4 * 3600
VERDICT_DAYS = 45
KEEP_MARKS = 6 * 400
SCORES = {
    "crypto": {"ret_24h": -1.0, "ret_7d": -1.0, "oi_change_24h_pct": -1.0, "oi_change_7d_pct": -1.0, "volume_change_24h": -1.0,
               "funding_rate": -1.0, "taker_buy_sell_24h": -1.0, "top_trader_long_pct": 1.0},
    "stock": {"ret_24h": 1.0, "oi_change_24h_pct": 1.0, "funding_rate": -1.0, "taker_buy_sell_24h": 1.0},
}
SINCE = 1794441600.0                                                   # 2026-11-12 00:00 UTC
_last = {"mark": 0}


def _k(*parts: str) -> str:
    return kv.key("stock_flow", *parts)


async def _get(path: str, **params):
    from data.http import get_json
    return await get_json(f"https://fapi.binance.com{path}", params=params)


async def universe() -> list[str]:
    info = await _get("/fapi/v1/exchangeInfo")
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in await _get("/fapi/v1/ticker/24hr")}
    syms = [s["symbol"] for s in info["symbols"] if s["contractType"] == "TRADIFI_PERPETUAL" and s["status"] == "TRADING"
            and (s.get("underlyingType") or "EQUITY") == "EQUITY" and not any("pre" in x.lower() for x in (s.get("underlyingSubType") or []))]
    return sorted(syms, key=lambda s: -vol.get(s, 0))[:N_NAMES]


def _chg(a: float | None, b: float | None) -> float | None:
    return (a / b - 1) * 100 if a and b else None


async def reading(sym: str, mark: float) -> dict | None:
    """The lab's readings for one perp at `mark` (an hour boundary), from the hours that closed before it."""
    end = int(mark * 1000) - 1
    k = await _get("/fapi/v1/klines", symbol=sym, interval="1h", endTime=end, limit=170)
    if not k or len(k) < 169:
        return None
    oi = await _get("/futures/data/openInterestHist", symbol=sym, period="1h", endTime=end, limit=170) or []
    tk = await _get("/futures/data/takerlongshortRatio", symbol=sym, period="1h", endTime=end, limit=24) or []
    tp = await _get("/futures/data/topLongShortPositionRatio", symbol=sym, period="1h", endTime=end, limit=1) or []
    pi = await _get("/fapi/v1/premiumIndex", symbol=sym) or {}
    close = [float(r[4]) for r in k]
    qv = [float(r[7]) for r in k]
    oiv = [float(r["sumOpenInterestValue"]) for r in oi]
    buy, sell = sum(float(r["buyVol"]) for r in tk), sum(float(r["sellVol"]) for r in tk)
    return {"symbol": sym, "price": close[-1],
            "ret_24h": _chg(close[-1], close[-25]), "ret_7d": _chg(close[-1], close[-169]),
            "oi_change_24h_pct": _chg(oiv[-1], oiv[-25]) if len(oiv) >= 25 else None,
            "oi_change_7d_pct": _chg(oiv[-1], oiv[-169]) if len(oiv) >= 169 else None,
            "volume_change_24h": _chg(sum(qv[-24:]), sum(qv[-48:-24])),
            "funding_rate": float(pi["lastFundingRate"]) if pi.get("lastFundingRate") not in (None, "") else None,
            "taker_buy_sell_24h": buy / sell if sell else None,
            "top_trader_long_pct": float(tp[-1]["longAccount"]) if tp else None}


def score(rows: list[dict], signs: dict[str, float]) -> dict[str, float]:
    """Each reading's cross-sectional percentile rank, signed, averaged over the readings a row has."""
    parts: dict[str, list[float]] = {r["symbol"]: [] for r in rows}
    for col, sign in signs.items():
        have = sorted((r[col], r["symbol"]) for r in rows if r.get(col) is not None)
        n = len(have)
        if n < 8:
            continue
        for i, (_, sym) in enumerate(have):
            parts[sym].append(sign * ((i + 0.5) / n - 0.5))
    return {s: sum(v) / len(v) for s, v in parts.items() if v}


def slot_return(rows: list[dict], fwd: dict[str, tuple[float, float]], signs: dict[str, float], k: int = TOP_K) -> float | None:
    """Long the top k / short the bottom k of the score; fwd[sym] = (24h % move, % funding a long paid). Net of costs."""
    sc = score([r for r in rows if r["symbol"] in fwd], signs)
    if len(sc) < 2 * k + 2:
        return None
    ranked = sorted(sc, key=sc.get)
    lo, hi = ranked[:k], ranked[-k:]
    long_leg = sum(fwd[s][0] - fwd[s][1] for s in hi) / k
    short_leg = sum(-fwd[s][0] + fwd[s][1] for s in lo) / k
    return (long_leg + short_leg) / 2 - 2 * COST_BPS / 100


async def _forward(sym: str, mark: float, price: float) -> tuple[float, float] | None:
    k = await _get("/fapi/v1/klines", symbol=sym, interval="1h", startTime=int((mark + 24 * 3600) * 1000), limit=1)
    if not k:
        return None
    fr = await _get("/fapi/v1/fundingRate", symbol=sym, startTime=int(mark * 1000), endTime=int((mark + 24 * 3600) * 1000), limit=100) or []
    return (float(k[0][1]) / price - 1) * 100, sum(float(r["fundingRate"]) for r in fr) * 100


async def record(mark: float) -> int:
    rows = []
    for sym in await universe():
        try:
            r = await reading(sym, mark)
        except Exception as e:
            logger.debug("Stock flow reading for %s failed: %s", sym, e)
            r = None
        if r:
            rows.append(r)
    if rows:
        kv.set_sync(_k("snap", str(int(mark))), {"mark": mark, "rows": rows})
        idx = kv.get_sync(_k("index")) or []
        if int(mark) not in idx:
            kv.set_sync(_k("index"), (idx + [int(mark)])[-KEEP_MARKS:])
    return len(rows)


async def settle(now: float) -> int:
    """Score every snapshot whose 24 hours have passed and that has no result yet."""
    done = 0
    for m in kv.get_sync(_k("index")) or []:
        snap = kv.get_sync(_k("snap", str(m)))
        if not snap or snap.get("result") or m + 25 * 3600 > now:
            continue
        fwd = {}
        for r in snap["rows"]:
            try:
                f = await _forward(r["symbol"], m, r["price"])
            except Exception:
                f = None
            if f:
                fwd[r["symbol"]] = f
        snap["result"] = {name: slot_return(snap["rows"], fwd, signs) for name, signs in SCORES.items()}
        snap["result"]["names"] = len(fwd)
        kv.set_sync(_k("snap", str(m)), snap)
        done += 1
    return done


async def tick(now: float | None = None) -> None:
    now = now or time.time()
    mark = now // MARK_S * MARK_S
    if mark <= _last["mark"] or now - mark > 1800:                    # once per 4h mark, within its first half hour
        return
    _last["mark"] = mark
    try:
        n = await record(mark)
        s = await settle(now)
        logger.info("Stock flow: %d readings at %s, %d snapshots scored", n, time.strftime("%m-%d %H:%M", time.gmtime(mark)), s)
    except Exception as e:
        logger.warning("Stock flow tick failed: %s", e)


def _stats(xs: list[float]) -> dict:
    n = len(xs)
    if n < 2:
        return {"n": n, "mean_pct": round(xs[0], 3) if xs else None}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return {"n": n, "mean_pct": round(m, 3), "t": round(m / sd * math.sqrt(n), 2) if sd else None, "pos_pct": round(100 * sum(x > 0 for x in xs) / n)}


def report() -> dict:
    snaps = [s for s in (kv.get_sync(_k("snap", str(m))) for m in (kv.get_sync(_k("index")) or [])) if s]
    scored = [s for s in snaps if s.get("result")]
    out = {"since": SINCE, "recorded_marks": len(snaps), "scored_marks": len(scored), "verdict_days": VERDICT_DAYS,
           "last_mark": snaps[-1]["mark"] if snaps else None, "names_last": len(snaps[-1]["rows"]) if snaps else 0, "scores": {}}
    days = len({int(s["mark"] // 86400) for s in scored})
    for name in SCORES:
        xs = [(s["mark"], s["result"][name]) for s in scored if s["result"].get(name) is not None]
        vals = [x for _, x in xs]
        daily = [x for m, x in xs if int(m) % 86400 == 0]                       # one non-overlapping slot a day (00:00 UTC)
        half = len(vals) // 2
        st = _stats(vals)
        dt = _stats(daily)
        halves = [round(sum(vals[:half]) / half, 3) if half else None, round(sum(vals[half:]) / (len(vals) - half), 3) if len(vals) - half else None]
        if days < VERDICT_DAYS:
            v = "measuring"
        else:
            v = "works" if (dt.get("t") or 0) >= 2 and all(h is not None and h > 0 for h in halves) else "no edge"
        out["scores"][name] = {"signs": SCORES[name], "all": st, "daily": dt, "halves_pct": halves, "days": days, "verdict": v}
    return out
