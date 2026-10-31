"""FastAPI routes for the Crypto Trading Oracle."""

import asyncio
import json
import logging
import time


from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from oracle.pipeline import run_oracle
from fastapi.responses import Response, StreamingResponse
from trading.paper_trader import (
    get_portfolio,
    reset_portfolio,
    export_trades_csv,
)
from autotrader import engine as autotrader
from trading import history, monitor, overview, regime
from data import cache, feeds
from scanner import bus, scheduler, watchlist

logger = logging.getLogger("api.routes")

router = APIRouter()

# ── Supported symbols ─────────────────────────────────────────────────────────
# Baseline allowlist. The scanner's universe follows the CMC top 10, which rotates
# (TRX, ZEC and HYPE are all in it as of writing), so validation also accepts any
# actively trading Binance USDT pair rather than only this fixed set.

VALID_SYMBOLS = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "MATICUSDT", "UNIUSDT",
}

VALID_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}

CACHE_TTL = cache.TTL["oracle"]


async def _tradeable_pairs() -> set[str]:
    pairs = await cache.get(cache.key("binance", "usdt_pairs"))
    return set(pairs) if pairs else set()


async def _validate_symbol(symbol: str) -> str:
    s = symbol.upper()
    if s in VALID_SYMBOLS:
        return s
    if s in await _tradeable_pairs():
        return s
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported symbol '{s}'. Must be an actively trading Binance USDT pair.",
    )


async def _cache_get(symbol: str) -> dict | None:
    return await cache.get(cache.key("oracle", symbol))


async def _cache_set(symbol: str, data: dict) -> None:
    await cache.set(cache.key("oracle", symbol), data, CACHE_TTL)


# ── Oracle endpoints ──────────────────────────────────────────────────────────

