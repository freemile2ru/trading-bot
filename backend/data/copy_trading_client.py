"""Binance Futures copy-trading, read-only: the public lead-trader leaderboard, a leader's profile, and their
position history — which includes what they hold right now. These are the endpoints Binance's own copy-trading
pages call; no login, no key, nothing is ever sent beyond the query. They are not part of the official API and
may change, so every call is defensive and the book caches the last good answer.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("data.copy_trading")

BASE = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "clienttype": "web", "lang": "en", "content-type": "application/json"}
TIME_RANGES = ("7D", "30D", "90D", "180D")
SORTS = ("ROI", "PNL", "SHARP_RATIO", "COPIER_PNL", "MDD", "AUM")


async def _post(path: str, body: dict) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{BASE}/{path}", json=body, headers=HEADERS)
        r.raise_for_status()
        d = r.json()
    if d.get("code") != "000000":
        raise RuntimeError(f"copy-trade {path}: {d.get('message') or d.get('code')}")
    return d.get("data") or {}


async def _get(path: str, params: dict) -> dict:
    from data.http import get_json
    d = await get_json(f"{BASE}/{path}", params=params, headers=HEADERS, timeout=20.0)
    if d.get("code") != "000000":
        raise RuntimeError(f"copy-trade {path}: {d.get('message') or d.get('code')}")
    return d.get("data") or {}


async def leaderboard(time_range: str = "90D", sort: str = "ROI", page: int = 1, page_size: int = 50) -> list[dict]:
    """One page of lead portfolios, best first by `sort`, with their daily ROI curve over `time_range`."""
    d = await _post("home-page/query-list", {"pageNumber": page, "pageSize": page_size, "timeRange": time_range, "dataType": sort,
                                             "favoriteOnly": False, "hideFull": False, "nickname": "", "order": "DESC",
                                             "userAsset": 0, "portfolioType": "ALL"})
    out = []
    for x in d.get("list") or []:
        out.append({"id": str(x.get("leadPortfolioId")), "nickname": x.get("nickname"), "roi": _f(x.get("roi")), "pnl": _f(x.get("pnl")),
                    "aum": _f(x.get("aum")), "mdd": _f(x.get("mdd")), "win_rate": _f(x.get("winRate")), "copiers": int(x.get("currentCopyCount") or 0),
                    "max_copiers": int(x.get("maxCopyCount") or 0), "copier_pnl": _f(x.get("copierPnl")), "sharpe": _f(x.get("sharpRatio")),
                    "tradfi": bool(x.get("tradFiTag")), "api": x.get("apiKeyTag") == "API_KEY_TRADE",
                    "curve": [(int(c["dateTime"]) / 1000, _f(c.get("value"))) for c in (x.get("chartItems") or []) if c.get("dataType") == "ROI"]})
    return out


async def detail(portfolio_id: str) -> dict:
    x = await _get("lead-portfolio/detail", {"portfolioId": portfolio_id})
    tags = [t.get("tagName") for t in (x.get("tagItemVos") or [])]
    return {"id": str(x.get("leadPortfolioId")), "nickname": x.get("nickname"), "nickname_en": x.get("nicknameTranslate"),
            "status": x.get("status"), "start_ts": (x.get("startTime") or 0) / 1000, "margin_balance": _f(x.get("marginBalance")),
            "aum": _f(x.get("aumAmount")), "copier_pnl": _f(x.get("copierPnl")), "copiers": int(x.get("currentCopyCount") or 0),
            "total_copiers": int(x.get("totalCopyCount") or 0), "close_lead_count": int(x.get("closeLeadCount") or 0),
            "profit_share_pct": _f(x.get("profitSharingRate")), "sharpe": _f(x.get("sharpRatio")), "position_show": bool(x.get("positionShow")),
            "badge": x.get("badgeName"), "tags": tags, "leverage_tier": next((t for t in tags if t.endswith("_LEVERAGE")), None)}


async def positions(portfolio_id: str, pages: int = 1, page_size: int = 50) -> list[dict]:
    """Position history, newest first — rows with `closed` None are what the leader holds now."""
    out = []
    for page in range(1, pages + 1):
        d = await _post("lead-portfolio/position-history", {"portfolioId": portfolio_id, "pageNumber": page, "pageSize": page_size})
        rows = d.get("list") or []
        for x in rows:
            qty, closed_qty = _f(x.get("maxOpenInterest")), _f(x.get("closedVolume"))
            out.append({"position_id": str(x.get("positionId")), "symbol": x.get("symbol"), "side": (x.get("side") or "").lower(),
                        "opened_ts": (x.get("opened") or 0) / 1000, "closed_ts": (x.get("closed") or 0) / 1000 or None,
                        "entry": _f(x.get("avgCost")), "close_price": _f(x.get("avgClosePrice")) or None, "pnl": _f(x.get("closingPnl")),
                        "roi": _f(x.get("roi")), "leverage": _f(x.get("leverage")) or 1.0, "qty": qty, "closed_qty": closed_qty,
                        "remaining": max(0.0, 1 - closed_qty / qty) if qty else 0.0, "status": x.get("status"),
                        "update_ts": (x.get("updateTime") or 0) / 1000})
        if len(rows) < page_size:
            break
    return out


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def curve_stats(curve: list[tuple[float, float]], active_only: bool = False) -> dict:
    """From a cumulative daily ROI curve (percent): max drawdown, daily Sharpe, share of positive weeks, days with data.
    `active_only` judges the weeks and days on which something happened — for a wallet's closed-P&L curve, a week
    without a trade is neither good nor bad."""
    pts = [(t, v) for t, v in sorted(curve) if v is not None]
    first = next((i for i, (_, v) in enumerate(pts) if v), len(pts))
    pts = pts[max(first - 1, 0):]
    if len(pts) < 10:
        return {"days": len(pts), "mdd_pct": None, "sharpe": None, "weeks_positive_pct": None, "roi_pct": pts[-1][1] if pts else None}
    eq = [1 + v / 100 for _, v in pts]
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    import math
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1))
    weeks = [sum(rets[i:i + 7]) for i in range(0, len(rets), 7) if len(rets[i:i + 7]) >= 5]
    if active_only:
        weeks = [w for w in weeks if abs(w) > 1e-9]
        active = [r for r in rets if abs(r) > 1e-9]
        if len(active) >= 2:
            mean = sum(active) / len(active)
            sd = math.sqrt(sum((r - mean) ** 2 for r in active) / (len(active) - 1))
    return {"days": len(pts), "active_days": sum(1 for r in rets if abs(r) > 1e-9), "active_weeks": len(weeks),
            "mdd_pct": round(mdd * 100, 2), "sharpe": round(mean / sd * math.sqrt(365), 2) if sd else None,
            "weeks_positive_pct": round(100 * sum(w > 0 for w in weeks) / len(weeks), 1) if weeks else None,
            "roi_pct": round(pts[-1][1], 2), "last_30d_pct": round((eq[-1] / eq[max(len(eq) - 31, 0)] - 1) * 100, 2),
            "last_7d_pct": round((eq[-1] / eq[max(len(eq) - 8, 0)] - 1) * 100, 2), "fetched_at": time.time()}
