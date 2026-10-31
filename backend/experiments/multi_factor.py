"""Multi-factor basket — momentum, trend and funding carry held together, risk-weighted, on paper.

Why (2026-10-29, research/multi_factor_lab.py on 4 years × 40 perpetuals): each family alone failed the holdout
(test Sharpe 0.56 / 0.35 / 1.08). Held together with risk parity they are nearly uncorrelated and the test-half
Sharpe rose to 1.25 with a 26% drawdown against 40–50% for any family alone — but the 95% range of the daily
return (−1.1 … +14.2 bps) still touched zero, so under the written rule it was NOT promoted. Like Hold-72 before
it, this book tests it forward at a measuring size ($1k), judged by the usual 40-trade verdict.

The book (its own paper wallet, books.WALLETS["multi_factor"]), rebalanced once a day after 00:10 UTC:
  xsmom     every XS_HOLD days: long the XS_K coins with the best 30-day return, short the XS_K worst (0.5/K each)
  tsmom     daily: long each coin whose 10-day return is positive, sized to equal volatility (2.5%/day per coin,
            capped at its 1/N share); flat otherwise
  funding   every FUND_HOLD days: short the FUND_K coins with the highest last-day funding, long the FUND_K lowest
  combine   risk parity: each family scaled by 1 / its trailing 60-day return volatility (simulated on the same data)
  size      the combined weight × equity per coin at LEVERAGE; positions are only touched when the target flips side,
            disappears, or moves more than TOLERANCE from what is held (turnover is what kills these strategies)
  exits     the next rebalance, or a DISASTER_PCT stop; nothing else
"""
from __future__ import annotations

import asyncio
import logging
import os
import pickle
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from storage import state as kv
from trading import book_switches, monitor

logger = logging.getLogger("experiments.multi_factor")

BOOK = "multi_factor"
LABEL = "Multi-factor basket (momentum + trend + funding)"
XS_LOOKBACK, XS_HOLD, XS_K = 30, 3, 3
TS_LOOKBACK, TS_VOL_TARGET = 10, 0.025
FUND_LOOKBACK, FUND_HOLD, FUND_K = 1, 7, 8
RP_WINDOW = 60
WINDOW_DAYS = 150                                  # history fetched for the signals and the risk-parity volatilities
DISASTER_PCT = float(os.getenv("MULTI_FACTOR_DISASTER_PCT", "30"))
LEVERAGE = int(os.getenv("MULTI_FACTOR_LEVERAGE", "3"))      # 3× is the most a 30% stop allows
MIN_NOTIONAL = 10.0                                # on a $1k book the funding legs are ~$20 each; paper, so no exchange minimum
TOLERANCE = 0.25                                   # re-size a position only when its target moved this far
MIN_HISTORY = 100
_CACHE_PANEL = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "daily_4y_40c.pkl")
_state: dict = {"last": {}}


def universe() -> list[str]:
    """The lab's 40 perpetuals (the same coins the result was measured on); the weekly book's list if the cache is gone."""
    try:
        daily, _ = pickle.load(open(_CACHE_PANEL, "rb"))
        return sorted(daily.columns)
    except Exception:
        from experiments import weekly_rs
        return list(weekly_rs.UNIVERSE)


def _done_key(day: str) -> str:
    return kv.key(BOOK, "done", day)


