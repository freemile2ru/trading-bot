"""Burst book — two rules after a 5% five-minute burst, each on its own paper wallet, pre-registered 2026-11-06.

Why: the ZEC spike that stopped the reversal lane (2026-11-06) asked whether rapid moves can be traded. The spike lab
(research/spike_lab.py: 40 Binance crypto perps, 45 days of minute bars, 423 bursts of ≥5% in 5 minutes at ≥4σ on 3×
volume) found no pre-registered rule that pays; split by direction AFTER the fact, two long rules stood out:
  burst_up    ride an UP-burst: long 15 minutes after it, hold 4 hours — +165 bps a trade after costs, but the median
              trade −42 bps and 48% winners: a few runners carry the average (t 1.5, both halves positive, 70% of coins)
  burst_dip   buy the dip after a DOWN-burst: long 60 minutes after it, hold 4 hours — +112 bps, median +44, 54% winners
              (t 1.3, both halves positive, 62% of coins); the cascade book's rule on a wider universe
Both held against BTC's move over the same hours (BTC was flat), and both swung by hundreds of bps week to week.
They were chosen by looking at that data, so this is a forward test, not a finding: each wallet is judged on its own
40 trades by the usual verdict (trading/verdicts.py); a rule that passes becomes an autotrader lane the usual way and
reaches production only on that record. Nothing here runs on the production instance (not promoted).

The rule, as the lab ran it:
  universe   the UNIVERSE_N most-traded crypto perps on Binance by 24-hour volume, refreshed daily (stock, ETF and
             commodity perps excluded)
  burst      at a CLOSED minute, the last 5 minutes moved ≥ THR_PCT %, at least SIGMA_X × the trailing day's standard
             deviation of 5-minute moves, on ≥ VOL_X × the trailing day's median 5-minute volume; one burst per coin
             per COOLDOWN_MIN minutes (the first counts); the lab's filter, line for line
  entry      long at market DELAY minutes after the burst's minute closed (a fire missed by more than LATE_S is dropped,
             never entered late); LEG_PCT of the wallet's equity a leg, at most MAX_OPEN open per wallet
  exit       HOLD_H hours after entry (expires_at), or a STOP_PCT disaster stop. The lab's rules carried no stop at all
             (a 2% stop closed three quarters of the up-burst legs before the runners ran); the wallet needs one, so it
             is wide enough never to matter inside four hours short of a crash. No profit lock: the books stay on the
             plain manual policy locally so the forward record is the lab's rule and nothing else. Every exit policy
             (trail, the protect family, the profit lock) is shadowed beside each entry from 2026-11-07, so the
             40-trade verdict can say whether a trailed exit would have beaten hold-to-expiry without changing the rule.
"""
from __future__ import annotations

import logging
import os
import time

import numpy as np
import pandas as pd

from storage import state as kv
from trading import book_switches, monitor

logger = logging.getLogger("experiments.burst")

BOOKS: dict[str, dict] = {
    "burst_up": {"bursts": 1, "delay_min": 15, "label": "Burst · ride the up-burst (+15m, 4h)"},
    "burst_dip": {"bursts": -1, "delay_min": 60, "label": "Burst · buy the dip (+60m, 4h)"},
}
THR_PCT = float(os.getenv("BURST_THR_PCT", "5"))
SIGMA_X, VOL_X, WINDOW, COOLDOWN_MIN = 4.0, 3.0, 1440, 60
HOLD_H = float(os.getenv("BURST_HOLD_HOURS", "4"))
LEG_PCT = float(os.getenv("BURST_LEG_PCT", "5"))
MAX_OPEN = int(os.getenv("BURST_MAX_OPEN", "6"))
STOP_PCT = float(os.getenv("BURST_STOP_PCT", "15"))           # a disaster stop only: the lab's rules had none
LEVERAGE = 2
UNIVERSE_N = int(os.getenv("BURST_UNIVERSE", "40"))
LATE_S = 600
RULE_SINCE = 1793980800.0                                     # 2026-11-06 16:00 UTC: registered before the first trade
PENDING_KEY = kv.key("burst", "pending")
UNIVERSE_KEY = kv.key("burst", "universe")
FAPI = "https://fapi.binance.com/fapi/v1"
_bars: dict[str, pd.DataFrame] = {}
_last_eval: dict[str, int] = {}


