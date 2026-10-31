"""Hyperliquid, read-only: the public leaderboard, any wallet's live positions and its fills.

Everything on Hyperliquid is on-chain, so a wallet's record can't be curated: the leaderboard
(stats-data.hyperliquid.xyz) lists ~50k wallets with day/week/month/all-time P&L, ROI and volume; the info API
returns a wallet's open positions (coin, signed size, entry, leverage, liquidation) and its fills, from which a
trading profile and a P&L curve are rebuilt here. No key, nothing is sent beyond the query.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("data.hyperliquid")

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO = "https://api.hyperliquid.xyz/info"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
PERP_DIRS = ("Open Long", "Open Short", "Close Long", "Close Short", "Long > Short", "Short > Long", "Liquidated Long", "Liquidated Short")


async def _post(body: dict) -> dict | list:
    """The info API; a 429 is waited out (the public limit is modest) rather than failed."""
    import asyncio
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(4):
            r = await client.post(INFO, json=body, headers={**HEADERS, "content-type": "application/json"})
            if r.status_code == 429 and attempt < 3:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
    return {}


async def leaderboard() -> list[dict]:
    """Every wallet on the board, flattened: {address, name, equity, day/week/month/all: {pnl, roi, volume}}."""
    import httpx
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.get(LEADERBOARD, headers=HEADERS)
        r.raise_for_status()
        d = r.json()
    rows = d.get("leaderboardRows") if isinstance(d, dict) else d
    return [flatten(x) for x in rows or []]


def flatten(x: dict) -> dict:
    w = {k: v for k, v in (x.get("windowPerformances") or [])}
    out = {"address": x.get("ethAddress"), "name": x.get("displayName"), "equity": _f(x.get("accountValue"))}
    for key, src in (("day", "day"), ("week", "week"), ("month", "month"), ("all", "allTime")):
        p = w.get(src) or {}
        out[key] = {"pnl": _f(p.get("pnl")), "roi": _f(p.get("roi")), "volume": _f(p.get("vlm"))}
    return out


# Builder-deployed markets (HIP-3): "xyz" lists stock, commodity, index and FX perps as xyz:TSLA, xyz:GOLD… Their
# positions live in a separate clearinghouse state (dex="xyz"); fills carry the prefixed coin name.
DEXES = ("", "xyz")
XYZ_TO_BINANCE = {"GOLD": "XAUUSDT", "SILVER": "XAGUSDT", "CL": "CLUSDT", "BRENTOIL": "BZUSDT", "COPPER": "COPPERUSDT", "NATGAS": "NATGASUSDT",
                  "PLATINUM": "XPTUSDT", "PALLADIUM": "XPDUSDT", "SMSN": "SAMSUNGUSDT", "SKHX": "SKHYNIXUSDT", "SP500": "SPYUSDT", "XYZ100": "QQQUSDT"}


async def positions(address: str) -> list[dict]:
    """Open perp positions on the main market and the builder markets: {coin, side, size, entry, leverage, liquidation,
    value, upnl, roe}; builder coins keep their prefix (xyz:TSLA)."""
    out = []
    for dex in DEXES:
        body = {"type": "clearinghouseState", "user": address}
        if dex:
            body["dex"] = dex
        try:
            st = await _post(body)
        except Exception as e:
            if not dex:
                raise
            logger.debug("Hyperliquid %s positions for %s unavailable: %s", dex, address, e)
            continue
        out += _positions_of(st)
    return out


def _positions_of(st: dict) -> list[dict]:
    out = []
    for ap in st.get("assetPositions") or []:
        p = ap.get("position") or {}
        szi = _f(p.get("szi"))
        if not szi:
            continue
        out.append({"coin": p.get("coin"), "side": "long" if szi > 0 else "short", "size": abs(szi), "entry": _f(p.get("entryPx")),
                    "leverage": _f((p.get("leverage") or {}).get("value")) or 1.0, "liquidation": _f(p.get("liquidationPx")) or None,
                    "value": _f(p.get("positionValue")), "upnl": _f(p.get("unrealizedPnl")), "roe": _f(p.get("returnOnEquity"))})
    return out


async def fills(address: str, start_ms: int, max_pages: int = 2) -> list[dict]:
    """Fills since `start_ms`, oldest first (2,000 a page, paced for the public rate limit)."""
    import asyncio
    out, t = [], start_ms
    for i in range(max_pages):
        if i:
            await asyncio.sleep(0.5)
        page = await _post({"type": "userFillsByTime", "user": address, "startTime": t})
        if not isinstance(page, list) or not page:
            break
        out += page
        if len(page) < 2000:
            break
        t = int(page[-1]["time"]) + 1
    return out


def episodes(fill_rows: list[dict]) -> list[dict]:
    """Perp positions rebuilt from fills: one row per round trip (from flat back to flat) with hold, P&L after fees
    and the P&L as a % of the notional put on. Spot fills and dust are ignored."""
    by: dict[str, dict] = {}
    out = []
    for f in sorted(fill_rows, key=lambda f: f["time"]):
        if f.get("dir") not in PERP_DIRS or str(f.get("coin", "")).startswith("@"):
            continue
        c = f["coin"]
        st = by.setdefault(c, {"size": 0.0, "start": None, "pnl": 0.0, "entry_notional": 0.0, "side": None})
        sz, px = _f(f.get("sz")), _f(f.get("px"))
        sgn = 1 if f.get("side") == "B" else -1
        if abs(st["size"]) < 1e-9:
            st.update(start=f["time"], pnl=0.0, entry_notional=0.0, side="long" if sgn > 0 else "short")
        new_size = round(st["size"] + sgn * sz, 10)
        flipped = st["size"] * new_size < 0                       # Long > Short in one fill: the old episode ends here
        closing = min(abs(st["size"]), sz) if flipped else 0.0
        if f["dir"].startswith("Open") or (flipped and sz > closing):
            st["entry_notional"] += (sz - closing) * px
        st["size"] = new_size
        st["pnl"] += _f(f.get("closedPnl")) - _f(f.get("fee"))
        if (abs(st["size"]) < 1e-9 or flipped) and st["start"]:
            out.append({"coin": c, "side": st["side"], "opened_ts": st["start"] / 1000, "closed_ts": f["time"] / 1000,
                        "hold_h": (f["time"] - st["start"]) / 3600000, "pnl": st["pnl"],
                        "move_pct": st["pnl"] / st["entry_notional"] * 100 if st["entry_notional"] else 0.0})
            st["start"] = None
            if flipped:                                            # the remainder starts the opposite episode now
                st.update(start=f["time"], pnl=0.0, entry_notional=abs(new_size) * px, side="long" if new_size > 0 else "short")
    return out


def pnl_curve(fill_rows: list[dict], equity: float, days: int = 60) -> list[tuple[float, float]]:
    """A daily cumulative ROI curve (percent of today's equity) from closed P&L — the consistency input."""
    import datetime as dt
    by_day: dict[str, float] = {}
    for f in fill_rows:
        if f.get("dir") not in PERP_DIRS:
            continue
        d = dt.datetime.fromtimestamp(f["time"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        by_day[d] = by_day.get(d, 0.0) + _f(f.get("closedPnl")) - _f(f.get("fee"))
    if not by_day or equity <= 0:
        return []
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    out, cum = [], 0.0
    for i in range(days + 1):
        day = start + dt.timedelta(days=i)
        cum += by_day.get(day.strftime("%Y-%m-%d"), 0.0)
        out.append((day.timestamp(), cum / equity * 100))
    return out


def binance_symbol(coin: str, perps: set[str]) -> str | None:
    """Hyperliquid coin → Binance USDT perp: BTC → BTCUSDT, kPEPE → 1000PEPEUSDT, xyz:TSLA → TSLAUSDT, xyz:GOLD → XAUUSDT, else None."""
    if not coin or coin.startswith("@"):
        return None
    if ":" in coin:
        dex, name = coin.split(":", 1)
        if dex != "xyz":
            return None
        cand = XYZ_TO_BINANCE.get(name, f"{name}USDT")
        return cand if cand in perps else None
    cands = [f"{coin}USDT"]
    if coin.startswith("k"):
        cands.append(f"1000{coin[1:]}USDT")
    return next((c for c in cands if c in perps), None)


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def now_ms() -> int:
    return int(time.time() * 1000)