async def panel(now: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily closes and daily summed funding for the universe, up to yesterday's close."""
    from data.derivatives_client import fetch_funding_range, fetch_klines_range
    end = int(now // 86400) * 86400 * 1000                 # today 00:00 UTC: everything before it is closed
    start = end - WINDOW_DAYS * 86400 * 1000
    closes, fund = {}, {}
    for i, sym in enumerate(universe()):
        try:
            k = await fetch_klines_range(sym, "1d", start, end - 1)
            f = await fetch_funding_range(sym, start, end - 1)
        except Exception as e:
            logger.debug("Multi-factor: no data for %s (%s)", sym, e)
            continue
        closes[sym] = pd.Series({pd.Timestamp(r[0], unit="ms", tz="UTC").normalize(): r[4] for r in k})
        fund[sym] = pd.Series({pd.Timestamp(r["time"], unit="ms", tz="UTC"): r["rate"] for r in f})
        if i % 10 == 9:
            await asyncio.sleep(0.2)
    daily = pd.DataFrame(closes).sort_index()
    daily = daily.where(daily > 0)
    funding = pd.DataFrame({s: f for s, f in fund.items() if len(f)}).resample("1D").sum().reindex(daily.index).fillna(0.0)
    return daily, funding


def _rank_weights(score: pd.Series, k: int) -> dict[str, float]:
    row = score.dropna()
    if len(row) < 2 * k:
        return {}
    ranked = row.sort_values()
    return {**{s: 0.5 / k for s in ranked.index[-k:]}, **{s: -0.5 / k for s in ranked.index[:k]}}


def family_targets(daily: pd.DataFrame, funding: pd.DataFrame, epoch_day: int, stored: dict) -> dict[str, dict[str, float]]:
    """Each family's target weights today (gross exposure 1). The rebalanced families keep their last targets
    between their rebalance days; `stored` carries those across days."""
    out = {}
    if epoch_day % XS_HOLD == 0 or not stored.get("xsmom"):
        out["xsmom"] = _rank_weights(daily.pct_change(XS_LOOKBACK).iloc[-1], XS_K)
    else:
        out["xsmom"] = stored["xsmom"]
    if epoch_day % FUND_HOLD == 0 or not stored.get("funding"):
        avg = funding.rolling(FUND_LOOKBACK).mean().iloc[-1].where(daily.iloc[-1].notna())
        out["funding"] = _rank_weights(-avg, FUND_K)                   # long the lowest funding, short the highest
    else:
        out["funding"] = stored["funding"]
    trend = np.sign(daily.pct_change(TS_LOOKBACK).iloc[-1]).clip(lower=0)
    vol = daily.pct_change().rolling(20).std().iloc[-1]
    listed = max(int(daily.iloc[-1].notna().sum()), 1)
    size = (TS_VOL_TARGET / vol).clip(upper=1.0) / listed
    ts = (trend * size).dropna()
    out["tsmom"] = {s: float(v) for s, v in ts.items() if v > 0}
    return out


def risk_parity(daily: pd.DataFrame, funding: pd.DataFrame, fams: dict[str, dict[str, float]]) -> tuple[dict[str, float], dict]:
    """Combine today's family targets by 1 / each family's trailing return volatility, simulated on the window."""
    from research import strategy_lab as SL
    sims = {
        "xsmom": SL.xsmom(daily, funding, XS_LOOKBACK, XS_HOLD, XS_K, 1),
        "tsmom": SL.tsmom(daily, funding, TS_LOOKBACK, "long_flat"),
        "funding": SL.funding_carry(daily, funding, FUND_LOOKBACK, FUND_HOLD, FUND_K),
    }
    inv = {}
    for name, w in sims.items():
        r = SL.run(w, daily, funding)
        sd = float(r.rolling(RP_WINDOW).std().iloc[-1]) if len(r) >= RP_WINDOW else float(r.std() or 0)
        inv[name] = 1.0 / sd if sd and sd > 0 else 0.0
    total = sum(inv.values()) or 1.0
    combined: dict[str, float] = {}
    for name, weights in fams.items():
        for sym, w in weights.items():
            combined[sym] = combined.get(sym, 0.0) + w * inv[name] / total
    return combined, {k: round(v / total, 3) for k, v in inv.items()}


def _held_notional(t: dict) -> float:
    return float(t.get("notional_usd") or (t.get("quantity") or 0) * (t.get("entry_price") or 0))


async def rebalance(force: bool = False, now: datetime | None = None) -> dict | None:
    """Once a day after 00:10 UTC: recompute the targets and trade the differences. Returns what changed."""
    from experiments import books
    from trading import capital
    now = now or datetime.now(timezone.utc)
    if not books.ENABLED or not book_switches.enabled(BOOK):
        return None
    day = now.strftime("%Y-%m-%d")
    if not force and ((now.hour == 0 and now.minute < 10) or kv.get_sync(_done_key(day))):
        return None
    ts = now.timestamp()
    daily, funding = await panel(ts)
    if len(daily) < MIN_HISTORY:
        logger.warning("Multi-factor: only %d days of data — rebalance skipped", len(daily))
        return None
    epoch_day = int(ts // 86400)
    stored = kv.get_sync(kv.key(BOOK, "family_targets")) or {}
    fams = family_targets(daily, funding, epoch_day, stored)
    kv.set_sync(kv.key(BOOK, "family_targets"), fams)
    weights, shares = risk_parity(daily, funding, fams)

    w = books.WALLETS[BOOK]
    equity = capital.sizing_equity(BOOK, w.get_portfolio()["total_equity"])
    held = {t["symbol"]: t for t in w.get_portfolio()["open_trades"]}
    opened, closed, kept = [], [], []
    for sym in sorted(set(weights) | set(held)):
        target = weights.get(sym, 0.0) * equity
        t = held.get(sym)
        if abs(target) < MIN_NOTIONAL:
            if t:
                price = await monitor.live_price(sym) or t["current_price"]
                if w.close_trade_manually(t["id"], price, "rebalance"):
                    closed.append(sym)
            continue
        direction = "long" if target > 0 else "short"
        if t and (t["direction"] != direction or abs(abs(target) - _held_notional(t)) > TOLERANCE * _held_notional(t)):
            price = await monitor.live_price(sym) or t["current_price"]
            if w.close_trade_manually(t["id"], price, "rebalance"):
                closed.append(sym)
            t = None
        if t:
            kept.append(sym)
            continue
        price = await monitor.live_price(sym)
        if not price:
            continue
        s = 1 if direction == "long" else -1
        out = w.open_trade(symbol=sym, direction=direction, entry_price=price, stop_loss=price * (1 - s * DISASTER_PCT / 100),
                           take_profit_1=price * (1 + s * 0.9), take_profit_2=price * (1 + s * 0.95), notional_usd=abs(target),
                           timeframe="1d", leverage=LEVERAGE,
                           extra={"strategy": BOOK, "exit_policy": "manual", "shadows": False, "claude_verdict": "skipped",
                                  "weight": round(weights.get(sym, 0.0), 4), "family_shares": shares})
        if not out.get("error"):
            opened.append(f"{direction} {sym}")
    gross = sum(abs(v) for v in weights.values())
    kv.set_sync(_done_key(day), {"at": time.time(), "opened": opened, "closed": closed}, 3 * 86400)
    _state["last"] = {"at": ts, "day": day, "opened": opened, "closed": closed, "kept": kept, "gross": round(gross, 3),
                      "family_shares": shares, "coins_with_data": int(daily.iloc[-1].notna().sum()),
                      "longs": sum(1 for v in weights.values() if v > 0), "shorts": sum(1 for v in weights.values() if v < 0)}
    logger.info("Multi-factor rebalanced: opened %s · closed %s · kept %d · gross %.2f · shares %s", opened or "nothing",
                closed or "nothing", len(kept), gross, shares)
    return _state["last"]


def status() -> dict:
    return {"last": _state["last"], "config": {"xsmom": f"{XS_LOOKBACK}d / top {XS_K} / every {XS_HOLD}d",
                                                "tsmom": f"{TS_LOOKBACK}d long-flat, {TS_VOL_TARGET:.1%} vol per coin",
                                                "funding": f"{FUND_LOOKBACK}d / {FUND_K} each side / every {FUND_HOLD}d",
                                                "risk_parity_window_d": RP_WINDOW, "leverage": LEVERAGE, "disaster_pct": DISASTER_PCT}}
