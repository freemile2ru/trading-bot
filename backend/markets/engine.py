"""Scanning and trading the non-crypto books (see markets/__init__.py).

Every SWEEP_S the market's perps are scanned with the production TA, setups and conviction, built
from their own Binance candles (no news, FA or macro — those are crypto-specific here). Then:
  rules   if the autotrader's bias + timing rules say enter → a market entry with the autotrader's
          levels and risk sizing, the live exit policy ("protect"); one position per instrument
  claude  the same moment, but Claude must agree (autotrader thresholds) — at most
          CLAUDE_PER_SWEEP asks per sweep and MARKETS_CLAUDE_DAILY_CALLS a day per market
  trend   once a day after the US close: long while the 126-trading-day return of the underlying
          (Yahoo history — the perps are only months old) is positive, flat otherwise; sized to
          ~1% daily volatility per instrument, capped at 1/N; a 15% disaster stop; closed on a flip
Fills, stops, funding: the shared paper wallet + monitor, every minute.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import httpx
import pandas as pd

from storage import state as kv
from storage.db import SWING_DB
from trading import book_switches, exits, monitor
from trading.paper_trader import PaperWallet

logger = logging.getLogger("markets")

ENABLED = os.getenv("MARKETS_ENABLED", "true").lower() != "false"
SWEEP_S = int(os.getenv("MARKETS_SWEEP_S", "900"))
CLAUDE_DAILY = int(os.getenv("MARKETS_CLAUDE_DAILY_CALLS", "20"))
CLAUDE_PER_SWEEP = 2
MAX_OPEN = 8
COOLDOWN_S = 1800
TREND_DAYS = 126
# Daily volatility target per name (2026-10-27: 1% → 2.5%). At 1% the stock book split $10k over 23 names into ~$200
# positions and sat on $3.5k of exposure; 2.5% puts it near fully invested at the 3× the book allows, roughly the
# 10–15% annual volatility trend programmes usually run at. Applies to positions opened from the next rebalance.
# Sizing (2026-10-28): a name gets equity × VOL_TARGET / its 20-day volatility, capped so the names in an uptrend
# together hold at most TREND_GROSS × equity. The old rule divided equity by ALL names (20+) and so held ~$200 a
# name on a $10k book — wins of a few dollars whatever the trend did.
# 2.8× gross since 2026-10-29 (was 1.5×). The leverage round asked for 7–10×, but the 15% disaster stop caps a name's
# leverage at 3× (liquidation must sit 2× the stop distance away — trading/futures.safe_leverage), so the margin for
# anything past ~2.8× gross does not exist. A tighter stop would allow more; the whipsaw lab said tighter stops lose.
TREND_VOL_TARGET = float(os.getenv("MARKETS_TREND_VOL_TARGET", "0.35"))
TREND_GROSS = float(os.getenv("MARKETS_TREND_GROSS", "2.8"))
TREND_DISASTER = 0.15
# Hysteresis (2026-10-23): enter above +TREND_BAND, exit below −TREND_BAND. With a bare "> 0" rule the
# books flapped — GOOGL and AMZN out, in and out again within days. On 10 years of daily data
# (research/trend_whipsaw_lab.py: Dow-2016, 68 US, 15 Asian stocks, commodity ETFs) ±5% cut switches
# from 7–9 to 2–3 per asset a year with equal or better return and Sharpe in every universe and half.
TREND_BAND = float(os.getenv("MARKETS_TREND_BAND", "0.05"))
TREND_HOUR_UTC = 21                     # after the US close
# 2026-10-28, asked for: a trend position older than TAKE_PROFIT_DAYS that is up ≥ TAKE_PROFIT_ROE % on its full position
# size (the price move, leverage included) is closed ("profit_take"). It truncates the right tail a trend follower lives on —
# judged by the exit reason's record.
TAKE_PROFIT_ROE = float(os.getenv("MARKETS_TREND_TAKE_PROFIT_PCT", "25"))
TAKE_PROFIT_DAYS = float(os.getenv("MARKETS_TREND_TAKE_PROFIT_DAYS", "7"))
TAKE_PROFIT_BASIS = os.getenv("MARKETS_TREND_TAKE_PROFIT_BASIS", "price")    # "price": % of position size; "roe": return on margin
# Claude's daily picks: the strongest-conviction instruments go to Claude whether or not the rules
# say "enter", so the Claude book tests Claude's own judgement — not just a filter on rules we
# already know lose. Its entries are tagged entry_source="daily_pick" to keep them separable.
PICKS_PER_DAY = int(os.getenv("MARKETS_CLAUDE_PICKS", "4"))
PICK_HOUR_UTC = int(os.getenv("MARKETS_PICK_HOUR_UTC", "15"))     # US session under way

# Binance perpetual → the Yahoo ticker its trend signal is read from (for futures, the ETF holding the
# real commodity — its price includes the cost of rolling futures, like a perp's funding).
MARKETS: dict[str, dict] = {
    "stocks": {"label": "Stocks", "note": "US and Asian stock / ETF perpetual",
               "symbols": {f"{t}USDT": t for t in ("NVDA", "MU", "MRVL", "MSTR", "INTC", "GOOGL", "TSLA", "AMD", "AVGO",
                                                    "AAPL", "AMZN", "META", "COIN", "HOOD", "ORCL", "MSFT", "PLTR",
                                                    "TSM", "SPY", "QQQ")}
               | {"SKHYNIXUSDT": "000660.KS", "SAMSUNGUSDT": "005930.KS", "HK0700USDT": "0700.HK"}},
    "futures": {"label": "Futures", "note": "commodity perpetual",
                "symbols": {"XAUUSDT": "GLD", "XAGUSDT": "SLV", "XPTUSDT": "PPLT", "XPDUSDT": "PALL",
                            "COPPERUSDT": "CPER", "CLUSDT": "USO", "BZUSDT": "BNO", "NATGASUSDT": "UNG"}},
}
KINDS = {"trend": "Trend (6-month)", "rules": "Scanner rules", "claude": "Claude-confirmed"}
BOOK_NAMES = [f"{m}_{k}" for m in MARKETS for k in KINDS]
LABELS = {f"{m}_{k}": f"{MARKETS[m]['label']} · {v}" for m in MARKETS for k, v in KINDS.items()}
WALLETS: dict[str, PaperWallet] = {n: PaperWallet(SWING_DB, label=n, history_cap=3000, min_snapshot_seconds=120)
                                   for n in BOOK_NAMES}
_state: dict = {"scans": {}, "last_sweep": {}, "last_trend": {}}


# ── Scanning ───────────────────────────────────────────────────────────────────

TF_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}


async def _candles(symbol: str, tf: str) -> list[dict]:
    from data import cache
    from data.derivatives_client import fetch_klines_range

    async def load():
        end = int(time.time() * 1000)
        rows = await fetch_klines_range(symbol, tf, end - 200 * TF_MS[tf], end)
        return [{"open_time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in rows]
    return await cache.cached(cache.key("mkt_kl", symbol, tf), cache.OHLCV_TTL.get(tf, 300), load,
                              stale_ttl=cache.OHLCV_TTL.get(tf, 300) * 10)


async def scan(symbol: str, market: str) -> dict | None:
    from analysis.scoring import build_signal_package
    from analysis.technical import compute_multi_timeframe_ta
    from scanner.engine import _extract_triggers, compute_conviction
    from scanner.strategies import detect_setups
    ohlcv = {}
    for tf in TF_MS:
        try:
            ohlcv[tf] = await _candles(symbol, tf)
        except Exception as e:
            logger.debug("%s %s candles unavailable: %s", symbol, tf, e)
            ohlcv[tf] = []
    if len(ohlcv["15m"]) < 60:
        return None
    ta = compute_multi_timeframe_ta(ohlcv)
    pkg = build_signal_package(symbol=symbol, ta_result=ta, news={}, macro={}, fear_greed={"value": 50, "classification": "Neutral"},
                               social={}, coingecko={})
    price = await monitor.live_price(symbol) or ohlcv["15m"][-1]["close"]
    pkg["price"] = price
    pkg["asset_note"] = f"{symbol} is a Binance {MARKETS[market]['note']} (not a crypto coin): no news, FA or funding context is supplied."
    tf_signals = pkg["ta_summary"]["timeframe_signals"]
    setups = detect_setups(tf_signals, pkg.get("tf_alignment", {}))
    conv = compute_conviction(pkg["composite_score"], setups, 0.0)
    return {"symbol": symbol, "base": symbol.removesuffix("USDT"), "market": market, "price": price,
            "conviction": conv["conviction"], "conviction_detail": conv,
            "confidence": min(95, pkg.get("confidence_pct", 0) + 3 * len(conv["aligned_setups"])),
            "tf_alignment": pkg.get("tf_alignment", {}), "best_timeframe": pkg.get("best_timeframe"),
            "hold_duration": pkg.get("hold_duration"), "fa": None, "setups": setups, "signal_package": pkg,
            "triggers": _extract_triggers(tf_signals, pkg.get("best_timeframe", "1h"), (pkg.get("stop_loss_pct") or 0.0) / 1.5),
            "scanned_at": time.time()}


# ── Books ──────────────────────────────────────────────────────────────────────

def _open_symbols(w: PaperWallet) -> set[str]:
    return {t["symbol"] for t in w.get_portfolio()["open_trades"]}


def _claude_budget_key(market: str) -> str:
    return kv.key("markets", market, "claude", datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def claude_calls_today(market: str) -> int:
    return int(kv.get_sync(_claude_budget_key(market)) or 0)


async def _enter(book: str, scan_: dict, direction: str, fast: dict, extra: dict) -> dict | None:
    from autotrader import strategy
    w = WALLETS[book]
    price = await monitor.live_price(scan_["symbol"]) or scan_["price"]
    lv = strategy.build_levels(direction, price, scan_, fast)
    gross = sum(t.get("size_pct", 0) for t in w.get_portfolio()["open_trades"])
    size = strategy.position_size_pct(lv["sl_pct"], gross)
    if size < 1.0:
        return None
    t = await asyncio.to_thread(
        w.open_trade, symbol=scan_["symbol"], direction=direction, entry_price=price, stop_loss=lv["stop_loss"],
        take_profit_1=lv["take_profit_1"], take_profit_2=lv["take_profit_2"], size_pct=size,
        timeframe=scan_.get("best_timeframe") or "1h", hold_duration=scan_.get("hold_duration") or "N/A",
        extra={"strategy": book, "exit_policy": exits.DEFAULT_POLICY, "market": scan_["market"],
               "conviction_at_entry": scan_["conviction"],
               "setups_at_entry": scan_["conviction_detail"].get("aligned_setups", []), **extra})
    if t.get("error"):
        logger.debug("%s entry %s refused: %s", book, scan_["symbol"], t["error"])
        return None
    logger.info("%s opened %s %s @ %s", book, direction, scan_["symbol"], price)
    return t


async def _rules_and_claude(market: str, scans: list[dict]) -> None:
    from autotrader import strategy
    from autotrader.engine import VETO_SECONDS, _verdict
    from oracle.claude_oracle import synthesize_trade
    asked = 0
    for sc in sorted(scans, key=lambda s: -abs(s["conviction"])):
        sym = sc["symbol"]
        _, fast = strategy.fast_tf(sc)
        prev_key = kv.key("markets", market, "prev", sym)
        prev = kv.get_sync(prev_key)
        kv.set_sync(prev_key, strategy.compact_fast(fast), 86400)
        direction, why = strategy.bias(sc)
        sc["rules"] = {"direction": direction, "reasons": why}
        if not direction or not fast:
            continue
        t = strategy.timing(direction, fast, prev, sc)
        sc["rules"].update(score=t["score"], enter=t["enter"], timing=t["reasons"])
        if not t["enter"]:
            continue
        for kind in ("rules", "claude"):
            book = f"{market}_{kind}"
            w = WALLETS[book]
            if not book_switches.enabled(book) or sym in _open_symbols(w) or len(_open_symbols(w)) >= MAX_OPEN:
                continue
            if kv.get_sync(kv.key("markets", book, "cd", sym)):
                continue
            if kind == "rules":
                await _enter(book, sc, direction, fast, {"claude_verdict": "skipped"})
                continue
            if asked >= CLAUDE_PER_SWEEP or claude_calls_today(market) >= CLAUDE_DAILY - _picks_reserve(market) or \
                    kv.get_sync(kv.key("markets", book, "veto", sym)):
                continue
            asked += 1
            await kv.incr_with_expire(_claude_budget_key(market), 172800)
            res = await synthesize_trade(sc["signal_package"], {})
            if res.get("error"):
                logger.warning("%s: Claude failed for %s — %s", book, sym, res["error"])
                continue
            rec = res.get("trade_recommendation", {})
            ok, note = _verdict(rec.get("action", "HOLD"), int(rec.get("confidence") or 0), direction)
            sc["rules"]["claude"] = note
            if ok:
                await _enter(book, sc, direction, fast, {"claude_verdict": "approved", "claude_confidence": rec.get("confidence"),
                                                         "claude_thesis": (res.get("reasoning") or {}).get("primary_thesis", "")})
            else:
                kv.set_sync(kv.key("markets", book, "veto", sym), {"note": note, "at": time.time()}, VETO_SECONDS)


def _picks_key(market: str) -> str:
    return kv.key("markets", "picks", market, datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def _picks_reserve(market: str) -> int:
    """Claude calls held back for today's daily picks until they've run."""
    return 0 if kv.get_sync(_picks_key(market)) or not book_switches.enabled(f"{market}_claude") else PICKS_PER_DAY


async def claude_picks(market: str, scans: list[dict], force: bool = False) -> list[dict]:
    """Once a day after PICK_HOUR_UTC: ask Claude about the PICKS_PER_DAY strongest-conviction
    instruments and enter wherever it calls BUY or SELL with enough confidence."""
    from autotrader import strategy
    from autotrader.engine import MIN_CLAUDE_CONFIDENCE
    from oracle.claude_oracle import synthesize_trade
    book, now = f"{market}_claude", datetime.now(timezone.utc)
    done_key = _picks_key(market)
    if not force and (now.hour < PICK_HOUR_UTC or kv.get_sync(done_key)):
        return []
    if not book_switches.enabled(book) or not scans:
        return []
    kv.set_sync(done_key, {"at": time.time()}, 172800)
    w, picks = WALLETS[book], []
    for sc in sorted(scans, key=lambda s: -abs(s["conviction"])):
        if len(picks) >= PICKS_PER_DAY or claude_calls_today(market) >= CLAUDE_DAILY:
            break
        sym = sc["symbol"]
        if sym in _open_symbols(w) or kv.get_sync(kv.key("markets", book, "cd", sym)):
            continue
        _, fast = strategy.fast_tf(sc)
        if not fast:
            continue
        await kv.incr_with_expire(_claude_budget_key(market), 172800)
        res = await synthesize_trade(sc["signal_package"], {})
        if res.get("error"):
            logger.warning("%s: Claude pick failed for %s — %s", book, sym, res["error"])
            continue
        rec = res.get("trade_recommendation", {})
        action, conf = rec.get("action", "HOLD"), int(rec.get("confidence") or 0)
        direction = {"BUY": "long", "SELL": "short"}.get(action)
        pick = {"symbol": sym, "base": sym.removesuffix("USDT"), "conviction": sc["conviction"], "action": action,
                "confidence": conf, "entered": False, "at": time.time(),
                "thesis": ((res.get("reasoning") or {}).get("primary_thesis") or "")[:400]}
        if direction and conf >= MIN_CLAUDE_CONFIDENCE and len(_open_symbols(w)) < MAX_OPEN:
            t = await _enter(book, sc, direction, fast, {"claude_verdict": "approved", "claude_confidence": conf,
                                                         "entry_source": "daily_pick", "claude_thesis": pick["thesis"]})
            pick["entered"] = bool(t)
        picks.append(pick)
    kv.set_sync(kv.key("markets", "picks", market, "last"), {"at": time.time(), "picks": picks}, 7 * 86400)
    logger.info("%s: Claude's daily picks — %s", book, ", ".join(f"{p['base']} {p['action']} {p['confidence']}%" for p in picks) or "none")
    return picks


async def _yahoo_daily(ticker: str) -> pd.Series | None:
    from data import cache

    async def load():
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as c:
            # Explicit dates: Yahoo silently returns monthly bars for long "range" requests.
            r = await c.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                            params={"period1": int(time.time()) - 450 * 86400, "period2": int(time.time()), "interval": "1d"})
        j = r.json()["chart"]["result"][0]
        adj = (j["indicators"].get("adjclose") or [{}])[0].get("adjclose") or j["indicators"]["quote"][0]["close"]
        return [[t, v] for t, v in zip(j["timestamp"], adj) if v is not None]
    rows = await cache.cached(cache.key("mkt_yahoo", ticker), 6 * 3600, load)
    if not rows or len(rows) < TREND_DAYS + 25:
        return None
    return pd.Series([v for _, v in rows], index=pd.to_datetime([t for t, _ in rows], unit="s"))


async def trend_rebalance(market: str, force: bool = False) -> list[str]:
    """Once a day: long what's trending up (above +TREND_BAND), flat once it's trending down (below −TREND_BAND).
    Returns what changed."""
    book = f"{market}_trend"
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    if not force and (now.hour < TREND_HOUR_UTC or _state["last_trend"].get(market) == day):
        return []
    _state["last_trend"][market] = day
    w = WALLETS[book]
    held = {t["symbol"]: t for t in w.get_portfolio()["open_trades"]}
    syms = MARKETS[market]["symbols"]
    changes = []
    from trading import capital
    equity = capital.sizing_equity(book, w.get_portfolio()["total_equity"])
    # First pass: every name's trend and volatility, so sizing knows how many are long this rebalance.
    stats: dict[str, tuple[float, bool, float]] = {}
    for perp, yt in syms.items():
        try:
            s = await _yahoo_daily(yt)
        except Exception as e:
            logger.debug("Trend data for %s unavailable: %s", yt, e)
            continue
        if s is None:
            continue
        trend = float(s.iloc[-1] / s.iloc[-1 - TREND_DAYS] - 1)
        up = trend > -TREND_BAND if perp in held else trend > TREND_BAND
        stats[perp] = (trend, up, float(s.pct_change().iloc[-20:].std()))
    n_long = sum(1 for _, up, _ in stats.values() if up) or 1
    for perp, (trend, up, vol) in stats.items():
        if perp in held and not up:
            price = await monitor.live_price(perp) or held[perp]["current_price"]
            await asyncio.to_thread(w.close_trade_manually, held[perp]["id"], price, "trend_flip")
            changes.append(f"closed {perp}")
        elif up and perp not in held and book_switches.enabled(book):
            from ops import calendar
            why = calendar.earnings_block(perp) if market == "stocks" else None
            if why:
                logger.info("%s: not opening %s — %s", book, perp, why)
                continue
            price = await monitor.live_price(perp)
            if not price or not vol:
                continue
            notional = min(equity * TREND_VOL_TARGET / vol, equity * TREND_GROSS / n_long)
            t = await asyncio.to_thread(
                w.open_trade, symbol=perp, direction="long", entry_price=price, stop_loss=price * (1 - TREND_DISASTER),
                take_profit_1=price * 1.5, take_profit_2=price * 2.0, notional_usd=notional, timeframe="1d",
                extra={"strategy": book, "exit_policy": "manual", "shadows": False, "market": market, "claude_verdict": "skipped",
                       "trend_return_pct": round(trend * 100, 2), "near_event": calendar.near_event(perp)})
            if not t.get("error"):
                changes.append(f"opened {perp}")
    if changes:
        logger.info("%s rebalanced: %s", book, ", ".join(changes))
    return changes


async def sweep(market: str) -> None:
    scans = []
    for sym in MARKETS[market]["symbols"]:
        try:
            sc = await scan(sym, market)
        except Exception as e:
            logger.debug("Scan %s failed: %s", sym, e)
            continue
        if sc:
            scans.append(sc)
    await _rules_and_claude(market, scans)
    await claude_picks(market, scans)
    await trend_rebalance(market)
    _state["scans"][market] = [{k: v for k, v in s.items() if k not in ("signal_package", "triggers")} for s in scans]
    _state["last_sweep"][market] = time.time()


async def run() -> None:
    """The scheduler's markets loop (every SWEEP_S)."""
    if not ENABLED:
        return
    for m in MARKETS:
        try:
            await sweep(m)
        except Exception as e:
            logger.warning("Markets sweep %s failed: %s", m, e)


