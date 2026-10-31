"""Lighter (zkLighter), read-only: the public P&L leaderboard, any account's live positions, its trades and its P&L series.

Lighter is an order-book perpetuals DEX whose state is public: the leaderboard (~100k accounts, 24h/7d/30d/all windows,
sortable by P&L, ROI, volume, account value) names an L1 address; the address resolves to account indexes; an account
returns its positions (symbol, signed size, average entry, liquidation) and a daily P&L series; trades come per market.
No key, nothing is sent beyond the query. Markets include crypto, US stocks (AAPL, AMD, MU, QQQ…) and commodities
(XAG, XAU, WTI, NATGAS, XCU), so one leader can feed all three copy books.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("data.lighter")

BASE = "https://mainnet.zklighter.elliot.ai/api/v1"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept": "application/json"}
# Lighter symbol → Binance USDT perp where the name differs; everything else is SYMBOL + "USDT" when Binance lists it.
ALIASES = {"WTI": "CLUSDT", "BRENT": "BZUSDT", "XCU": "COPPERUSDT", "SAMSUNGUSD": "SAMSUNGUSDT", "1000SHIB": "1000SHIBUSDT"}
_markets: dict = {"at": 0.0, "by_symbol": {}, "by_id": {}}
ALL_MARKETS = 255          # observed 2026-10-30: a market id that doesn't exist makes /trades return the account's trades on every market


async def _get(path: str, params: dict) -> dict:
    import asyncio
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(4):
            r = await client.get(f"{BASE}/{path}", params=params, headers=HEADERS)
            if r.status_code == 429 and attempt < 3:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            d = r.json()
            if isinstance(d, dict) and d.get("code") not in (None, 200):
                raise RuntimeError(f"lighter {path}: {d.get('message')}")
            return d
    return {}


async def leaderboard(time_window: str = "30d", sort_by: str = "pnl", limit: int = 100, offset: int = 0) -> list[dict]:
    """Rows {rank, address, account_value, pnl, roi (percent of account value), volume, streak, biggest_asset}."""
    d = await _get("pnlLeaderboard", {"time_window": time_window, "sort_by": sort_by, "sort_dir": "desc", "limit": limit, "offset": offset})
    out = []
    for e in d.get("entries") or []:
        out.append({"rank": e.get("rank"), "address": e.get("l1_address"), "account_value": _f(e.get("account_value")), "pnl": _f(e.get("pnl")),
                    "roi": _f(e.get("roi")), "volume": _f(e.get("volume")), "streak": e.get("streak"), "biggest_asset": e.get("biggest_position_asset")})
    return out


async def account_index(address: str) -> int | None:
    """The main account (type 0) behind an L1 address."""
    d = await _get("accountsByL1Address", {"l1_address": address})
    subs = d.get("sub_accounts") or []
    main = next((s for s in subs if s.get("account_type") == 0), subs[0] if subs else None)
    return int(main["index"]) if main else None


async def account(index: int) -> dict:
    d = await _get("account", {"by": "index", "value": index})
    accs = d.get("accounts") or []
    return accs[0] if accs else {}


async def positions(index: int) -> list[dict]:
    """Open perp positions: {coin, side, size, entry, leverage, liquidation, value, upnl, roe} — the Hyperliquid shape."""
    a = await account(index)
    out = []
    for p in a.get("positions") or []:
        size = _f(p.get("position"))
        if size <= 0:
            continue
        value, margin = _f(p.get("position_value")), _f(p.get("allocated_margin"))
        imf = _f(p.get("initial_margin_fraction"))
        lev = (value / margin) if margin > 0 else (100.0 / imf if imf > 0 else 1.0)       # IMF is in percent
        out.append({"coin": p.get("symbol"), "side": "long" if int(p.get("sign") or 1) > 0 else "short", "size": size,
                    "entry": _f(p.get("avg_entry_price")), "leverage": round(max(lev, 1.0), 1), "liquidation": _f(p.get("liquidation_price")) or None,
                    "value": value, "upnl": _f(p.get("unrealized_pnl")),
                    "roe": (_f(p.get("unrealized_pnl")) / margin) if margin > 0 else 0.0, "market_id": p.get("market_id")})
    return out


async def markets() -> dict[str, int]:
    """symbol → market id (hourly cache)."""
    now = time.time()
    if now - _markets["at"] > 3600 or not _markets["by_symbol"]:
        d = await _get("orderBooks", {})
        by = {o["symbol"]: int(o["market_id"]) for o in d.get("order_books") or [] if o.get("market_type", "perp") == "perp"}
        _markets.update(at=now, by_symbol=by, by_id={v: k for k, v in by.items()})
    return _markets["by_symbol"]


async def trades(index: int, start_ms: int, market_id: int = ALL_MARKETS, max_pages: int = 30, page: int = 100) -> list[dict]:
    """An account's trades since `start_ms` (every market by default), newest first as served, paged by cursor (100 a
    page is the most the API serves; paging stops as soon as a page reaches back past `start_ms`)."""
    import asyncio
    out, cursor = [], None
    for i in range(max_pages):
        if i:
            await asyncio.sleep(0.15)
        params = {"market_id": market_id, "account_index": index, "sort_by": "timestamp", "limit": page}
        if cursor:
            params["cursor"] = cursor
        d = await _get("trades", params)
        rows = d.get("trades") or []
        out += rows
        if not rows or int(rows[-1].get("timestamp") or 0) < start_ms or not d.get("next_cursor"):
            break
        cursor = d["next_cursor"]
    return [r for r in out if int(r.get("timestamp") or 0) >= start_ms]


async def pnl_series(index: int, days: int = 60) -> list[tuple[float, float]]:
    """Daily cumulative trading P&L, $ — the raw input for the ROI curve."""
    now = int(time.time())
    d = await _get("pnl", {"by": "index", "value": index, "resolution": "1d", "start_timestamp": now - days * 86400, "end_timestamp": now, "count_back": days})
    return [(float(p["timestamp"]), _f(p.get("trade_pnl"))) for p in d.get("pnl") or [] if p.get("timestamp")]


def roi_curve(series: list[tuple[float, float]], equity: float) -> list[tuple[float, float]]:
    """Cumulative ROI (percent of today's equity) from the cumulative P&L series — the consistency input."""
    if not series or equity <= 0:
        return []
    base = series[0][1]
    return [(ts, (pnl - base) / equity * 100) for ts, pnl in series]


def episodes(trade_rows: list[dict], index: int) -> list[dict]:
    """Round trips (flat → flat) rebuilt from an account's trades with average-cost accounting: one row per episode with
    hold, P&L (price only — Lighter's taker fee is zero at the time of writing) and the P&L as a % of the notional put on."""
    by: dict[str, dict] = {}
    out = []
    for t in sorted(trade_rows, key=lambda r: int(r.get("timestamp") or 0)):
        sym = t.get("market_symbol")
        sz, px, ts = _f(t.get("size")), _f(t.get("price")), int(t.get("timestamp") or 0)
        if not sym or sz <= 0 or px <= 0:
            continue
        sgn = -1 if int(t.get("ask_account_id") or -1) == index else 1            # we sold (ask) or bought (bid)
        st = by.setdefault(sym, {"size": 0.0, "avg": 0.0, "start": None, "pnl": 0.0, "entry_notional": 0.0, "side": None})
        if abs(st["size"]) < 1e-12:
            st.update(start=ts, pnl=0.0, entry_notional=0.0, avg=0.0, side="long" if sgn > 0 else "short")
        same_way = st["size"] == 0 or (st["size"] > 0) == (sgn > 0)
        if same_way:
            total = abs(st["size"]) + sz
            st["avg"] = (st["avg"] * abs(st["size"]) + px * sz) / total
            st["entry_notional"] += sz * px
            st["size"] += sgn * sz
            continue
        closing = min(abs(st["size"]), sz)
        st["pnl"] += (px - st["avg"]) * closing * (1 if st["size"] > 0 else -1)
        remainder = sz - closing
        st["size"] += sgn * closing
        if abs(st["size"]) < 1e-12 and st["start"] is not None:
            out.append({"coin": sym, "side": st["side"], "opened_ts": st["start"] / 1000, "closed_ts": ts / 1000, "hold_h": (ts - st["start"]) / 3600000,
                        "pnl": st["pnl"], "move_pct": st["pnl"] / st["entry_notional"] * 100 if st["entry_notional"] else 0.0})
            st.update(size=0.0, avg=0.0, start=None, pnl=0.0, entry_notional=0.0)
            if remainder > 0:                                                      # flipped: the rest opens the opposite way now
                st.update(size=sgn * remainder, avg=px, start=ts, entry_notional=remainder * px, side="long" if sgn > 0 else "short")
    return out


def binance_symbol(symbol: str, perps: set[str]) -> str | None:
    """Lighter market → Binance USDT perp, or None when Binance has no such perp."""
    if not symbol:
        return None
    if symbol in ALIASES:
        return ALIASES[symbol] if ALIASES[symbol] in perps else None
    base = symbol.split("/")[0]
    if base.endswith("USD") and not base.endswith("USDT"):
        base = base[:-3]
    cand = f"{base}USDT"
    return cand if cand in perps else None


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
