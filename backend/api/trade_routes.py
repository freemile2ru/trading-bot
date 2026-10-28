"""Trade tab — place, manage and close your own futures orders, paper or live on Binance.

Every order goes through the main wallet's active ledger (live/swing.active("main")): the paper
wallet, or the live ledger when the main wallet is switched to live on the Live tab. So the
same rules apply as everywhere else — a stop is required and becomes a real exchange stop when
live, leverage can't put liquidation inside the stop, and live trades pass the live guards.

Market data (candles, ticker) comes from the venue you're trading on: Binance mainnet for paper
(paper fills use mainnet perpetual prices) and the configured Binance environment when live
(testnet prices differ from mainnet). The browser streams the order book and trades straight
from that venue's public websocket (`ws` in /trade/venue).
"""

import asyncio
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from data.http import get_json
from live import config as LC
from live import control
from live import swing as live_swing
from trading import futures

router = APIRouter(prefix="/trade", tags=["trade"])

MAINNET = {"rest": "https://fapi.binance.com", "ws": "wss://fstream.binance.com"}
INTERVAL_S = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
              "6h": 21600, "12h": 43200, "1d": 86400, "1w": 604800}
INTERVALS = set(INTERVAL_S)
MAX_BARS = 10_000          # 3 months of 15m; 1m/5m charts stop at about 1 week / 5 weeks
_klines_cache: dict[tuple, tuple[float, list]] = {}
_symbols: dict[str, tuple[float, list[dict]]] = {}


def _live() -> bool:
    return control.is_live("main")


def _venue() -> dict:
    return {"rest": LC.REST.rstrip("/"), "ws": LC.WS} if _live() else MAINNET


def _no_target(direction: str, price: float) -> float:
    """A target that can't be reached — the manual policy then simply has no take-profit."""
    return price * 50 if direction == "long" else price / 50


class OrderRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,20}USDT$")
    side: str = Field(pattern="^(long|short)$")
    type: str = Field(pattern="^(market|limit)$")
    notional_usd: float = Field(gt=0, description="position size in USDT (margin × leverage)")
    leverage: int = Field(ge=1, le=125)
    stop_loss: float = Field(gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    price: float | None = Field(default=None, gt=0, description="limit price")


class LevelsRequest(BaseModel):
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)


@router.get("/venue")
async def venue():
    live = _live()
    # Binance mainnet serves the order book on /public and trades/klines/mark price on /market;
    # the testnet still serves everything on /stream.
    ws = _venue()["ws"].rstrip("/")
    streams = ({"ws_book": f"{ws}/public/stream", "ws_market": f"{ws}/market/stream"}
               if "fstream.binance.com" in ws else {"ws_book": f"{ws}/stream", "ws_market": f"{ws}/stream"})
    return {"mode": "live" if live else "paper", "env": LC.ENV if live else "mainnet data", **streams, "live_allowed": LC.ALLOWED, "has_keys": LC.keys("main") is not None,
            "confirm": control.confirmation_phrase("main"),
            "max_leverage": LC.MAX_LEVERAGE if live else futures.MAX_LEVERAGE,
            "maker_fee": futures.MAKER_FEE, "taker_fee": futures.TAKER_FEE}


