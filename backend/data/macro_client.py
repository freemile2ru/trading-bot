"""Macro economic data: FRED (Fed), US Treasury yields, Fear & Greed."""

import asyncio
import os
from data.http import get_json

FRED_BASE = "https://api.stlouisfed.org/fred"

FRED_SERIES: dict[str, str] = {
    "fed_funds_rate": "FEDFUNDS",
    "cpi_yoy":        "CPIAUCSL",
    "unemployment":   "UNRATE",
    "gdp_growth":     "A191RL1Q225SBEA",
    "dxy_index":      "DTWEXBGS",
    "treasury_10y":   "DGS10",
    "treasury_2y":    "DGS2",
    "vix":            "VIXCLS",
    "sp500":          "SP500",
    "m2_money_supply":"M2SL",
}


async def fetch_fred_series(series_id: str, limit: int = 5) -> dict:
    key = os.getenv("FRED_API_KEY", "")
    if not key:
        return {"error": "FRED_API_KEY not set"}
    try:
        data = await get_json(
            f"{FRED_BASE}/series/observations",
            params={
                "series_id": series_id,
                "api_key": key,
                "file_type": "json",
                "limit": limit,
                "sort_order": "desc",
            },
            timeout=15,
        )
        obs = data.get("observations", [])
        values = [
            {"date": o["date"], "value": float(o["value"]) if o["value"] != "." else None}
            for o in obs
        ]
        latest = next((v["value"] for v in values if v["value"] is not None), None)
        return {"series_id": series_id, "latest": latest, "history": values}
    except Exception as e:
        return {"series_id": series_id, "error": str(e)}


async def fetch_macro_snapshot() -> dict:
    """Pull all macro indicators in parallel."""
    tasks = {name: fetch_fred_series(sid) for name, sid in FRED_SERIES.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    data: dict = {}
    for name, result in zip(tasks.keys(), results):
        data[name] = result if not isinstance(result, Exception) else {"error": str(result)}

    t10 = data.get("treasury_10y", {}).get("latest")
    t2 = data.get("treasury_2y", {}).get("latest")
    if t10 and t2:
        data["yield_curve_spread"] = round(t10 - t2, 3)
        data["yield_curve_inverted"] = (t10 - t2) < 0

    return data


async def fetch_fear_greed_index() -> dict:
    """Alternative.me Fear & Greed Index — free, no key needed."""
    try:
        data = await get_json("https://api.alternative.me/fng/?limit=3", timeout=10)
        entries = data.get("data", [])
        latest = entries[0] if entries else {}
        return {
            "value": int(latest.get("value", 50)),
            "classification": latest.get("value_classification", "Neutral"),
            "timestamp": latest.get("timestamp"),
            "history": [
                {
                    "value": int(e.get("value", 50)),
                    "label": e.get("value_classification"),
                    "timestamp": e.get("timestamp"),
                }
                for e in entries
            ],
        }
    except Exception as e:
        return {"value": 50, "classification": "Neutral", "error": str(e)}