_trend_shadows_cleared = False


async def monitor_all() -> None:
    """Every minute: fills, stops, targets and funding; a closed trade starts the coin's cooldown."""
    global _trend_shadows_cleared
    if not ENABLED:
        return
    if not _trend_shadows_cleared:
        # Trend positions opened before 2026-10-19 got intraday exit-policy shadows; stamped "1d" they'd run
        # up to 60 days, fetching minute bars every minute for nothing. Trend books never need shadows.
        _trend_shadows_cleared = True
        for name, w in WALLETS.items():
            if name.endswith("_trend"):
                n = await asyncio.to_thread(w.drop_shadows)
                if n:
                    logger.info("%s: dropped %d leftover exit-policy shadows", name, n)
    for name, w in WALLETS.items():
        try:
            for ev in await monitor.run(w):
                if ev["type"] == "closed":
                    kv.set_sync(kv.key("markets", name, "cd", ev["trade"]["symbol"]), {"at": time.time()}, COOLDOWN_S)
            if name.endswith("_trend"):
                for t in await asyncio.to_thread(take_profits, w):
                    kv.set_sync(kv.key("markets", name, "cd", t["symbol"]), {"at": time.time()}, COOLDOWN_S)
        except Exception as e:
            logger.warning("Monitoring %s failed: %s", name, e)