async def universe(now: float | None = None) -> list[str]:
    """The lab's universe: the most-traded USDT crypto perps on Binance (underlyingType COIN), cached for a day."""
    from data.http import get_json
    now = now or time.time()
    cached = kv.get_sync(UNIVERSE_KEY)
    if cached and now - cached.get("at", 0) < 86400 and cached.get("symbols"):
        return list(cached["symbols"])
    try:
        info = await get_json(f"{FAPI}/exchangeInfo")
        tick = await get_json(f"{FAPI}/ticker/24hr")
    except Exception as e:
        logger.warning("Burst universe not refreshed (%s)", e)
        return list((cached or {}).get("symbols") or [])
    crypto = {s["symbol"] for s in info["symbols"]
              if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
              and s.get("underlyingType") == "COIN"}
    rows = sorted((r for r in tick if r["symbol"] in crypto), key=lambda r: -float(r["quoteVolume"]))
    syms = [r["symbol"] for r in rows[:UNIVERSE_N]]
    kv.set_sync(UNIVERSE_KEY, {"at": now, "symbols": syms}, 7 * 86400)
    return syms


async def _fetch(symbol: str, start_ms: int, limit: int = 1500) -> list[list]:
    from data.http import get_json
    return await get_json(f"{FAPI}/klines", params={"symbol": symbol, "interval": "1m", "startTime": start_ms, "limit": limit})


