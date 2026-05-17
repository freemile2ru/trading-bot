"""FastAPI routes for the Crypto Trading Oracle."""

import asyncio
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from oracle.pipeline import run_oracle
from oracle.signal_history import get_history
from trading.paper_trader import (
    get_portfolio,
    open_trade,
    close_trade_manually,
    update_prices,
    reset_portfolio,
)
from data.binance_client import fetch_ticker_24h, fetch_ohlcv

router = APIRouter()

# ── Supported symbols ─────────────────────────────────────────────────────────

VALID_SYMBOLS = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "MATICUSDT", "UNIUSDT",
}

VALID_INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d", "1w"}


def _validate_symbol(symbol: str) -> str:
    s = symbol.upper()
    if s not in VALID_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported symbol '{s}'. Valid: {sorted(VALID_SYMBOLS)}",
        )
    return s


# ── In-memory oracle cache ────────────────────────────────────────────────────
# Entries: { symbol: {"data": ..., "ts": float} }

_cache: dict[str, dict] = {}
CACHE_TTL = 60  # seconds


def _cache_get(symbol: str) -> dict | None:
    entry = _cache.get(symbol)
    if entry and time.monotonic() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    if entry:
        del _cache[symbol]  # evict expired
    return None


def _cache_set(symbol: str, data: dict) -> None:
    _cache[symbol] = {"data": data, "ts": time.monotonic()}
    # Evict any entries older than 2× TTL to bound memory
    cutoff = time.monotonic() - CACHE_TTL * 2
    stale = [k for k, v in _cache.items() if v["ts"] < cutoff]
    for k in stale:
        del _cache[k]


# ── Oracle endpoints ──────────────────────────────────────────────────────────

@router.get("/oracle/{symbol}")
async def get_oracle_signal(symbol: str):
    """Run the full oracle pipeline for a symbol. Results cached for 60s."""
    symbol = _validate_symbol(symbol)
    cached = _cache_get(symbol)
    if cached:
        return {**cached, "cached": True}
    try:
        result = await run_oracle(symbol)
        _cache_set(symbol, result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/oracle/{symbol}/quick")
async def get_quick_signal(symbol: str):
    """Return just the trade call (no raw data) — always uses cache if available."""
    symbol = _validate_symbol(symbol)
    cached = _cache_get(symbol)
    source = cached if cached else await run_oracle(symbol)
    return {
        "symbol": symbol,
        "oracle": source.get("oracle"),
        "signal_summary": {
            "composite_score": source.get("signal_package", {}).get("composite_score"),
            "direction": source.get("signal_package", {}).get("direction"),
            "confidence": source.get("signal_package", {}).get("confidence_pct"),
            "best_timeframe": source.get("signal_package", {}).get("best_timeframe"),
        },
        "pipeline_status": source.get("pipeline_status"),
        "cached": cached is not None,
    }


@router.get("/oracle/{symbol}/history")
async def get_signal_history(
    symbol: str,
    limit: int = Query(default=20, ge=1, le=100),
):
    """Return the last N oracle signals for a symbol."""
    symbol = _validate_symbol(symbol)
    return {"symbol": symbol, "history": get_history(symbol, limit)}


# ── Market data endpoints ─────────────────────────────────────────────────────

@router.get("/ohlcv/{symbol}")
async def get_ohlcv(
    symbol: str,
    interval: str = Query(default="1h"),
    limit: int = Query(default=100, ge=10, le=500),
):
    """Return OHLCV candles for charting."""
    symbol = _validate_symbol(symbol)
    if interval not in VALID_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval '{interval}'. Valid: {sorted(VALID_INTERVALS)}")
    try:
        candles = await fetch_ohlcv(symbol, interval, limit)
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


@router.get("/symbols")
async def list_symbols():
    return {"symbols": sorted(VALID_SYMBOLS)}


# ── Paper trading endpoints ───────────────────────────────────────────────────

class OpenTradeRequest(BaseModel):
    symbol: str
    direction: Literal["long", "short"]
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    size_pct: float = 2.0
    timeframe: str = "1h"
    oracle_confidence: int = 50
    oracle_reasoning: str = ""

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        s = v.upper()
        if s not in VALID_SYMBOLS:
            raise ValueError(f"Unsupported symbol: {s}")
        return s

    @field_validator("entry_price", "stop_loss", "take_profit_1", "take_profit_2")
    @classmethod
    def positive_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Price must be positive")
        return v

    @field_validator("size_pct")
    @classmethod
    def valid_size(cls, v: float) -> float:
        if not (0.1 <= v <= 100):
            raise ValueError("size_pct must be between 0.1 and 100")
        return v


@router.post("/paper/trade")
async def paper_open_trade(req: OpenTradeRequest):
    return open_trade(
        symbol=req.symbol,
        direction=req.direction,
        entry_price=req.entry_price,
        stop_loss=req.stop_loss,
        take_profit_1=req.take_profit_1,
        take_profit_2=req.take_profit_2,
        size_pct=req.size_pct,
        timeframe=req.timeframe,
        oracle_confidence=req.oracle_confidence,
        oracle_reasoning=req.oracle_reasoning,
    )


@router.delete("/paper/trade/{trade_id}")
async def paper_close_trade(trade_id: str, current_price: float = Query(..., gt=0)):
    trade = close_trade_manually(trade_id, current_price)
    if not trade:
        raise HTTPException(status_code=404, detail=f"Trade '{trade_id}' not found")
    return trade


@router.get("/paper/portfolio")
async def paper_portfolio():
    return get_portfolio()


@router.post("/paper/update-prices")
async def paper_update_prices():
    """Fetch live Binance prices and apply to all open paper trades."""
    portfolio = get_portfolio()
    open_trades = portfolio.get("open_trades", [])
    if not open_trades:
        return {"message": "No open trades", "auto_closed": []}

    symbols = list({t["symbol"] for t in open_trades})
    results = await asyncio.gather(
        *[fetch_ticker_24h(s) for s in symbols],
        return_exceptions=True,
    )
    prices = {
        s: r["price"]
        for s, r in zip(symbols, results)
        if not isinstance(r, Exception) and r.get("price")
    }
    auto_closed = update_prices(prices)
    return {"prices_updated": prices, "auto_closed": auto_closed}


@router.post("/paper/reset")
async def paper_reset(starting_balance: float = Query(default=10000.0, gt=0)):
    return reset_portfolio(starting_balance)


# ── Oracle + auto-execute ─────────────────────────────────────────────────────

@router.post("/oracle/{symbol}/execute")
async def oracle_and_execute(
    symbol: str,
    auto_size_pct: float = Query(default=2.0, ge=0.1, le=10.0),
):
    """Run oracle then open a paper trade if confidence ≥ 60 and action is BUY/SELL."""
    symbol = _validate_symbol(symbol)
    result = await run_oracle(symbol)
    _cache_set(symbol, result)

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

    trade = open_trade(
        symbol=symbol,
        direction=direction,
        entry_price=rec.get("entry_price") or result["signal_package"]["price"],
        stop_loss=rec.get("stop_loss", 0),
        take_profit_1=rec.get("take_profit_1", 0),
        take_profit_2=rec.get("take_profit_2", 0),
        size_pct=auto_size_pct,
        timeframe=rec.get("timeframe", "1h"),
        oracle_confidence=confidence,
        oracle_reasoning=oracle.get("reasoning", {}).get("primary_thesis", ""),
    )
    return {"message": "Paper trade opened", "trade": trade, "oracle": oracle}