def take_profits(w: PaperWallet, now: float | None = None) -> list[dict]:
    """Close trend positions older than TAKE_PROFIT_DAYS that are up ≥ TAKE_PROFIT_ROE % of position size."""
    now = now or time.time()
    out = []
    for t in w.get_portfolio()["open_trades"]:
        opened = _ts(t.get("opened_at"))
        if opened is None or now - opened < TAKE_PROFIT_DAYS * 86400:
            continue
        roe = t.get("roe_pct") if TAKE_PROFIT_BASIS == "roe" else t.get("pnl_pct")
        if roe is None or roe < TAKE_PROFIT_ROE or not t.get("current_price"):
            continue
        done = w.close_trade_manually(t["id"], t["current_price"], "profit_take")
        if done:
            out.append(done)
            logger.info("%s: profit take on %s — ROI %+.1f%% after %.0f days", w.label, t["symbol"], roe, (now - opened) / 86400)
    return out


def _ts(iso) -> float | None:
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(iso)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def summary(market: str) -> dict:
    books = []
    for kind, label in KINDS.items():
        name = f"{market}_{kind}"
        p = WALLETS[name].get_portfolio(closed_limit=200, history_limit=300)
        closed = p["closed_trades"]
        rs = [t["pnl_usd"] / t["risk_usd"] for t in closed if t.get("risk_usd")]
        books.append({"name": name, "kind": kind, "label": label, "enabled": book_switches.enabled(name),
                      "equity": p["total_equity"], "initial": p["initial_balance"], "balance": p["balance"],
                      "return_pct": round((p["total_equity"] / p["initial_balance"] - 1) * 100, 2) if p["initial_balance"] else None,
                      "open": p["open_trades"], "closed": len(closed),
                      "wins": sum(1 for t in closed if t["pnl_usd"] > 0),
                      "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
                      "net_pnl": round(sum(t["pnl_usd"] for t in closed), 2),
                      "recent_closed": list(reversed(closed[-10:])),
                      "equity_history": p.get("equity_history", [])[-300:]})
    return {"market": market, "label": MARKETS[market]["label"], "symbols": list(MARKETS[market]["symbols"]),
            "books": books, "scans": sorted(_state["scans"].get(market, []), key=lambda s: -abs(s["conviction"])),
            "last_sweep": _state["last_sweep"].get(market), "sweep_s": SWEEP_S,
            "claude_calls_today": claude_calls_today(market), "claude_daily": CLAUDE_DAILY,
            "last_trend": _state["last_trend"].get(market), "enabled": ENABLED,
            "picks": kv.get_sync(kv.key("markets", "picks", market, "last")), "picks_per_day": PICKS_PER_DAY,
            "pick_hour_utc": PICK_HOUR_UTC}
