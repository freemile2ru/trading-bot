"""Shared async HTTP utility — GET with retry and exponential backoff."""

import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)


def _ascii_headers(headers: dict | None, host: str) -> dict:
    """Strip headers whose values contain non-ASCII characters (HTTP spec requires ASCII)."""
    if not headers:
        return {}
    safe = {}
    for k, v in headers.items():
        try:
            str(v).encode("ascii")
            safe[k] = v
        except UnicodeEncodeError:
            logger.warning(
                "Header %r for %s contains non-ASCII characters and was dropped. "
                "Check your API key in .env for copy-paste formatting artifacts (e.g. em-dashes).",
                k, host,
            )
    return safe


async def get_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 12.0,
    retries: int = 2,
) -> dict | list:
    """
    Async GET that retries on transient network errors and 429 rate limits.
    Raises the last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None
    host = url.split("/")[2] if "/" in url else url
    headers = _ascii_headers(headers, host)

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < retries:
                wait = 2.0 ** (attempt + 1)
                logger.warning("Rate limited by %s — retrying in %.1fs", host, wait)
                await asyncio.sleep(wait)
                last_exc = e
            else:
                raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            last_exc = e
            if attempt < retries:
                wait = 0.5 * (2 ** attempt)
                logger.debug(
                    "Network error (attempt %d/%d) for %s — retrying in %.1fs: %s",
                    attempt + 1, retries + 1, host, wait, e,
                )
                await asyncio.sleep(wait)

    raise last_exc  # type: ignore[misc]


async def get_text(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10.0,
    retries: int = 2,
) -> str:
    last_exc: Exception | None = None
    host = url.split("/")[2] if "/" in url else url

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, params=params, headers=_ascii_headers(headers, host))
                r.raise_for_status()
                return r.text.strip()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(0.5 * (2 ** attempt))
            logger.debug("Network error for %s (attempt %d): %s", host, attempt + 1, e)

    raise last_exc  # type: ignore[misc]


async def get_with_client(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    retries: int = 2,
) -> httpx.Response:
    """GET using an existing shared client — retries on transient failures."""
    last_exc: Exception | None = None
    host = url.split("/")[2] if "/" in url else url

    for attempt in range(retries + 1):
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < retries:
                await asyncio.sleep(2.0 ** (attempt + 1))
                last_exc = e
            else:
                raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            last_exc = e
            if attempt < retries:
                wait = 0.5 * (2 ** attempt)
                logger.debug("Network error (attempt %d/%d) for %s in %.1fs: %s",
                             attempt + 1, retries + 1, host, wait, e)
                await asyncio.sleep(wait)

    raise last_exc  # type: ignore[misc]


def err_text(e: BaseException) -> str:
    """An exception's message for a log line — its type when the message is empty (httpx timeouts often are)."""
    return str(e) or type(e).__name__
