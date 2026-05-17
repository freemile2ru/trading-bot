"""News aggregation: CryptoPanic + NewsAPI for crypto/macro/geopolitical news."""

import asyncio
import os
from data.http import get_json
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_vader = SentimentIntensityAnalyzer()

CRYPTOPANIC_BASE = "https://cryptopanic.com/api/v1"
NEWSAPI_BASE = "https://newsapi.org/v2"


async def fetch_cryptopanic(
    currencies: str = "BTC,ETH",
    kind: str = "news",
    filter_: str = "hot",
    limit: int = 20,
) -> list[dict]:
    key = os.getenv("CRYPTOPANIC_API_KEY", "")
    if not key:
        return []
    try:
        data = await get_json(
            f"{CRYPTOPANIC_BASE}/posts/",
            params={
                "auth_token": key,
                "currencies": currencies,
                "kind": kind,
                "filter": filter_,
                "public": "true",
            },
            timeout=15,
        )
        return [_parse_cryptopanic_post(p) for p in data.get("results", [])[:limit]]
    except Exception:
        return []


def _parse_cryptopanic_post(p: dict) -> dict:
    title = p.get("title", "")
    scores = _vader.polarity_scores(title)
    return {
        "source": "cryptopanic",
        "title": title,
        "url": p.get("url"),
        "published_at": p.get("published_at"),
        "currencies": [c["code"] for c in p.get("currencies", [])],
        "sentiment_compound": scores["compound"],
        "sentiment_label": _label(scores["compound"]),
        "votes": {
            "positive": p.get("votes", {}).get("positive", 0),
            "negative": p.get("votes", {}).get("negative", 0),
            "important": p.get("votes", {}).get("important", 0),
        },
    }


async def fetch_newsapi(
    query: str,
    language: str = "en",
    sort_by: str = "publishedAt",
    page_size: int = 20,
) -> list[dict]:
    key = os.getenv("NEWS_API_KEY", "")
    if not key:
        return []
    try:
        data = await get_json(
            f"{NEWSAPI_BASE}/everything",
            params={
                "q": query,
                "language": language,
                "sortBy": sort_by,
                "pageSize": page_size,
                "apiKey": key,
            },
            timeout=15,
        )
        return [_parse_newsapi_article(a) for a in data.get("articles", [])]
    except Exception:
        return []


def _parse_newsapi_article(a: dict) -> dict:
    text = f"{a.get('title', '')} {a.get('description', '')}"
    scores = _vader.polarity_scores(text)
    return {
        "source": a.get("source", {}).get("name", "newsapi"),
        "title": a.get("title"),
        "description": a.get("description"),
        "url": a.get("url"),
        "published_at": a.get("publishedAt"),
        "sentiment_compound": scores["compound"],
        "sentiment_label": _label(scores["compound"]),
    }


async def fetch_all_news(symbol: str = "BTC") -> dict:
    """Fetch crypto news + macro/geopolitical news in parallel."""
    coin = symbol.replace("USDT", "")
    coin_name = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana"}.get(coin, coin)

    cp, crypto, macro, geo = await asyncio.gather(
        fetch_cryptopanic(currencies=coin, filter_="hot"),
        fetch_newsapi(query=f"{coin_name} crypto", page_size=15),
        fetch_newsapi(query="Federal Reserve interest rates inflation economy", page_size=10),
        fetch_newsapi(query="war geopolitical crisis sanctions", page_size=10),
    )

    all_articles = cp + crypto + macro + geo
    avg_sentiment = (
        sum(a["sentiment_compound"] for a in all_articles) / len(all_articles)
        if all_articles else 0.0
    )

    return {
        "cryptopanic": cp,
        "crypto_news": crypto,
        "macro_news": macro,
        "geopolitical_news": geo,
        "aggregate_sentiment": avg_sentiment,
        "aggregate_label": _label(avg_sentiment),
        "total_articles": len(all_articles),
    }


def _label(compound: float) -> str:
    if compound >= 0.05:
        return "bullish"
    if compound <= -0.05:
        return "bearish"
    return "neutral"
