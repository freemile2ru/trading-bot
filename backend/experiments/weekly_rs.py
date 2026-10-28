"""Weekly near-high — every Monday, long the coins nearest their 7-day high, short those furthest below (paper).

Why (2026-10-24, research/signal_rebuild_lab.py): a year of 4h data on 39 perps, ranking coins by how far
they sit below their 7-day high and holding a long-top-5 / short-bottom-5 book for a week, made +1.36% a
week after costs — +1.23 in the first half, +1.48 in the second, 95% range +0.18…+2.62, 67% of weeks up.
It's a known effect (strength measured from the recent high). Caveats: shuffled-label luck reached +1.02%
a week, and the 40 coins were picked with hindsight — so it's a paper forward test, judged by the usual
pre-set verdict. Trading the same ranking every 4h lost money: the edge only survives low turnover.

The book (its own paper wallet, books.WALLETS["weekly_rs"]):
  rebalance  Monday ≥ 00:00 UTC, once a week: score = close / 7-day high − 1 on 4h candles; long the TOP
             highest, short the TOP lowest; positions already on the right side are kept
  size       NOTIONAL_PCT of equity each (5 long + 5 short = 100% gross, roughly market-neutral)
  exits      the next rebalance, or a DISASTER_PCT stop on the position; nothing else
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from storage import state as kv
from trading import book_switches, monitor

logger = logging.getLogger("experiments.weekly_rs")

BOOK = "weekly_rs"
TOP = 5
# 25% a name since 2026-10-29 (was 10): ten names → 2.5× gross, the most the 30% disaster stop allows (a position's
# liquidation must sit beyond its stop, which caps leverage at 3×). The research ran at 10% / 2×.
NOTIONAL_PCT = float(os.getenv("WEEKLY_RS_NOTIONAL_PCT", "25"))
DISASTER_PCT = float(os.getenv("WEEKLY_RS_DISASTER_PCT", "30"))
LOOKBACK_BARS = 42                     # 7 days of 4h candles
# A 30% stop leaves the automatic leverage at 1×, so ten positions would need the whole balance as margin.
# At 2× liquidation sits ~49% away — well beyond the stop.
LEVERAGE = int(os.getenv("WEEKLY_RS_LEVERAGE", "3"))             # per-position leverage: 3× is the cap for a 30% stop
UNIVERSE = ["ETHUSDT", "SOLUSDT", "QNTUSDT", "ZECUSDT", "XRPUSDT", "NEARUSDT", "SUIUSDT", "WLDUSDT", "DOGEUSDT",
            "HYPEUSDT", "ENAUSDT", "UNIUSDT", "TAOUSDT", "PUMPUSDT", "BNBUSDT", "AVAXUSDT", "1000PEPEUSDT", "ARBUSDT",
            "BCHUSDT", "ONDOUSDT", "LINKUSDT", "FILUSDT", "ADAUSDT", "DASHUSDT", "LTCUSDT", "AAVEUSDT", "PENGUUSDT",
            "TRUMPUSDT", "XLMUSDT", "DOTUSDT", "RUNEUSDT"]


def _week_key(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return kv.key("weekly_rs", "done", f"{y}-W{w:02d}")


async def scores() -> dict[str, float]:
    """close / 7-day high − 1 for each coin (0 = at its high), from 4h candles."""
    # Perpetual candles: the research ranked perps, and the book trades them (spot prices differ, and some of
    # these coins — 1000PEPE — don't exist on spot at all).
    from data.binance_client import fetch_futures_klines
    out = {}
    for sym in UNIVERSE:
        try:
            bars = await fetch_futures_klines(sym, "4h", LOOKBACK_BARS + 2)
        except Exception as e:
            logger.debug("Weekly RS: no candles for %s (%s)", sym, e)
            continue
        if len(bars) < LOOKBACK_BARS + 1:
            continue
        import time as _t
        closed = [b for b in bars if b["close_time"] / 1000 < _t.time()]          # the forming candle isn't a close
        window = closed[-LOOKBACK_BARS:]
        out[sym] = window[-1]["close"] / max(b["high"] for b in window) - 1
    return out


async def rebalance(force: bool = False, now: datetime | None = None) -> dict | None:
    """Monday's rebalance. Returns what changed, or None when it's not due."""
    from experiments import books
    now = now or datetime.now(timezone.utc)
    if not books.ENABLED or not book_switches.enabled(BOOK):
        return None
    if not force and (now.weekday() != 0 or kv.get_sync(_week_key(now))):
        return None
    sc = await scores()
    if len(sc) < 2 * TOP + 2:
        logger.warning("Weekly RS: only %d coins scored — rebalance skipped", len(sc))
        return None
    ranked = sorted(sc, key=sc.get)
    want = {s: "short" for s in ranked[:TOP]} | {s: "long" for s in ranked[-TOP:]}
    w = books.WALLETS[BOOK]
    held = {t["symbol"]: t for t in w.get_portfolio()["open_trades"]}
    closed, opened = [], []
    for sym, t in held.items():
        if want.get(sym) != t["direction"]:
            price = await monitor.live_price(sym) or t["current_price"]
            if w.close_trade_manually(t["id"], price, "rebalance"):
                closed.append(sym)
    from trading import capital
    equity = capital.sizing_equity(BOOK, w.get_portfolio()["total_equity"])
    for sym, d in want.items():
        if sym in held and held[sym]["direction"] == d:
            continue
        price = await monitor.live_price(sym)
        if not price:
            continue
        s = 1 if d == "long" else -1
        t = w.open_trade(symbol=sym, direction=d, entry_price=price, stop_loss=price * (1 - s * DISASTER_PCT / 100),
                         take_profit_1=price * (1 + s * 0.9), take_profit_2=price * (1 + s * 0.95),
                         notional_usd=equity * NOTIONAL_PCT / 100, timeframe="1w", leverage=LEVERAGE,
                         extra={"strategy": BOOK, "exit_policy": "manual", "shadows": False, "claude_verdict": "skipped",
                                "score_from_7d_high": round(sc[sym], 4)})
        if not t.get("error"):
            opened.append(f"{d} {sym}")
    kv.set_sync(_week_key(now), {"at": time.time(), "closed": closed, "opened": opened}, 14 * 86400)
    logger.info("Weekly RS rebalanced: closed %s · opened %s", closed or "nothing", opened or "nothing")
    return {"closed": closed, "opened": opened, "scores": {s: round(v, 4) for s, v in sc.items()}}
