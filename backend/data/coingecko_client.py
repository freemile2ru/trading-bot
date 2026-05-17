"""CoinGecko public API — market data, dominance, coin metadata."""

import logging
import os
from data.http import get_json

_log = logging.getLogger(__name__)

BASE = "https://api.coingecko.com/api/v3"

SYMBOL_TO_ID: dict[str, str] = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "BNBUSDT": "binancecoin",
    "SOLUSDT": "solana",
    "XRPUSDT": "ripple",
    "ADAUSDT": "cardano",
    "DOGEUSDT": "dogecoin",
    "AVAXUSDT": "avalanche-2",
    "LINKUSDT": "chainlink",
    "DOTUSDT": "polkadot",
    "MATICUSDT": "matic-network",
    "UNIUSDT": "uniswap",
}


def _headers() -> dict:
    key = os.getenv("COINGECKO_API_KEY", "").strip()
    if not key:
        return {}
    try:
        key.encode("ascii")
        return {"x-cg-demo-api-key": key}
    except UnicodeEncodeError:
        _log.warning(
            "COINGECKO_API_KEY contains non-ASCII characters (e.g. an em-dash from copy-pasting). "
            "Falling back to unauthenticated CoinGecko requests. Fix your .env file."
        )
        return {}


async def fetch_coin_data(coin_id: str) -> dict:
    try:
        d = await get_json(
            f"{BASE}/coins/{coin_id}",
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "true",
                "developer_data": "false",
            },
            headers=_headers(),
            timeout=15,
        )
        md = d.get("market_data", {})
        cd = d.get("community_data", {})
        return {
            "id": coin_id,
            "name": d.get("name"),
            "symbol": d.get("symbol"),
            "market_cap_rank": d.get("market_cap_rank"),
            "price_usd": md.get("current_price", {}).get("usd"),
            "market_cap_usd": md.get("market_cap", {}).get("usd"),
            "total_volume_usd": md.get("total_volume", {}).get("usd"),
            "price_change_24h_pct": md.get("price_change_percentage_24h"),
            "price_change_7d_pct": md.get("price_change_percentage_7d"),
            "price_change_30d_pct": md.get("price_change_percentage_30d"),
            "ath": md.get("ath", {}).get("usd"),
            "ath_change_pct": md.get("ath_change_percentage", {}).get("usd"),
            "circulating_supply": md.get("circulating_supply"),
            "max_supply": md.get("max_supply"),
            "twitter_followers": cd.get("twitter_followers"),
            "reddit_subscribers": cd.get("reddit_subscribers"),
            "sentiment_votes_up_pct": d.get("sentiment_votes_up_percentage"),
        }
    except Exception as e:
        return {"error": str(e)}


async def fetch_global_market() -> dict:
    """BTC dominance, total market cap, fear/greed proxy."""
    try:
        data = await get_json(f"{BASE}/global", headers=_headers(), timeout=15)
        d = data.get("data", {})
        return {
            "total_market_cap_usd": d.get("total_market_cap", {}).get("usd"),
            "total_volume_24h_usd": d.get("total_volume", {}).get("usd"),
            "btc_dominance_pct": d.get("market_cap_percentage", {}).get("btc"),
            "eth_dominance_pct": d.get("market_cap_percentage", {}).get("eth"),
            "active_cryptocurrencies": d.get("active_cryptocurrencies"),
            "market_cap_change_24h_pct": d.get("market_cap_change_percentage_24h_usd"),
        }
    except Exception as e:
        return {"error": str(e)}


async def get_for_symbol(binance_symbol: str) -> dict:
    coin_id = SYMBOL_TO_ID.get(binance_symbol.upper())
    if not coin_id:
        return {"error": f"No CoinGecko mapping for {binance_symbol}"}
    return await fetch_coin_data(coin_id)
