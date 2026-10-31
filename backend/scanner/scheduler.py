"""The two scan tiers, run as asyncio loops for the lifetime of the app.

  * general sweep  — every 5 min, all ten universe symbols, full timeframes
  * watchlist sweep — every 1 min, watched symbols only, fast timeframes

Both tiers read through the Redis cache, so the 1-minute tier costs roughly one Binance
klines call per symbol; everything else is still warm from the 5-minute sweep.
"""

import asyncio
import logging
import os
import time

from autotrader import engine as autotrader
from trading import monitor, position_manager
from data import feeds
from storage import signals as signal_log
from storage import state
from data.coinmarketcap_client import get_tradeable_pool
from scanner import bus, escalation, selection, watchlist
from scanner.engine import scan_symbol

logger = logging.getLogger("scanner.scheduler")

GENERAL_INTERVAL = 300
WATCHLIST_INTERVAL = 60
UNIVERSE_SIZE = int(os.getenv("SCANNER_UNIVERSE_SIZE", "10"))   # best N tradeable, scanned each sweep
POOL_SIZE = int(os.getenv("SCANNER_POOL_SIZE", "30"))           # top-N by market cap, ranked for tradeability
CONCURRENCY = 4          # be polite to Binance/CoinGecko on a ten-symbol fan-out
SIGNAL_LIVE_SECONDS = escalation.SIGNAL_LIVE_SECONDS

SNAPSHOT_KEY = state.key("scan", "snapshot")
SNAPSHOT_TTL = 3600

_tasks: list[asyncio.Task] = []
_state: dict = {"last_general": 0.0, "last_watchlist": 0.0, "universe": [], "btc": {}}

# Serialise each tier against itself so a manual POST /signals/scan can't run
# concurrently with the scheduled sweep and double-publish to the stream.
_locks: dict[str, asyncio.Lock] = {"general": asyncio.Lock(), "watchlist": asyncio.Lock()}


async def get_snapshot() -> dict:
    return await state.get(SNAPSHOT_KEY) or {
        "rows": [], "fired": [], "watchlist": [], "updated_at": None, "universe_size": 0,
    }


def _strip(scan: dict) -> dict:
    """Drop the full signal package before it goes over the wire or into the snapshot.

    The package is several KB of nested indicator output per symbol; the table needs
    none of it, and the drilldown already has /api/oracle/{symbol}.
    """
    return {k: v for k, v in scan.items() if k != "signal_package"}


async def universe_size() -> int:
    """How many coins this sweep scans: the usual UNIVERSE_SIZE, or the autotrader's wider net when it has been dry."""
    try:
        dry = await asyncio.to_thread(autotrader.dry_mode)
    except Exception as e:
        logger.debug("dry-mode check failed: %s", e)
        return UNIVERSE_SIZE
    return max(UNIVERSE_SIZE, dry["universe"] or 0) if dry.get("active") else UNIVERSE_SIZE


async def _must_scan() -> set[str]:
    """Symbols scanned every sweep regardless of tradeability rank."""
    entries = await watchlist.get_all()
    pinned = {s for s, e in entries.items() if e.get("pinned")}
    try:
        from live import swing as live_swing
        main_open = {t["symbol"] for w in live_swing.wallets("main") for t in w.get_portfolio()["open_trades"]}
    except Exception:
        main_open = set()
    # Autotrader positions are not here on purpose: leaving the ten (and so the
    # watchlist) is an exit signal for them, not something to protect against.
    # Autotrader positions stay scanned after leaving the watchlist so their flip checks
    # and position reviews continue — leaving now tightens the stop rather than closing.
    from experiments import books as experiment_books
    return pinned | main_open | autotrader.open_symbols() | experiment_books.open_symbols() | await live_signal_symbols()


async def live_signal_symbols() -> set[str]:
    """Symbols whose fired signal is still within its tradeable window.

    Mirrors STALE_AFTER_SECONDS in the frontend — these stay on the watchlist so a live
    signal is never hidden by the watchlist-filtered view.
    """
    cutoff = time.time() - SIGNAL_LIVE_SECONDS
    return {
        f["symbol"] for f in await escalation.recent_fired()
        if f.get("fired_at", 0) >= cutoff
    }