async def bars_for(symbol: str, now_ms: int) -> pd.DataFrame:
    """The coin's last WINDOW+ closed minute bars (o, h, l, c, v), kept in memory and topped up each tick."""
    cur = _bars.get(symbol)
    start = int(cur.index[-1]) + 60_000 if cur is not None and len(cur) else now_ms - (WINDOW + 10) * 60_000
    rows = await _fetch(symbol, start) if start < now_ms - 60_000 else []
    if rows:
        new = pd.DataFrame([[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows],
                           columns=["t", "o", "h", "l", "c", "v"]).set_index("t")
        new = new[new.index + 60_000 <= now_ms]                            # closed minutes only
        cur = new if cur is None else pd.concat([cur, new[~new.index.isin(cur.index)]])
        _bars[symbol] = cur = cur.iloc[-(WINDOW + 10):]
    return cur if cur is not None else pd.DataFrame(columns=["o", "h", "l", "c", "v"])


def trigger(df: pd.DataFrame) -> dict | None:
    """The lab's burst test on the last closed minute: the move, its size in trailing-day sigmas, and volume vs the median."""
    if len(df) < WINDOW // 2 + 6:
        return None
    c = df["c"]
    ret5 = c / c.shift(5) - 1
    sigma = float(ret5.iloc[:-1].rolling(WINDOW, min_periods=WINDOW // 2).std().iloc[-1])
    vol5 = df["v"].rolling(5).sum()
    vmed = float(vol5.iloc[:-1].rolling(WINDOW, min_periods=WINDOW // 2).median().iloc[-1])
    move, vol = float(ret5.iloc[-1]), float(vol5.iloc[-1])
    if not (sigma == sigma and vmed == vmed) or sigma <= 0 or vmed <= 0:
        return None
    if abs(move) < THR_PCT / 100 or abs(move) < SIGMA_X * sigma or vol < VOL_X * vmed:
        return None
    return {"dir": 1 if move > 0 else -1, "move_pct": round(move * 100, 2), "sigma_x": round(abs(move) / sigma, 1), "vol_x": round(vol / vmed, 1)}


def pending() -> list[dict]:
    return list(kv.get_sync(PENDING_KEY) or [])


async def tick(now: float | None = None) -> list[dict]:
    """Every minute: test each coin's last closed minute for a burst, queue the rules' entries, fire the ones due."""
    from experiments import books
    now = now or time.time()
    active = [b for b in BOOKS if book_switches.enabled(b)]
    if not books.ENABLED or not active:
        return []
    now_ms = int(now * 1000)
    queue = pending()
    queued = {(q["book"], q["symbol"]) for q in queue}
    syms = await universe(now)
    if not _bars:
        logger.info("Burst book: watching %d coins for %s", len(syms), " and ".join(BOOKS[b]["label"] for b in active))
    for sym in syms:
        try:
            df = await bars_for(sym, now_ms)
        except Exception as e:
            logger.warning("Burst bars for %s unavailable (%s)", sym, e)
            continue
        if not len(df) or _last_eval.get(sym) == int(df.index[-1]):
            continue
        _last_eval[sym] = int(df.index[-1])
        hit = trigger(df)
        if not hit or kv.get_sync(kv.key("burst", "cd", sym)):
            continue
        kv.set_sync(kv.key("burst", "cd", sym), 1, COOLDOWN_MIN * 60)
        closed_at = int(df.index[-1]) / 1000 + 60
        for book, rule in BOOKS.items():
            if rule["bursts"] != hit["dir"] or book not in active or (book, sym) in queued:
                continue
            queue.append({"book": book, "symbol": sym, "trigger_ts": closed_at, "fire_at": closed_at + rule["delay_min"] * 60, **hit})
            logger.info("Burst on %s: %+.2f%% in 5 min (%.1fσ, %.1f× volume) — %s queued for +%d min", sym, hit["move_pct"], hit["sigma_x"], hit["vol_x"], book, rule["delay_min"])
    opened, keep = [], []
    for q in queue:
        if now < q["fire_at"]:
            keep.append(q)
            continue
        book = q["book"]
        w = books.WALLETS[book]
        held = {t["symbol"] for t in w.get_portfolio()["open_trades"]}
        mine = sum(1 for t in opened if t.get("strategy") == book)
        if now - q["fire_at"] > LATE_S or book not in active or q["symbol"] in held or len(held) + mine >= MAX_OPEN:
            continue                                                       # late, off, already held, or the wallet is full
        price = await monitor.live_price(q["symbol"])
        if not price:
            keep.append(q)
            continue
        equity = w.get_portfolio()["total_equity"] or 1
        t = w.open_trade(symbol=q["symbol"], direction="long", entry_price=price, stop_loss=price * (1 - STOP_PCT / 100),
                         take_profit_1=price * 1.5, take_profit_2=price * 1.9, notional_usd=equity * LEG_PCT / 100,
                         timeframe="4h", oracle_confidence=0, leverage=LEVERAGE,
                         oracle_reasoning=f"{BOOKS[book]['label']}: {q['move_pct']:+.2f}% in 5 min ({q['sigma_x']}σ, {q['vol_x']}× volume), "
                                          f"entered {BOOKS[book]['delay_min']} min after", hold_duration=f"{HOLD_H:g}h",
                         extra={"strategy": book, "exit_policy": "manual", "shadows": True, "claude_verdict": "skipped", "entry_type": "market",
                                "near_event": __import__("ops.calendar", fromlist=["near_event"]).near_event(q["symbol"]),
                                "burst_move_pct": q["move_pct"], "burst_sigma_x": q["sigma_x"], "burst_vol_x": q["vol_x"],
                                "burst_trigger_ts": q["trigger_ts"], "entry_delay_min": BOOKS[book]["delay_min"], "rule": "v1"})
        if t.get("error"):
            logger.info("Burst %s %s refused: %s", book, q["symbol"], t["error"])
            continue
        w.annotate(t["id"], expires_at=now + HOLD_H * 3600)
        opened.append(t)
        logger.info("%s: long %s @ %s, %+.2f%% burst %d min ago", BOOKS[book]["label"], q["symbol"], price, q["move_pct"], BOOKS[book]["delay_min"])
    if keep or queue:
        kv.set_sync(PENDING_KEY, keep, 86400)
    return opened


def config() -> dict:
    return {"thr_pct": THR_PCT, "sigma_x": SIGMA_X, "vol_x": VOL_X, "cooldown_min": COOLDOWN_MIN, "hold_hours": HOLD_H, "leg_pct": LEG_PCT,
            "max_open": MAX_OPEN, "stop_pct": STOP_PCT, "universe_n": UNIVERSE_N, "rules": {b: {k: v for k, v in r.items()} for b, r in BOOKS.items()},
            "universe": list((kv.get_sync(UNIVERSE_KEY) or {}).get("symbols") or []), "pending": pending()}
