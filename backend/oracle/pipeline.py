"""Main oracle pipeline — gathers all data, scores it, calls Claude."""

import asyncio
import logging
import time
from data.binance_client import fetch_market_snapshot
from data.coingecko_client import get_for_symbol, fetch_global_market
from data.news_client import fetch_all_news
from data.macro_client import fetch_macro_snapshot, fetch_fear_greed_index
from data.onchain_client import fetch_onchain_snapshot
from data.social_client import fetch_twitter_sentiment
from analysis.technical import compute_multi_timeframe_ta
from analysis.scoring import build_signal_package
from oracle.claude_oracle import synthesize_trade
from oracle.signal_history import save_signal

logger = logging.getLogger("oracle.pipeline")

SOURCES = [
    "binance",
    "coingecko",
    "global_market",
    "news",
    "macro",
    "fear_greed",
    "onchain",
    "social",
]

DEFAULTS = {
    "binance":       lambda sym: {"symbol": sym, "ohlcv": {}, "ticker": {}, "order_book": {}},
    "coingecko":     lambda _: {},
    "global_market": lambda _: {},
    "news":          lambda _: {"aggregate_sentiment": 0, "aggregate_label": "neutral", "total_articles": 0, "cryptopanic": [], "crypto_news": [], "macro_news": [], "geopolitical_news": []},
    "macro":         lambda _: {},
    "fear_greed":    lambda _: {"value": 50, "classification": "Neutral"},
    "onchain":       lambda sym: {"asset": sym.replace("USDT", "")},
    "social":        lambda _: {"sentiment": 0.0, "label": "neutral", "tweets_analyzed": 0},
}


async def run_oracle(symbol: str = "BTCUSDT") -> dict:
    """
    Full pipeline:
    1. Gather all data in parallel
    2. Compute TA across timeframes
    3. Build signal package
    4. Claude synthesizes → trade recommendation
    """
    logger.info("=== Oracle pipeline starting for %s ===", symbol)
    t0 = time.perf_counter()

    # Step 1: Parallel data fetch
    logger.info("[1/4] Fetching data from all sources in parallel...")
    raw_results = await asyncio.gather(
        fetch_market_snapshot(symbol),
        get_for_symbol(symbol),
        fetch_global_market(),
        fetch_all_news(symbol),
        fetch_macro_snapshot(),
        fetch_fear_greed_index(),
        fetch_onchain_snapshot(symbol),
        fetch_twitter_sentiment(symbol),
        return_exceptions=True,
    )

    # Log each source result and apply fallbacks
    resolved = {}
    fetch_statuses = {}
    for name, result in zip(SOURCES, raw_results):
        if isinstance(result, Exception):
            logger.warning("  ✗ %-15s FAILED  — %s: %s", name, type(result).__name__, result)
            resolved[name] = DEFAULTS[name](symbol)
            fetch_statuses[name] = {"ok": False, "error": f"{type(result).__name__}: {result}"}
        else:
            # Also surface any error keys embedded in the result dict
            embedded_error = result.get("error") if isinstance(result, dict) else None
            if embedded_error:
                logger.warning("  ~ %-15s PARTIAL — %s", name, embedded_error)
                fetch_statuses[name] = {"ok": "partial", "error": embedded_error}
            else:
                logger.info("  ✓ %-15s OK", name)
                fetch_statuses[name] = {"ok": True}
            resolved[name] = result

    market_snapshot = resolved["binance"]
    cg_data         = resolved["coingecko"]
    global_market   = resolved["global_market"]
    news            = resolved["news"]
    macro           = resolved["macro"]
    fear_greed      = resolved["fear_greed"]
    onchain         = resolved["onchain"]
    social          = resolved["social"]

    # Step 2: TA across all timeframes
    logger.info("[2/4] Computing multi-timeframe technical analysis...")
    ta_result = compute_multi_timeframe_ta(market_snapshot.get("ohlcv", {}))
    tf_statuses = {
        tf: ("ok" if not d.get("error") else f"error: {d['error']}")
        for tf, d in ta_result.get("timeframes", {}).items()
    }
    for tf, status in tf_statuses.items():
        if "error" in status:
            logger.warning("  ~ TA [%-4s] %s", tf, status)
        else:
            logger.info("  ✓ TA [%-4s] score=%.3f  label=%s", tf,
                        ta_result["timeframes"][tf].get("signal_score", 0),
                        ta_result["timeframes"][tf].get("signal_label", "?"))

    # Step 3: Build signal package
    logger.info("[3/4] Building composite signal package...")
    signal_package = build_signal_package(
        symbol=symbol,
        ta_result=ta_result,
        news=news,
        macro=macro,
        fear_greed=fear_greed,
        onchain=onchain,
        social=social,
        coingecko=cg_data,
    )

    ticker = market_snapshot.get("ticker", {})
    if ticker.get("price"):
        signal_package["price"] = ticker["price"]

    logger.info(
        "  composite=%.4f  direction=%s  confidence=%d%%  tf=%s",
        signal_package.get("composite_score", 0),
        signal_package.get("direction", "?"),
        signal_package.get("confidence_pct", 0),
        signal_package.get("best_timeframe", "?"),
    )

    # Step 4: Claude oracle synthesis
    logger.info("[4/4] Calling Claude oracle for trade synthesis...")
    oracle_result = await synthesize_trade(signal_package, global_market)

    if oracle_result.get("error"):
        logger.error("  ✗ Claude oracle FAILED — %s", oracle_result["error"])
    else:
        rec = oracle_result.get("trade_recommendation", {})
        meta = oracle_result.get("_meta", {})
        logger.info(
            "  ✓ Claude oracle OK — action=%s  confidence=%d%%  tokens(in=%d out=%d cache_hit=%d)",
            rec.get("action", "?"),
            rec.get("confidence", 0),
            meta.get("input_tokens", 0),
            meta.get("output_tokens", 0),
            meta.get("cache_read_tokens", 0),
        )

    elapsed = time.perf_counter() - t0
    logger.info("=== Pipeline complete in %.2fs ===", elapsed)

    result = {
        "symbol": symbol,
        "signal_package": signal_package,
        "oracle": oracle_result,
        "raw_data": {
            "ticker": ticker,
            "order_book": market_snapshot.get("order_book", {}),
            "fear_greed": fear_greed,
        },
        "pipeline_status": {
            "elapsed_seconds": round(elapsed, 2),
            "data_sources": fetch_statuses,
            "ta_timeframes": tf_statuses,
            "oracle_ok": not bool(oracle_result.get("error")),
        },
    }

    # Persist to history (non-blocking — skip on error so history never breaks the pipeline)
    try:
        save_signal(result)
    except Exception as e:
        logger.warning("Could not save signal history: %s", e)

    return result