def latest_full_scans() -> list[dict]:
    """The last general sweep's scans with their signal packages (for on-demand reviews)."""
    return list(_state.get("full_scans") or [])


async def shared_feeds() -> dict:
    return await _shared_feeds()


async def _shared_feeds() -> dict:
    """Symbol-independent data, fetched once and shared by every symbol in the sweep."""
    macro, fear_greed, global_market, stablecoins = await asyncio.gather(
        feeds.cached_macro(),
        feeds.cached_fear_greed(),
        feeds.cached_global_market(),
        feeds.cached_stablecoins(),
        return_exceptions=True,
    )
    pool = _state.get("pool") or []
    return {
        "macro": {} if isinstance(macro, Exception) else macro,
        "fear_greed": (
            {"value": 50, "classification": "Neutral"}
            if isinstance(fear_greed, Exception) else fear_greed
        ),
        "global_market": {} if isinstance(global_market, Exception) else global_market,
        "stablecoins": {} if isinstance(stablecoins, Exception) else stablecoins,
        # The whole top-N pool's returns, so each coin's move can be ranked against its peers.
        "pool_changes": {
            "24h": [c["percent_change_24h"] for c in pool if c.get("percent_change_24h") is not None],
            "7d": [c["percent_change_7d"] for c in pool if c.get("percent_change_7d") is not None],
        },
    }


async def _scan_many(universe: list[dict], shared: dict, *, light: bool) -> list[dict]:
    # BTC is the FA benchmark even when it isn't among the selected ten.
    btc = next((c for c in universe if c["symbol"] == "BTC"), None) or _state.get("btc") or {}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(coin: dict) -> dict | None:
        async with semaphore:
            try:
                scan = await scan_symbol(coin["pair"], coin, btc, shared, light=light)
                scan["tradeability"] = coin.get("tradeability")
                scan["tradeability_rank"] = coin.get("tradeability_rank")
                scan["tradeability_detail"] = coin.get("tradeability_detail")
                scan["held_only"] = bool(coin.get("held_only"))
                return scan
            except Exception as e:
                logger.warning("Scan failed for %s: %s", coin.get("pair"), e)
                return None

    results = await asyncio.gather(*(one(c) for c in universe))
    return [r for r in results if r]


async def _escalate_all(scans: list[dict], shared: dict) -> list[dict]:
    """Highest conviction first, so a limited daily budget buys the best setups."""
    fired = []
    for scan in sorted(scans, key=lambda s: abs(s.get("conviction", 0)), reverse=True):
        try:
            result = await escalation.try_escalate(scan, shared.get("global_market", {}))
        except Exception as e:
            logger.error("Escalation failed for %s: %s", scan["symbol"], e)
            continue
        if result:
            fired.append(result)
            bus.publish("signal", result)
    return fired


async def _persist(scans: list[dict]) -> dict:
    entries = await watchlist.get_all()
    snapshot = {
        "rows": [_strip(s) for s in sorted(
            scans, key=lambda s: abs(s.get("conviction", 0)), reverse=True
        )],
        "fired": await escalation.recent_fired(),
        "watchlist": list(entries.values()),
        "updated_at": time.time(),
        "universe_size": len([s for s in scans if not s.get("held_only")]),
        "pool_size": _state.get("pool_size", 0),
        "universe_target": _state.get("universe_target", UNIVERSE_SIZE),
        "selection": [
            {"symbol": c["pair"], "base": c["symbol"], "tradeability": c["tradeability"],
             "rank": c["tradeability_rank"], "selected": c["tradeability_rank"] <= _state.get("universe_target", UNIVERSE_SIZE),
             "range_pct": c["tradeability_detail"]["range_pct"],
             "quote_volume": c["tradeability_detail"]["quote_volume"]}
            for c in _state.get("ranking", [])
        ],
        "next_sweep_at": time.time() + GENERAL_INTERVAL,
        "llm_budget_used": await escalation.budget_used(),
        "llm_budget_total": escalation.DAILY_BUDGET,
    }
    await state.set(SNAPSHOT_KEY, snapshot, SNAPSHOT_TTL)
    return snapshot


