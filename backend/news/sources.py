"""News sources and their parsers.

Only syndication formats are read — RSS 2.0, Atom, Google News RSS and Binance's public
announcement JSON. They're what publishers offer for automated reading, carry a publish
time on every item, and don't break when a site redesigns its article pages.

Dates: every parser returns `published` as epoch seconds (UTC) or None. What's done with a
missing, stale or future date is the collector's decision (collector.accept), not the parser's.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger("news.sources")

# A descriptive agent with a contact point — the SEC asks for one and rejects anonymous clients.
USER_AGENT = "Mozilla/5.0 (compatible; CryptoOracle/1.0; news reader; +https://localhost)"
BROWSER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
GOOGLE_PER_COIN = 30          # newest results kept per coin search


@dataclass(frozen=True)
class Source:
    name: str
    kind: str            # rss | google | binance
    url: str
    interval: int        # seconds between polls
    scope: str = "crypto"   # crypto | macro — a hint for the classifier


# Poll intervals: announcements move markets within minutes; news sites publish every few
# minutes; regulators and per-coin searches change slowly and are rate-sensitive.
SOURCES: list[Source] = [
    Source("Binance listings", "binance", "48", 120),
    Source("Binance delistings", "binance", "161", 120),
    Source("Binance news", "binance", "49", 120),
    Source("CoinDesk", "rss", "https://www.coindesk.com/arc/outboundfeeds/rss/", 300),
    Source("Cointelegraph", "rss", "https://cointelegraph.com/rss", 300),
    Source("The Block", "rss", "https://www.theblock.co/rss.xml", 300),
    Source("Decrypt", "rss", "https://decrypt.co/feed", 300),
    Source("CryptoSlate", "rss", "https://cryptoslate.com/feed/", 300),
    Source("The Defiant", "rss", "https://thedefiant.io/api/feed", 300),
    Source("Crypto Briefing", "rss", "https://cryptobriefing.com/feed/", 300),
    Source("CryptoPotato", "rss", "https://cryptopotato.com/feed/", 600),
    Source("NewsBTC", "rss", "https://www.newsbtc.com/feed/", 600),
    Source("Bitcoin Magazine", "rss", "https://bitcoinmagazine.com/feed", 600),
    Source("Unchained", "rss", "https://unchainedcrypto.com/feed/", 600),
    # Left out: Blockworks and DL News — both feeds' newest items were months old (2026-09-29).
    Source("SEC", "rss", "https://www.sec.gov/news/pressreleases.rss", 900, "macro"),
    Source("Federal Reserve", "rss", "https://www.federalreserve.gov/feeds/press_all.xml", 900, "macro"),
    # One entry stands for a search per coin in the scanned universe (collector expands it).
    Source("Google News", "google", "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en", 900),
]

BINANCE_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
BINANCE_ARTICLE = "https://www.binance.com/en/support/announcement/detail/{code}"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean(text: str | None, limit: int = 600) -> str:
    """Plain text from an HTML-ish feed field."""
    if not text:
        return ""
    t = _WS.sub(" ", html.unescape(_TAG.sub(" ", html.unescape(text)))).strip()
    return t[:limit]


def parse_date(value: str | None) -> float | None:
    """RFC 822 (RSS), ISO 8601 (Atom) or epoch; always UTC epoch seconds. Naive times are UTC."""
    if not value:
        return None
    v = value.strip()
    try:
        dt = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element, *names: str) -> str | None:
    c = _child(el, *names)
    return (c.text or "").strip() if c is not None and c.text else None


def _xml(raw: bytes) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        # The usual defect: a bare "&" in a title. Escape those and try once more.
        fixed = re.sub(rb"&(?!(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);)", b"&amp;", raw)
        return ET.fromstring(fixed)


def parse_feed(raw: bytes, source: str) -> list[dict]:
    """Items from an RSS 2.0 or Atom document."""
    root = _xml(raw)
    out = []
    if _local(root.tag) == "feed":                                   # Atom
        for e in root:
            if _local(e.tag) != "entry":
                continue
            link = next((c.get("href") for c in e if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate"), None)
            out.append({"title": clean(_text(e, "title"), 300), "url": link,
                        "summary": clean(_text(e, "summary", "content")),
                        "published": parse_date(_text(e, "published", "updated")), "source": source})
        return out
    channel = _child(root, "channel")
    if channel is None:                     # an Element with no children is falsy — never use `or` here
        channel = root
    for it in channel:
        if _local(it.tag) != "item":
            continue
        title = clean(_text(it, "title"), 300)
        publisher = _child(it, "source")
        out.append({"title": title, "url": _text(it, "link"), "summary": clean(_text(it, "description")),
                    "published": parse_date(_text(it, "pubDate", "date", "published", "updated")),
                    "source": source,
                    # Google News: the real outlet is in <source>; strip its " - Outlet" title suffix.
                    "publisher": (publisher.text or "").strip() if publisher is not None else None})
    return out


def google_query(name: str, symbol: str) -> str:
    """A per-coin search: the name AND a crypto word, so "Near" or "Sui" don't match the dictionary."""
    return quote_plus(f'"{name}" (crypto OR token OR {symbol}) when:1d')


def strip_publisher(title: str, publisher: str | None) -> str:
    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(publisher) - 3]
    return title


def parse_binance(data: dict, source: str) -> list[dict]:
    out = []
    for cat in (data.get("data") or {}).get("catalogs") or []:
        for a in cat.get("articles") or []:
            out.append({"title": clean(a.get("title"), 300), "url": BINANCE_ARTICLE.format(code=a.get("code")),
                        "summary": "", "published": (a.get("releaseDate") or 0) / 1000 or None, "source": source})
    return out


async def fetch(client: httpx.AsyncClient, src: Source, url: str | None = None) -> list[dict]:
    """One poll of one source (for Google, one coin's search URL)."""
    if src.kind == "binance":
        # Binance's edge refuses agents that don't look like a browser (403); its public site JSON is the same data.
        r = await client.get(BINANCE_URL, params={"type": 1, "pageNo": 1, "pageSize": 20, "catalogId": src.url},
                             headers={"User-Agent": BROWSER_AGENT, "Accept": "application/json"})
        r.raise_for_status()
        return parse_binance(r.json(), src.name)
    r = await client.get(url or src.url)
    r.raise_for_status()
    items = parse_feed(r.content, src.name)
    if src.kind == "google":
        for it in items:
            it["title"] = strip_publisher(it["title"], it.get("publisher"))
            it["summary"] = ""                        # Google's description is only the headline again
        items = sorted(items, key=lambda i: i["published"] or 0, reverse=True)[:GOOGLE_PER_COIN]
    return items


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT})
