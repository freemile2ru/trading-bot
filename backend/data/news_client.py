"""News for one coin: the scraped, Claude-classified headlines (news/) plus CryptoPanic.

NewsAPI was dropped on 2026-09-29: its free tier (100 calls a day) meant per-coin news was
cached for 6 hours, and its results were scored by VADER. The scraper reads 15 sources on
their own schedules and Claude reads every headline — see news/.
"""

import logging
import os

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from data import cache
from data.http import get_json

logger = logging.getLogger("data.news")

CRYPTOPANIC_TTL = 1800
CRYPTOPANIC_WEIGHT = 0.2         # of the blended sentiment, when CryptoPanic has posts for the coin

_vader = SentimentIntensityAnalyzer()

CRYPTOPANIC_BASE = "https://cryptopanic.com/api/v1"


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


async def fetch_all_news(symbol: str = "BTC") -> dict:
    """The coin's news reading. Scraped news is local (news.db), so only CryptoPanic is cached."""
    from news import scoring
    coin = symbol.removesuffix("USDT")
    try:
        cp = await cache.cached(cache.key("cryptopanic", coin), CRYPTOPANIC_TTL,
                                lambda: fetch_cryptopanic(currencies=coin, filter_="hot"))
    except Exception:
        cp = []
    n = scoring.for_coin(coin)
    score = n["score"]
    if cp:
        cp_avg = sum(a["sentiment_compound"] for a in cp) / len(cp)
        score = (1 - CRYPTOPANIC_WEIGHT) * score + CRYPTOPANIC_WEIGHT * cp_avg
    market = n["market"]
    return {
        "cryptopanic": cp,
        "crypto_news": n["top"],
        "macro_news": market["top"],
        "geopolitical_news": [],
        "aggregate_sentiment": round(score, 4),
        "aggregate_label": _label(score),
        "total_articles": n["stories"] + market["stories"] + len(cp),
        # Tells analysis.scoring the aggregate is already the final news score.
        "scraped": {"score": n["score"], "coin_score": n["coin_score"], "market_score": market["score"],
                    "stories": n["stories"], "stories_6h": n["stories_6h"], "burst": n["burst"]},
    }


def _label(compound: float) -> str:
    if compound >= 0.05:
        return "bullish"
    if compound <= -0.05:
        return "bearish"
    return "neutral"