async def run_general_sweep() -> dict:
    """Full sweep across the CMC top-10. Safe to call directly for a manual refresh."""
    async with _locks["general"]:
        return await _general_sweep()


async def _general_sweep() -> dict:
    t0 = time.perf_counter()
    logger.info("── General sweep starting ──")

    size = await universe_size()
    wide = size > UNIVERSE_SIZE
    pool = await get_tradeable_pool(max(POOL_SIZE, size * 3) if wide else POOL_SIZE)
    _state["btc"] = next((c for c in pool if c["symbol"] == "BTC"), {})
    _state["pool"] = pool
    _state["universe_target"] = size
    incumbents = {c["pair"] for c in _state.get("universe", []) if not c.get("held_only")}
    must = await _must_scan()
    # A position the autotrader opened on the wider dry-market net is outside the usual pool once the
    # net narrows again; its flip checks and reviews need the coin scanned until it's closed.
    missing = autotrader.open_symbols() - {c["pair"] for c in pool}
    if missing and not wide:
        try:
            extra = [c for c in await get_tradeable_pool(max(POOL_SIZE, autotrader.DRY_UNIVERSE * 3)) if c["pair"] in missing]
            pool = pool + extra
        except Exception as e:
            logger.warning("Wider pool for held coins %s unavailable: %s", sorted(missing), e)
    universe, ranking = await selection.select(
        pool, size, incumbents, must,
        min_volume=selection.DRY_MIN_QUOTE_VOLUME if wide else selection.MIN_QUOTE_VOLUME,
    )
    _state["universe"] = universe
    _state["ranking"] = ranking
    _state["pool_size"] = len(pool)

    shared = await _shared_feeds()
    scans = await _scan_many(universe, shared, light=False)
    if not scans:
        logger.error("General sweep produced no results")
        return await get_snapshot()

    previous = {r["symbol"]: r for r in (await get_snapshot()).get("rows", [])}
    # Escalate before reconciling so a symbol that fires in this sweep is promoted in the
    # same pass — otherwise its signal would be on screen while the symbol was not yet
    # watched, and the watchlist-filtered view would hide it.
    fired = await _escalate_all(scans, shared)
    live = await live_signal_symbols()
    changes = await watchlist.reconcile(scans, previous, live)
    # The watchlist draws only from the selected ten — anything that fell out of it goes,
    # unless it's protected (open position, pin, live signal).
    # Held-only scans (kept for open positions) don't count as being in the top ten.
    changes["removed"] += await watchlist.prune_outside(
        {s["symbol"] for s in scans if not s.get("held_only")}, protected=live
    )
    snapshot = await _persist(scans)
    _state["full_scans"] = scans
    await _run_autotrader(scans, shared)
    try:
        from experiments import hold72
        await hold72.on_scans(scans)           # full sweeps only — the replay it tests used full scans
    except Exception as e:
        logger.warning("Hold-72 entries failed: %s", e)
    try:
        from experiments import fade24
        await fade24.on_scans(scans)           # trades against the scanner (see experiments/fade24.py)
    except Exception as e:
        logger.warning("Fade-24 entries failed: %s", e)
    try:
        from experiments import claude_swing
        await claude_swing.maybe_review(scans, shared)
    except Exception as e:
        logger.warning("Claude swing scheduling failed: %s", e)
    try:
        from trading import regime
        now_regime = await regime.current()
        for s in scans:
            s["regime_at_scan"] = now_regime
        signal_log.add_scan_features(scans)
    except Exception as e:
        logger.warning("Recording scan features failed: %s", e)

    bus.publish("scan", snapshot)
    if changes["added"] or changes["removed"]:
        bus.publish("watchlist", {"entries": snapshot["watchlist"], **changes})

    _state["last_general"] = time.time()
    top = snapshot["rows"][0] if snapshot.get("rows") else None       # rows are sorted by |conviction|
    logger.info(
        "── General sweep done in %.1fs — %d scanned, %d fired, %d watched%s ──",
        time.perf_counter() - t0, len(scans), len(fired), len(snapshot["watchlist"]),
        f", top {top['symbol']} {top['conviction']:+.2f}" if top else "",
    )
    return snapshot