@router.get("/oracle/{symbol}")
async def get_oracle_signal(symbol: str):
    """Run the full oracle pipeline for a symbol. Results cached for 60s."""
    symbol = await _validate_symbol(symbol)
    cached = await _cache_get(symbol)
    if cached:
        return {**cached, "cached": True}
    try:
        result = await run_oracle(symbol)
        await _cache_set(symbol, result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Market data endpoints ─────────────────────────────────────────────────────

@router.get("/ohlcv/{symbol}")
async def get_ohlcv(
    symbol: str,
    interval: str = Query(default="1h"),
    limit: int = Query(default=100, ge=10, le=500),
):
    """Return OHLCV candles for charting."""
    symbol = await _validate_symbol(symbol)
    if interval not in VALID_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval '{interval}'. Valid: {sorted(VALID_INTERVALS)}")
    try:
        candles = await feeds.cached_ohlcv(symbol, interval, limit)
        return {
            "symbol": symbol,
            "interval": interval,
            "candles": [
                {
                    "time": c["open_time"],
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                    "volume": c["volume"],
                }
                for c in candles
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Binance fetch failed: {e}")


# ── Paper trading endpoints ───────────────────────────────────────────────────

@router.delete("/paper/trade/{trade_id}")
async def paper_close_trade(trade_id: str, current_price: float = Query(..., gt=0)):
    """Close a main-wallet trade — paper, or live (a real market order) if it's in the live ledger."""
    from live import swing as live_swing
    found = live_swing.find_trade("main", trade_id)
    trade = await asyncio.to_thread(found[0].close_trade_manually, trade_id, current_price) if found else None
    if not trade:
        raise HTTPException(status_code=404, detail=f"Trade '{trade_id}' not found")
    return trade


@router.get("/paper/portfolio")
async def paper_portfolio():
    return get_portfolio()


@router.post("/paper/update-prices")
async def paper_update_prices():
    """Walk open positions along the perpetual's minute bars now, rather than at the next sweep."""
    from live import swing as live_swing
    events = []
    for wallet in live_swing.wallets("main"):
        events += await monitor.run(wallet)
    prices = {t["symbol"]: t["current_price"] for w in live_swing.wallets("main") for t in w.get_portfolio()["open_trades"]}
    return {"prices_updated": prices,
            "auto_closed": [e["trade"] for e in events if e["type"] == "closed"]}


@router.post("/paper/reset")
async def paper_reset(starting_balance: float = Query(default=10000.0, gt=0)):
    return reset_portfolio(starting_balance)


@router.get("/paper/export")
async def paper_export_csv():
    """Download all trades as a CSV file."""
    content = export_trades_csv()
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=oracle_trades.csv"},
    )


# ── Oracle + auto-execute ─────────────────────────────────────────────────────

@router.post("/oracle/{symbol}/execute")
async def oracle_and_execute(
    symbol: str,
    risk_pct: float = Query(default=1.0, gt=0, le=3.0, description="% of equity lost if the stop is hit"),
    auto_size_pct: float | None = Query(default=None, ge=0.1, le=500.0,
                                        description="legacy: notional % of balance; overrides risk_pct"),
):
    """Run oracle then open a futures paper trade (market, at the perpetual's price) if
    confidence ≥ 60 and action is BUY/SELL. Sized by risk; leverage is chosen per position
    so liquidation sits well beyond the stop."""
    symbol = await _validate_symbol(symbol)
    from trading import book_switches
    if not book_switches.enabled("main"):
        raise HTTPException(409, "the main wallet is switched off on the Books tab — no new trades")
    result = await run_oracle(symbol)
    await _cache_set(symbol, result)

    oracle = result.get("oracle", {})
    rec = oracle.get("trade_recommendation", {})
    action = rec.get("action", "HOLD")
    confidence = rec.get("confidence", 0)

    if action == "HOLD" or confidence < 60:
        return {
            "message": f"No trade — action={action}, confidence={confidence}%",
            "oracle": oracle,
            "pipeline_status": result.get("pipeline_status"),
        }

    direction = rec.get("direction", "none")
    if direction not in ("long", "short"):
        return {"message": "No clear direction — skipped", "oracle": oracle}

    # A market order fills at the market — not at Claude's "optimal" entry, which may be
    # a level price hasn't reached. The wallet rejects levels that make no sense from here.
    market = await monitor.live_price(symbol) or result["signal_package"]["price"]
    from live import swing as live_swing
    wallet = live_swing.active("main")          # the live ledger when the main wallet is switched to live
    trade = await asyncio.to_thread(
        wallet.open_trade,
        symbol=symbol,
        direction=direction,
        entry_price=market,
        stop_loss=rec.get("stop_loss", 0),
        take_profit_1=rec.get("take_profit_1", 0),
        take_profit_2=rec.get("take_profit_2", 0),
        size_pct=auto_size_pct,
        risk_pct=None if auto_size_pct else risk_pct,
        timeframe=rec.get("timeframe", "1h"),
        oracle_confidence=confidence,
        oracle_reasoning=oracle.get("reasoning", {}).get("primary_thesis", ""),
        hold_duration=rec.get("hold_duration", "N/A"),
        extra={"claude_entry": rec.get("entry_price"),
               "fa_at_entry": autotrader._fa_snapshot(result["signal_package"].get("fundamentals")),
               "regime_at_entry": await regime.current()},
    )
    if trade.get("error"):
        return {"message": trade["error"], "oracle": oracle}
    kind = "LIVE trade opened on Binance" if trade.get("mode") == "live" else "Paper trade opened"
    return {"message": kind, "trade": trade, "oracle": oracle}


# ── Scanner / All Signals ─────────────────────────────────────────────────────

@router.get("/signals")
async def get_signals():
    """Latest scan snapshot, so the All Signals tab renders before the stream connects."""
    snapshot = await scheduler.get_snapshot()
    return {**snapshot, "scanner": scheduler.status()}


@router.get("/signals/coins")
async def coin_status():
    """Per coin: how many signals it has fired (24h / 7d / all time) and every open position it
    has right now in any system — main wallet, autotrader, scalper — paper or live."""
    from storage import signals as signal_log
    from trading import coin_lock

    def load():
        out: dict[str, dict] = {sym: {"signals": t, "trades": []} for sym, t in signal_log.fired_tally().items()}
        for sym, hs in coin_lock.holders().items():
            out.setdefault(sym, {"signals": None, "trades": []})["trades"] = hs
        return out
    return await asyncio.to_thread(load)


@router.get("/signals/watchlist")
async def get_watchlist():
    entries = await watchlist.get_all()
    return {
        "entries": sorted(
            entries.values(), key=lambda e: abs(e.get("conviction", 0)), reverse=True
        ),
        "max_size": watchlist.MAX_SIZE,
        "check_interval_seconds": scheduler.WATCHLIST_INTERVAL,
    }


@router.post("/signals/watchlist/{symbol}")
async def pin_watchlist(symbol: str):
    """Pin a symbol — exempt from auto-demotion and from the size cap."""
    symbol = await _validate_symbol(symbol)
    entry = await watchlist.pin(symbol)
    entries = await watchlist.get_all()
    bus.publish("watchlist", {"entries": list(entries.values()), "added": [entry], "removed": []})
    return entry


@router.delete("/signals/watchlist/{symbol}")
async def unpin_watchlist(symbol: str):
    symbol = await _validate_symbol(symbol)
    removed = await watchlist.unpin(symbol)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{symbol} is not on the watchlist")
    entries = await watchlist.get_all()
    bus.publish("watchlist", {
        "entries": list(entries.values()),
        "added": [],
        "removed": [{"symbol": symbol, "reason": "manually removed"}],
    })
    return {"message": f"{symbol} removed from watchlist"}


@router.post("/signals/scan")
async def force_scan():
    """Run a general sweep immediately instead of waiting for the next tick."""
    try:
        snapshot = await scheduler.run_general_sweep()
    except Exception as e:
        logger.exception("Manual sweep failed")
        raise HTTPException(status_code=500, detail=str(e))
    return {**snapshot, "scanner": scheduler.status()}


@router.get("/signals/stream")
async def stream_signals(request: Request):
    """Server-sent events: `scan`, `watch`, `signal`, `watchlist`, `trade_closed`,
    `auto` (autotrader summary) and `auto_trade` (autotrader open/close)."""

    async def events():
        # Seed immediately so a reconnecting client isn't blank until the next sweep.
        snapshot = await scheduler.get_snapshot()
        yield "retry: 5000\n\n"
        yield f"event: scan\ndata: {json.dumps(snapshot, default=str)}\n\n"

        with bus.subscribe() as queue:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"   # keeps proxies from closing an idle stream
                    continue
                if event is bus.CLOSE:
                    break   # server shutting down — let the client's EventSource reconnect
                payload = json.dumps(event["data"], default=str)
                yield f"event: {event['type']}\ndata: {payload}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Trade history & backtest (both wallets) ────────────────────────────────────

def _history_filters(
    symbol: str | None, direction: str | None, outcome: str | None, result: str | None,
    entry_type: str | None, date_from: float | None, date_to: float | None, q: str | None,
    fa: str | None = None,
) -> dict:
    return {
        "symbol": symbol, "direction": direction, "outcome": outcome, "result": result,
        "entry_type": entry_type, "date_from": date_from, "date_to": date_to, "q": q, "fa": fa,
    }


@router.get("/trades/history")
async def trade_history(
    wallet: str = Query("all", pattern="^(all|main|auto)$"),
    symbol: str | None = None,
    direction: str | None = Query(None, pattern="^(long|short)$"),
    outcome: str | None = None,
    result: str | None = Query(None, pattern="^(win|loss)$"),
    entry_type: str | None = Query(None, pattern="^(market|limit)$"),
    date_from: float | None = Query(None, description="epoch seconds, on close time"),
    date_to: float | None = Query(None, description="epoch seconds, on close time"),
    q: str | None = Query(None, max_length=100),
    fa: str | None = Query(None, pattern="^(with|neutral|against|unknown)$"),
    sort: str = "closed_at",
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    """Filtered, paginated closed trades plus a backtest over the whole filtered set."""
    filters = _history_filters(symbol, direction, outcome, result, entry_type, date_from, date_to, q, fa)
    return await asyncio.to_thread(history.query, wallet, filters, sort, order, page, page_size)


# Every system the Trades page can filter on — the trading books and the Lab's experiment books.
SYSTEM_PATTERN = "^((main|auto|scalper|impulse)(_live)?|auto_rules|signals_claude|signals_rules|claude_swing|hold72|fade24|events|weekly_rs|cascade|carry|carry_q|unlocks|vol_premium|copy_trading|copy_stocks|copy_futures|copy_(crypto|stocks|futures)_(wide|fast|late)|multi_factor|auction|(stocks|futures)_(trend|rules|claude))$"


@router.get("/trades/all")
async def trades_all(
    system: str | None = Query(None, pattern=SYSTEM_PATTERN),
    mode: str | None = Query(None, pattern="^(paper|live)$"),
    status: str | None = Query(None, pattern="^(open|closed)$"),
    symbol: str | None = None,
    direction: str | None = Query(None, pattern="^(long|short)$"),
    result: str | None = Query(None, pattern="^(win|loss)$"),
    exit_reason: str | None = None,
    date_from: float | None = Query(None, description="epoch seconds"),
    date_to: float | None = Query(None, description="epoch seconds"),
    sort: str = "closed_ts",
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    include_retired: bool = Query(False, description="include retired strategies' trades"),
):
    """Every trade from every system (main wallet, autotrader, scalper), open and closed."""
    filters = {"system": system, "mode": mode, "status": status, "symbol": symbol, "direction": direction, "result": result,
               "exit_reason": exit_reason, "date_from": date_from, "date_to": date_to, "include_retired": include_retired}
    return await asyncio.to_thread(overview.trades, filters, sort, order, page, page_size)


@router.get("/trades/analytics")
async def trades_analytics(
    system: str | None = Query(None, pattern=SYSTEM_PATTERN),
    mode: str | None = Query(None, pattern="^(paper|live)$"),
    symbol: str | None = None,
    direction: str | None = Query(None, pattern="^(long|short)$"),
    result: str | None = Query(None, pattern="^(win|loss)$"),
    exit_reason: str | None = None,
    date_from: float | None = Query(None, description="epoch seconds"),
    date_to: float | None = Query(None, description="epoch seconds"),
    include_retired: bool = Query(False, description="include retired strategies' trades"),
):
    """Charts and research for the closed trades matching the Trades page filters."""
    from trading import trade_analytics
    filters = {"system": system, "mode": mode, "symbol": symbol, "direction": direction, "result": result,
               "exit_reason": exit_reason, "date_from": date_from, "date_to": date_to, "include_retired": include_retired}
    return await asyncio.to_thread(trade_analytics.analytics, filters)


@router.get("/experiments/strategy-lab")
async def strategy_lab_results():
    """The latest research/strategy_lab.py run (portfolio strategies backtested walk-forward)."""
    import json
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "strategy_lab_results.json")
    if not os.path.exists(path):
        raise HTTPException(404, "not run yet — python -m research.strategy_lab --years 4")
    return await asyncio.to_thread(lambda: json.load(open(path)))


_signal_scores_cache: dict = {"at": 0.0, "data": None}
_signal_scores_lock = asyncio.Lock()


@router.get("/experiments/signal-scores")
async def signal_scores_view():
    """Does anything the scanner reads predict the next move? (labelled scan readings, cached an hour — labels refresh hourly)"""
    from research import signal_scores
    stale = time.time() - _signal_scores_cache["at"] > 3600 or _signal_scores_cache["data"] is None   # labels refresh hourly
    if stale and not _signal_scores_lock.locked():
        async with _signal_scores_lock:                  # one recompute at a time; others get the cached copy
            _signal_scores_cache.update(at=time.time(), data=await asyncio.to_thread(signal_scores.scores))
    elif _signal_scores_cache["data"] is None:
        async with _signal_scores_lock:                  # first ever request: wait for the one in flight
            pass
    return _signal_scores_cache["data"]


@router.get("/debriefs/summary")
async def debrief_summary(system: str | None = Query(None, pattern=SYSTEM_PATTERN)):
    """Why trades won or lost: tag counts, Claude's causes and recent lessons."""
    from trading import debrief
    return await asyncio.to_thread(debrief.summary, system)


@router.get("/debriefs/{system}/{trade_id}")
async def debrief_one(system: str, trade_id: str):
    """One trade's debrief (404 until its post-exit window has passed and it's been tagged)."""
    from storage import signals as signal_log
    d = await asyncio.to_thread(signal_log.debrief, system, trade_id)
    if d is None:
        raise HTTPException(404, "not debriefed yet")
    return d


@router.get("/experiments/retired")
async def retired_experiments():
    """Every experiment we stopped: hypothesis, method, evidence, why it failed, what was removed."""
    from experiments import retired
    return await asyncio.to_thread(retired.summary)


@router.get("/experiments")
async def experiments_summary(ledger: str = Query("both", pattern="^(both|paper|live)$")):
    """The Lab tab: every side experiment, the paired "what does Claude add" tests, and verdicts."""
    from experiments import lab
    return await asyncio.to_thread(lab.summary, ledger)


@router.post("/markets/{market}/trend/close-all")
async def markets_trend_close_all(market: str, reason: str = Query("retired", max_length=40)):
    """Close every open position of a market's trend book at the live price (used when the book is retired), then
    let the capital plan take the freed cash."""
    if market not in ("stocks", "futures"):
        raise HTTPException(404, "unknown market")
    from markets import engine as mk
    from trading import capital
    w = mk.WALLETS[f"{market}_trend"]
    out = []
    for t in w.get_portfolio()["open_trades"]:
        price = await monitor.live_price(t["symbol"]) or t.get("current_price")
        if not price:
            continue
        done = await asyncio.to_thread(w.close_trade_manually, t["id"], price, reason)
        if done:
            out.append({"symbol": t["symbol"], "pnl_usd": done["pnl_usd"]})
    moved = await asyncio.to_thread(capital.apply_plan)
    return {"closed": out, "net": round(sum(x["pnl_usd"] for x in out), 2), "moved": moved}


@router.post("/experiments/weekly-rs/resize")
async def weekly_rs_resize():
    """Bring the weekly book's open legs to their target size now (the Monday rebalance also does this). Oversized
    legs have the excess closed as its own row (reason "resize"); undersized legs are topped up."""
    from experiments import weekly_rs
    from trading import capital
    out = await weekly_rs.resize()
    capital.apply_plan(withdraw_only=True)          # hand the freed margin back to the plan at once
    return {"resized": out}


@router.post("/experiments/weekly-rs/restate")
async def weekly_rs_restate(target: float = Query(..., gt=0, description="the leg size to restate to, in USD notional")):
    """Restate the weekly book's closed record at the right leg size: legs closed at another size are scaled to
    `target`, and the cut-off parts of resized legs (reason "resize") are erased — a book that had always held
    the right size would never have had them."""
    from experiments import books
    from storage import signals
    w = books.WALLETS["weekly_rs"]
    closed = w.all_trades()["closed"]
    cut = {t["id"] for t in closed if t.get("close_reason") == "resize"}
    factors = {t["id"]: target / t["notional_usd"] for t in closed
               if t["id"] not in cut and t.get("notional_usd") and abs(t["notional_usd"] / target - 1) > 0.25}
    scaled = await asyncio.to_thread(w.rescale_closed_trades, factors, f"restated to ${target:,.0f} a leg")
    erased = await asyncio.to_thread(w.erase_closed_trades, cut, "resize parts")
    signals.delete_debriefs("weekly_rs", list(cut))
    return {"scaled": scaled, "erased": erased}


@router.post("/experiments/claude-swing/review")
async def claude_swing_review_now():
    """Run a Claude swing review now (counts against its daily call budget)."""
    from experiments import claude_swing
    from scanner import scheduler
    scans = scheduler.latest_full_scans()
    if not scans:
        raise HTTPException(409, "no scan yet — wait for the first sweep")
    from trading import book_switches
    if not book_switches.enabled("claude_swing"):
        raise HTTPException(409, "Claude swing is switched off on the Books tab")
    if await claude_swing.calls_today() >= claude_swing.DAILY_CALLS:
        raise HTTPException(429, f"Claude swing's {claude_swing.DAILY_CALLS} calls for today are used up")
    entry = await claude_swing.review(scans, await scheduler.shared_feeds())
    if entry is None:
        raise HTTPException(502, "the review failed — see the Lab tab's review log")
    return entry


@router.get("/portfolio/overview")
async def portfolio_overview(include_retired: bool = Query(False, description="include retired strategies")):
    """Performance, risk and exposure metrics per system and combined, with equity curves."""
    return await asyncio.to_thread(overview.portfolio, include_retired)


@router.get("/copy/leaders")
async def copy_leaders():
    """The copy-trading book: chosen leaders and their profiles, open copies, skipped entries and how they went."""
    from experiments import copy_trading
    return await asyncio.to_thread(copy_trading.status)


@router.get("/copy/log")
async def copy_log(leader_id: str | None = None, since: float | None = None, limit: int = Query(5000, le=50000)):
    """The copy-trading record: every leader position observed at every poll (for later study)."""
    from experiments import copy_trading
    return {"status": copy_trading.log_status(), "rows": await asyncio.to_thread(copy_trading.observations, leader_id, since, limit)}


@router.post("/copy/refresh")
async def copy_refresh():
    """Re-run the leader selection now (otherwise daily)."""
    from experiments import copy_trading
    if copy_trading._state.get("selecting"):
        raise HTTPException(409, "a leader selection is already running")
    copy_trading._state["selecting"] = True
    try:
        await copy_trading.select_leaders_off_loop()
    finally:
        copy_trading._state["selecting"] = False
    return await asyncio.to_thread(copy_trading.status)


@router.get("/portfolio/target")
async def portfolio_target():
    """The $/day tracker: every paper book's P&L per day vs the daily target, expected vs realised, breaker state."""
    from trading import target
    return await asyncio.to_thread(target.report)


@router.post("/portfolio/target/apply-plan")
async def portfolio_target_apply():
    """Bring every book to the plan in force (idempotent: deposits and withdrawals)."""
    from trading import capital, target
    done = await asyncio.to_thread(capital.apply_plan)
    return {"deposits": done, "report": await asyncio.to_thread(target.report, None, 0)}


@router.get("/portfolio/target/suggest")
async def portfolio_target_suggest():
    """What the evidence would move under the allocator's written rule (trading/allocator.py) — advisory only."""
    from trading import target
    return await asyncio.to_thread(target.suggestion)


@router.post("/portfolio/target/apply-suggestion")
async def portfolio_target_apply_suggestion():
    """Make the current suggestion the plan in force and move the money (same books, same total)."""
    from trading import capital, target
    sug = await asyncio.to_thread(target.suggestion)
    if not sug["changed"]:
        raise HTTPException(409, "the suggestion matches the plan already in force")
    capital.set_plan(sug["suggested"], "allocator suggestion applied from the Portfolio tab")
    done = await asyncio.to_thread(capital.apply_plan)
    return {"plan": capital.plan(), "moved": done, "report": await asyncio.to_thread(target.report, None, 0)}


@router.post("/portfolio/target/resume")
async def portfolio_target_resume():
    from trading import capital, target
    capital.resume()
    return await asyncio.to_thread(target.report, None, 0)


@router.get("/history/insights")
async def history_insights(wallet: str = Query("all", pattern="^(all|main|auto)$"),
                           ledger: str = Query("both", pattern="^(both|paper|live)$")):
    """Which decisions pay: closed swing trades grouped by what went into the entry."""
    return await asyncio.to_thread(history.insights, wallet, ledger)


@router.get("/exits/compare")
async def exits_compare(wallet: str = Query("all", pattern="^(all|main|auto)$"),
                        ledger: str = Query("both", pattern="^(both|paper|live)$")):
    """How each exit policy would have done on the same trades (shadow book)."""
    return await asyncio.to_thread(history.exit_comparison, wallet, ledger)


@router.get("/trades/all.csv")
async def trades_all_csv(
    system: str | None = Query(None, pattern=SYSTEM_PATTERN),
    mode: str | None = Query(None, pattern="^(paper|live)$"),
    status: str | None = Query(None, pattern="^(open|closed)$"),
    symbol: str | None = None,
    direction: str | None = Query(None, pattern="^(long|short)$"),
    result: str | None = Query(None, pattern="^(win|loss)$"),
    exit_reason: str | None = None,
    date_from: float | None = None,
    date_to: float | None = None,
    include_retired: bool = Query(False, description="include retired strategies' trades"),
):
    """The Trades page selection as CSV, every matching row (no paging)."""
    import csv
    import io
    from datetime import datetime, timezone
    from fastapi.responses import Response
    filters = {"system": system, "mode": mode, "status": status, "symbol": symbol, "direction": direction,
               "result": result, "exit_reason": exit_reason, "date_from": date_from, "date_to": date_to,
               "include_retired": include_retired}

    def load():
        rows = [r for r in overview.all_rows()[0] if overview.matches(r, filters)]
        return sorted(rows, key=lambda r: r["closed_ts"] or r["opened_ts"] or 0, reverse=True)
    rows = await asyncio.to_thread(load)
    cols = ["system", "symbol", "direction", "status", "opened_ts", "closed_ts", "hold_min", "entry_price", "exit_price",
            "stop_loss", "take_profit", "notional", "leverage", "margin", "risk_usd", "gross_pnl", "fees", "funding",
            "net_pnl", "r", "mfe_r", "exit_reason", "id"]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({**r, **{k: datetime.fromtimestamp(r[k], timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if r.get(k) else ""
                           for k in ("opened_ts", "closed_ts")}})
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="trades.csv"'})


@router.get("/trades/history.csv")
async def trade_history_csv(
    wallet: str = Query("all", pattern="^(all|main|auto)$"),
    symbol: str | None = None,
    direction: str | None = Query(None, pattern="^(long|short)$"),
    outcome: str | None = None,
    result: str | None = Query(None, pattern="^(win|loss)$"),
    entry_type: str | None = Query(None, pattern="^(market|limit)$"),
    date_from: float | None = None,
    date_to: float | None = None,
    q: str | None = Query(None, max_length=100),
    fa: str | None = Query(None, pattern="^(with|neutral|against|unknown)$"),
    sort: str = "closed_at",
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    filters = _history_filters(symbol, direction, outcome, result, entry_type, date_from, date_to, q, fa)
    data = await asyncio.to_thread(history.query, wallet, filters, sort, order, 1, 200)
    rows = data["rows"]
    for page in range(2, data["pages"] + 1):
        rows += (await asyncio.to_thread(history.query, wallet, filters, sort, order, page, 200))["rows"]
    return Response(
        content=history.to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trade_history.csv"},
    )


# ── Autotrader (autonomous experiment, separate wallet) ─────────────────────────

@router.get("/auto")
async def auto_summary():
    return await autotrader.summary()


@router.post("/auto/pause")
async def auto_pause(paused: bool = Query(...)):
    """Pausing stops new entries only — open positions still exit on stop/target/expiry."""
    await autotrader.set_paused(paused)
    summary = await autotrader.summary()
    bus.publish("auto", summary)
    return summary


@router.delete("/auto/trade/{trade_id}")
async def auto_close_trade(trade_id: str, reason: str = Query("manual", pattern="^(manual|dedup)$")):
    """Close an auto trade now. `reason` names why in the record: a hand close, or a de-duplication (the same signal
    held in another book)."""
    from live import swing as live_swing
    if not live_swing.find_trade("auto", trade_id):
        raise HTTPException(status_code=404, detail=f"No open auto trade {trade_id}")
    closed = await autotrader.close(trade_id, reason)
    if not closed:      # it exists, so the close itself was refused (a live one: its exchange stop stays)
        raise HTTPException(status_code=502, detail="close failed — for live positions check the Live tab's activity log")
    return closed


@router.delete("/auto/order/{order_id}")
async def auto_cancel_order(order_id: str):
    cancelled = await autotrader.cancel(order_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail=f"No pending auto order {order_id}")
    return cancelled


# ── Notifications ─────────────────────────────────────────────────────────────

# Per-symbol rather than global: with ten symbols being scanned, one global cooldown
# would let the first firing symbol mute every other alert for five minutes.
_NOTIFY_COOLDOWN = 300
_last_notify_ts: dict[str, float] = {}


class NotifySignalRequest(BaseModel):
    symbol: str
    action: str
    confidence: int
    entry_price: float
    stop_loss: float
    take_profit_1: float
    composite_score: float
    hold_duration: str
    primary_thesis: str = ""


@router.post("/notify/signal")
async def notify_signal(req: NotifySignalRequest):
    """Send email + SMS alert for a strong trade signal. Called by the frontend alarm."""
    from data.notifications import send_email, send_sms

    now = time.time()
    elapsed = now - _last_notify_ts.get(req.symbol, 0.0)
    if elapsed < _NOTIFY_COOLDOWN:
        remaining = int(_NOTIFY_COOLDOWN - elapsed)
        return {"email_sent": False, "sms_sent": False, "cooldown_remaining_seconds": remaining}

    _last_notify_ts[req.symbol] = now

    arrow = "▲" if req.action == "BUY" else "▼"
    subject = f"{arrow} {req.action} {req.symbol} — {req.confidence}% confidence | Crypto Oracle"
    body = (
        f"CRYPTO ORACLE TRADE ALERT\n"
        f"{'=' * 40}\n"
        f"Signal:      {req.action} {req.symbol}\n"
        f"Confidence:  {req.confidence}%\n"
        f"Score:       {req.composite_score:+.3f}\n"
        f"\n"
        f"Entry:       ${req.entry_price:,.2f}\n"
        f"Stop Loss:   ${req.stop_loss:,.2f}\n"
        f"Take Profit: ${req.take_profit_1:,.2f}\n"
        f"Hold:        {req.hold_duration}\n"
        f"\n"
        f"Thesis: {req.primary_thesis}\n"
    )
    sms_body = (
        f"{arrow} {req.action} {req.symbol} | {req.confidence}% conf | "
        f"Score {req.composite_score:+.3f} | "
        f"Entry ${req.entry_price:,.2f} | "
        f"Stop ${req.stop_loss:,.2f} | "
        f"TP ${req.take_profit_1:,.2f} | "
        f"{req.hold_duration}"
    )

    email_ok, sms_ok = await asyncio.gather(
        asyncio.to_thread(send_email, subject, body),
        asyncio.to_thread(send_sms, sms_body),
    )
    return {"email_sent": email_ok, "sms_sent": sms_ok, "cooldown_remaining_seconds": 0}