@router.get("/symbols")
async def symbols():
    """Tradeable USDT perpetuals on the current venue, most liquid first."""
    v = _venue()
    hit = _symbols.get(v["rest"])
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    info, tickers = await asyncio.gather(get_json(f"{v['rest']}/fapi/v1/exchangeInfo", timeout=20.0),
                                         get_json(f"{v['rest']}/fapi/v1/ticker/24hr", timeout=20.0))
    ok = {s["symbol"]: s for s in info["symbols"] if s.get("contractType") == "PERPETUAL"
          and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
    rows = sorted(({"symbol": t["symbol"], "volume": float(t["quoteVolume"]), "change": float(t["priceChangePercent"])}
                   for t in tickers if t["symbol"] in ok), key=lambda r: -r["volume"])
    for r in rows:
        f = {x["filterType"]: x for x in ok[r["symbol"]]["filters"]}
        r["tick"] = float(f["PRICE_FILTER"]["tickSize"])
        r["step"] = float(f["LOT_SIZE"]["stepSize"])
        r["min_notional"] = float(f.get("MIN_NOTIONAL", {}).get("notional", 0) or 0)
    _symbols[v["rest"]] = (time.time(), rows)
    return rows


@router.get("/klines")
async def klines(symbol: str = Query(pattern=r"^[A-Z0-9]{2,20}USDT$"), interval: str = "15m",
                 days: float = Query(90, gt=0, le=365)):
    """`days` of history (default 3 months), paged backwards 1,500 bars at a time from Binance,
    capped at MAX_BARS so 1-minute charts stay responsive."""
    if interval not in INTERVALS:
        raise HTTPException(400, f"interval must be one of {sorted(INTERVALS)}")
    key = (_venue()["rest"], symbol, interval, days)
    hit = _klines_cache.get(key)
    if hit and time.time() - hit[0] < 30:              # switching back to a timeframe is instant
        return hit[1]
    want = min(MAX_BARS, int(days * 86400 / INTERVAL_S[interval]) + 1)
    step_ms = INTERVAL_S[interval] * 1000
    start = (int(time.time() * 1000) // step_ms - want + 1) * step_ms
    # Pages are independent windows, so fetch them all at once (sequentially the testnet took ~15s).
    starts = [start + i * 1500 * step_ms for i in range(-(-want // 1500))]
    pages = await asyncio.gather(*(get_json(f"{_venue()['rest']}/fapi/v1/klines",
                                            params={"symbol": symbol, "interval": interval, "limit": 1500, "startTime": st})
                                   for st in starts))
    rows = sorted((r for page in pages for r in page), key=lambda r: r[0])
    seen, out = set(), []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            out.append({"time": r[0] // 1000, "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                        "close": float(r[4]), "volume": float(r[5])})
    _klines_cache[key] = (time.time(), out)
    if len(_klines_cache) > 50:
        _klines_cache.pop(next(iter(_klines_cache)))
    return out


@router.get("/ticker")
async def ticker(symbol: str = Query(pattern=r"^[A-Z0-9]{2,20}USDT$")):
    rest = _venue()["rest"]
    t, p = await asyncio.gather(get_json(f"{rest}/fapi/v1/ticker/24hr", params={"symbol": symbol}),
                                get_json(f"{rest}/fapi/v1/premiumIndex", params={"symbol": symbol}))
    return {"last": float(t["lastPrice"]), "change_pct": float(t["priceChangePercent"]),
            "high": float(t["highPrice"]), "low": float(t["lowPrice"]), "volume_usd": float(t["quoteVolume"]),
            "mark": float(p["markPrice"]), "index": float(p["indexPrice"]),
            "funding_rate": float(p["lastFundingRate"]), "next_funding": int(p["nextFundingTime"])}


@router.get("/account")
async def account():
    """Balance of the ledger new orders go to, and positions/orders/history from both main ledgers."""
    def load():
        wallets = live_swing.wallets("main")
        rows = {"positions": [], "orders": [], "history": []}
        for w in wallets:
            mode = "live" if w.label.endswith("_live") else "paper"
            p, o = w.get_portfolio(closed_limit=50), w.get_orders(history_limit=1)
            rows["positions"] += [{**t, "mode": mode} for t in p["open_trades"]]
            rows["orders"] += [{**x, "mode": mode} for x in o["pending"]]
            rows["history"] += [{**t, "mode": mode} for t in p["closed_trades"]]
        rows["history"].sort(key=lambda t: t.get("closed_at") or "", reverse=True)
        active = live_swing.active("main")
        p = active.get_portfolio(closed_limit=1)
        bal = {"equity": p["total_equity"], "available": p["free_margin"], "wallet": p["balance"],
               "unrealized": p["open_pnl"], "margin_used": p["margin_used"]}
        exchange = []
        if _live():
            try:
                c = control.client("main")
                bal = {**bal, **control.client("main").equity()}
                exchange = _exchange_positions(c)
            except Exception as e:
                bal["error"] = str(e)[:200]
        return {"mode": "live" if _live() else "paper", "balance": bal, **rows, "exchange": exchange}
    return await asyncio.to_thread(load)


def _exchange_positions(c) -> list[dict]:
    """Every position actually open on the Binance account, whichever system opened it, with its
    protective orders — the source of truth, next to the app's own ledgers."""
    try:
        algo = c.request("GET", "/fapi/v1/openAlgoOrders")
    except Exception:
        algo = []                     # accounts without the algo endpoint keep stops on /openOrders
    regular = c.open_orders()
    out = []
    for p in c.positions():
        sym, amt = p["symbol"], float(p["positionAmt"])
        owner = ("main" if live_swing.LIVE["main"].holds(sym) else "auto" if live_swing.LIVE["auto"].holds(sym)
                 else "unmanaged")
        stops = [float(o.get("triggerPrice") or 0) for o in algo if o["symbol"] == sym and o.get("orderType") == "STOP_MARKET"]
        stops += [float(o["stopPrice"]) for o in regular if o["symbol"] == sym and o["type"] == "STOP_MARKET"]
        tps = [float(o.get("triggerPrice") or 0) for o in algo if o["symbol"] == sym and o.get("orderType") == "TAKE_PROFIT_MARKET"]
        tps += [float(o["price"]) for o in regular if o["symbol"] == sym and o["type"] == "LIMIT" and o.get("reduceOnly")]
        entry, mark, lev = float(p["entryPrice"]), float(p["markPrice"]), int(float(p.get("leverage") or 1))
        out.append({"symbol": sym, "owner": owner, "side": "long" if amt > 0 else "short", "qty": abs(amt),
                    "entry": entry, "mark": mark, "notional": abs(amt) * mark, "leverage": lev,
                    "unrealized": float(p["unRealizedProfit"]), "liquidation": float(p.get("liquidationPrice") or 0),
                    "margin": float(p.get("isolatedMargin") or 0) or abs(amt) * entry / lev,
                    "stop": stops[0] if stops else None, "target": tps[0] if tps else None})
    return out


async def _price(symbol: str) -> float:
    t = await get_json(f"{_venue()['rest']}/fapi/v1/ticker/price", params={"symbol": symbol})
    return float(t["price"])


@router.post("/order")
async def place_order(req: OrderRequest):
    from trading import book_switches
    if not book_switches.enabled("main"):
        raise HTTPException(409, "the main wallet is switched off on the Books tab — no new trades")
    live = _live()
    cap = LC.MAX_LEVERAGE if live else futures.MAX_LEVERAGE
    if req.leverage > cap:
        raise HTTPException(400, f"leverage above the {cap}x cap")
    wallet = live_swing.active("main")
    ref = req.price if req.type == "limit" else await _price(req.symbol)
    if req.type == "limit" and not req.price:
        raise HTTPException(400, "a limit order needs a price")
    tp = req.take_profit or _no_target(req.side, ref)
    from trading import regime
    extra = {"strategy": "manual", "source": "trade tab", "exit_policy": "manual", "tp_set": bool(req.take_profit),
             "regime_at_entry": await regime.current()}
    common = dict(symbol=req.symbol, direction=req.side, stop_loss=req.stop_loss, take_profit_1=tp, take_profit_2=tp,
                  size_pct=None, leverage=req.leverage, notional_usd=req.notional_usd)
    if req.type == "market":
        result = await asyncio.to_thread(
            wallet.open_trade, entry_price=ref, timeframe="manual", oracle_confidence=0,
            oracle_reasoning="Manual order from the Trade tab", hold_duration="until closed", extra=extra, **common)
    else:
        result = await asyncio.to_thread(
            wallet.place_order, limit_price=req.price, expires_at=time.time() + 30 * 86400,
            trade_fields={"timeframe": "manual", "oracle_confidence": 0, "hold_duration": "until closed",
                          "oracle_reasoning": "Manual limit order from the Trade tab", "extra": extra},
            extra={"source": "trade tab"}, **common)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return {"mode": "live" if live else "paper", "kind": "position" if req.type == "market" else "order", **result}


@router.delete("/order/{order_id}")
async def cancel_order(order_id: str):
    for w in live_swing.wallets("main"):
        cancelled = await asyncio.to_thread(w.cancel_order, order_id, "manual")
        if cancelled:
            return cancelled
    raise HTTPException(404, "order not found")


@router.post("/position/{trade_id}/close")
async def close_position(trade_id: str):
    found = live_swing.find_trade("main", trade_id)
    if not found:
        raise HTTPException(404, "position not found")
    wallet, trade = found
    price = await _price(trade["symbol"]) if not wallet.label.endswith("_live") else trade["current_price"]
    closed = await asyncio.to_thread(wallet.close_trade_manually, trade_id, price, "manual")
    if not closed:
        raise HTTPException(502, "close failed — check the Live tab's activity log")
    return closed


@router.post("/position/{trade_id}/levels")
async def set_levels(trade_id: str, req: LevelsRequest):
    found = live_swing.find_trade("main", trade_id)
    if not found:
        raise HTTPException(404, "position not found")
    result = await asyncio.to_thread(found[0].update_levels, trade_id, req.stop_loss, req.take_profit)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result