async def run_watchlist_sweep() -> dict | None:
    """Every minute: check open positions (both wallets), then re-scan watched symbols.

    Position checks come first and don't depend on the watchlist — an empty watchlist or a
    failed scan must never leave stops and targets unchecked.
    """
    await _sync_open_trade_prices([])
    await autotrader.monitor_now()
    from experiments import books as experiment_books
    await experiment_books.monitor_all()
    from markets import engine as markets
    await markets.monitor_all()
    async with _locks["watchlist"]:
        return await _watchlist_sweep()


async def _watchlist_sweep() -> dict | None:
    watched = await watchlist.symbols()
    if not watched:
        return None

    universe = _state.get("universe") or await get_tradeable_pool(POOL_SIZE)
    coins = [c for c in universe if c["pair"] in watched]
    if not coins:
        return None

    t0 = time.perf_counter()
    shared = await _shared_feeds()
    scans = await _scan_many(coins, shared, light=True)
    if not scans:
        return None

    snapshot = await get_snapshot()
    previous = {r["symbol"]: r for r in snapshot.get("rows", [])}

    await _escalate_all(scans, shared)
    changes = await watchlist.reconcile(scans, previous, await live_signal_symbols())

    # Merge the refreshed rows back into the table so the UI shows one coherent view.
    merged = {r["symbol"]: r for r in snapshot.get("rows", [])}
    for scan in scans:
        merged[scan["symbol"]] = _strip(scan)

    entries = await watchlist.get_all()
    snapshot["rows"] = sorted(
        merged.values(), key=lambda r: abs(r.get("conviction", 0)), reverse=True
    )
    snapshot["watchlist"] = list(entries.values())
    snapshot["fired"] = await escalation.recent_fired()
    snapshot["updated_at"] = time.time()
    await state.set(SNAPSHOT_KEY, snapshot, SNAPSHOT_TTL)

    await _run_autotrader(scans, shared)

    bus.publish("watch", {
        "rows": [_strip(s) for s in scans],
        "watchlist": snapshot["watchlist"],
        "updated_at": snapshot["updated_at"],
    })
    if changes["added"] or changes["removed"]:
        bus.publish("watchlist", {"entries": snapshot["watchlist"], **changes})

    _state["last_watchlist"] = time.time()
    logger.debug(
        "Watchlist sweep: %d symbols in %.1fs", len(scans), time.perf_counter() - t0
    )
    return snapshot


async def _sync_open_trade_prices(scans: list[dict]) -> None:
    """Walk the main wallet's positions along the perpetual's minute bars (fills, stops,
    targets, liquidations) and charge funding — the same engine the autotrader uses.

    This is the reason open positions are force-added to the watchlist at all.
    """
    from live import swing as live_swing
    for wallet in live_swing.wallets("main"):           # paper wallet and live ledger
        try:
            for event in await monitor.run(wallet):
                if event["type"] == "closed":
                    trade = event["trade"]
                    logger.info(
                        "%s trade closed: %s %s — %s, P&L $%.2f", wallet.label,
                        trade.get("symbol"), trade.get("direction"), trade.get("close_reason"), trade["pnl_usd"],
                    )
                    bus.publish("trade_closed", trade)
        except Exception as e:
            logger.warning("Could not monitor %s positions: %s", wallet.label, e)


async def _run_autotrader(scans: list[dict], shared: dict) -> None:
    """The autonomous experiment trades its own wallet off the same scans (unstripped —
    it needs the per-timeframe detail in the signal package)."""
    try:
        await autotrader.on_scans(
            scans, set(await watchlist.symbols()), shared.get("global_market", {})
        )
    except Exception as e:
        logger.warning("Autotrader failed: %s", e)
    # Scale open positions in/out on the same fresh data (each trade at most every few minutes).
    await position_manager.review(scans)


async def _run_debriefs() -> None:
    """Tag closed trades and have Claude debrief the swing ones (trading/debrief.py)."""
    from trading import debrief
    await debrief.run()


