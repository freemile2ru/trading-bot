"""Twitter/X social sentiment via bearer token (API v2)."""

import logging
import os
import httpx
from data.http import get_json
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_vader = SentimentIntensityAnalyzer()
_log = logging.getLogger(__name__)

# Circuit breaker — set True after first 403 to avoid hammering a dead endpoint
_twitter_disabled = False

TWITTER_BASE = "https://api.twitter.com/2"

COIN_QUERIES: dict[str, str] = {
    "BTC": "(#Bitcoin OR $BTC OR Bitcoin) lang:en -is:retweet",
    "ETH": "(#Ethereum OR $ETH OR Ethereum) lang:en -is:retweet",
    "SOL": "(#Solana OR $SOL) lang:en -is:retweet",
    "BNB": "($BNB OR Binance) lang:en -is:retweet",
    "XRP": "($XRP OR Ripple) lang:en -is:retweet",
    "ADA": "($ADA OR Cardano) lang:en -is:retweet",
}

_NEUTRAL = {"sentiment": 0.0, "label": "neutral", "tweets_analyzed": 0}


async def fetch_twitter_sentiment(symbol: str = "BTC", max_results: int = 50) -> dict:
    global _twitter_disabled

    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")
    if not bearer:
        return _NEUTRAL

    if _twitter_disabled:
        return _NEUTRAL

    coin = symbol.upper().replace("USDT", "")
    query = COIN_QUERIES.get(coin, f"${coin} lang:en -is:retweet")

    try:
        data = await get_json(
            f"{TWITTER_BASE}/tweets/search/recent",
            params={
                "query": query,
                "max_results": min(max_results, 100),
                "tweet.fields": "created_at,public_metrics",
            },
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=15,
        )
        tweets = data.get("data", [])
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            _twitter_disabled = True
            _log.warning(
                "Twitter API returned 403 Forbidden — tweet search requires a paid plan "
                "(Basic tier or higher). Twitter sentiment disabled for this session. "
                "Either upgrade your Twitter API plan or remove TWITTER_BEARER_TOKEN from .env."
            )
        else:
            _log.debug("Twitter API error %d: %s", e.response.status_code, e)
        return {**_NEUTRAL, "error": str(e)}
    except Exception as e:
        return {**_NEUTRAL, "error": str(e)}

    if not tweets:
        return _NEUTRAL

    scores = [_vader.polarity_scores(t["text"])["compound"] for t in tweets]
    avg = sum(scores) / len(scores)
    bullish = sum(1 for s in scores if s >= 0.05)
    bearish = sum(1 for s in scores if s <= -0.05)

    return {
        "tweets_analyzed": len(tweets),
        "sentiment": round(avg, 4),
        "label": _label(avg),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": len(scores) - bullish - bearish,
        "bullish_pct": round(bullish / len(scores) * 100, 1),
    }


def _label(compound: float) -> str:
    if compound >= 0.05:
        return "bullish"
    if compound <= -0.05:
        return "bearish"
    return "neutral"