async def _label_readings() -> None:
    """Label past scan readings with what price did next (research/signal_scores.py)."""
    from research import signal_scores
    n = await signal_scores.label_pending()
    if n:
        logger.info("Signal scoring: labelled %d scan readings", n)


async def _loop(name: str, interval: int, fn, initial_delay: float = 0.0) -> None:
    if initial_delay:
        await asyncio.sleep(initial_delay)
    while True:
        started = time.time()
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("%s sweep raised — continuing: %s", name, e)

        elapsed = time.time() - started
        await asyncio.sleep(max(5.0, interval - elapsed))


def start() -> None:
    if _tasks:
        return
    _tasks.append(asyncio.create_task(
        _loop("general", GENERAL_INTERVAL, run_general_sweep), name="scanner-general"
    ))
    _tasks.append(asyncio.create_task(
        _loop("watchlist", WATCHLIST_INTERVAL, run_watchlist_sweep, initial_delay=45),
        name="scanner-watchlist",
    ))
    _tasks.append(asyncio.create_task(_loop("labels", 3600, _label_readings, initial_delay=300), name="signal-labels"))
    _tasks.append(asyncio.create_task(_loop("debriefs", 900, _run_debriefs, initial_delay=180), name="trade-debriefs"))
    from trading import guards
    if guards.ENABLED:
        # Rethink after consistent losses (trading/guards.py).
        _tasks.append(asyncio.create_task(_loop("guards", 900, guards.check, initial_delay=120), name="guards"))
    from markets import engine as markets
    if markets.ENABLED:
        # Stocks and futures (commodity) perps: scan, rules / Claude / trend books (markets/engine.py).
        _tasks.append(asyncio.create_task(_loop("markets", markets.SWEEP_S, markets.run, initial_delay=60), name="markets"))
    from ops import calendar
    # Earnings dates for the stock perps (Nasdaq's calendar), refreshed daily; FOMC/CPI are fixed schedules.
    _tasks.append(asyncio.create_task(_loop("calendar", 3600, calendar.refresh_earnings, initial_delay=20), name="calendar"))
    from data import positioning
    # Binance positioning ratios (top-trader long/short, taker flow): recorded hourly for a later lab, nothing trades on them.
    _tasks.append(asyncio.create_task(_loop("positioning", 3600, positioning.tick, initial_delay=240), name="positioning"))
    from data import funding_venues
    # Binance vs Hyperliquid funding on the same coins, hourly, for the cross-venue funding lab; nothing trades on it.
    _tasks.append(asyncio.create_task(_loop("funding_venues", 3600, funding_venues.tick, initial_delay=150), name="funding-venues"))
    from trading import target
    # Pair-book equity log + the $/day tracker and its circuit breaker.
    _tasks.append(asyncio.create_task(_loop("target", 900, target.tick, initial_delay=90), name="target"))
    from ops import watchdog
    if watchdog.ENABLED:
        # Says so (macOS notification, SMS, app banner) when prices or scans stop arriving.
        _tasks.append(asyncio.create_task(_loop("watchdog", watchdog.CHECK_S, watchdog.check, initial_delay=30), name="watchdog"))
    from news import collector as news
    if news.ENABLED:
        # Polls each news source on its own interval (2–15 min) and classifies new headlines.
        _tasks.append(asyncio.create_task(_loop("news", news.TICK, news.tick, initial_delay=10), name="news"))
    logger.info(
        "Scanner started — general every %ds, watchlist every %ds",
        GENERAL_INTERVAL, WATCHLIST_INTERVAL,
    )


async def stop() -> None:
    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    logger.info("Scanner stopped.")


def status() -> dict:
    return {
        "running": [t.get_name() for t in _tasks if not t.done()],
        "last_general": _state["last_general"],
        "last_watchlist": _state["last_watchlist"],
        "general_interval": GENERAL_INTERVAL,
        "watchlist_interval": WATCHLIST_INTERVAL,
        "subscribers": bus.subscriber_count(),
        # The firing bar, so the Signal tab can show how close each coin is when nothing has fired.
        "min_conviction": escalation.MIN_CONVICTION,
        "min_confidence": escalation.MIN_CONFIDENCE,
    }
