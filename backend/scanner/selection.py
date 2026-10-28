"""Narrows the top-30 pool to the ten most tradeable coins right now.

Market cap says what's big, not what's worth trading this hour. Ranking uses live
Binance 24h stats (one batched request) plus CMC's momentum fields, scored
cross-sectionally — each component is a percentile within the pool, so the score adapts
to quiet and wild markets alike instead of relying on fixed thresholds.

  liquidity   0.30  Binance quote volume — can we get in and out cleanly
  range       0.25  intraday high-low range — is there movement to capture
                    (penalised past RANGE_CEILING_PCT: that's chaos, not opportunity)
  momentum    0.20  |24h| and |1h| change — is it moving now
  coherence   0.15  1h / 24h / 7d agree in sign — trends are tradeable, chop isn't
  activity    0.10  CMC volume change — is participation rising

Incumbents get a small bonus so the selection doesn't churn on noise between sweeps.
"""

import hashlib
import logging

from data import cache
from data.binance_client import fetch_tickers_24h

logger = logging.getLogger("scanner.selection")

WEIGHTS = {"liquidity": 0.30, "range": 0.25, "momentum": 0.20, "coherence": 0.15, "activity": 0.10}
MIN_QUOTE_VOLUME = 20_000_000     # USD / 24h on Binance spot — below this, fills are fiction
DRY_MIN_QUOTE_VOLUME = 5_000_000  # the wider net (autotrader dry mode): the top-50 by cap mostly sit under $20M of spot volume
RANGE_CEILING_PCT = 15.0
INCUMBENT_BONUS = 0.05
TICKERS_TTL = 60


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    """Rank-based percentile in [0, 1]; ties share the average rank."""
    if not values:
        return {}
    if len(values) == 1:
        return {k: 1.0 for k in values}
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    out, i = {}, 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        pct = ((i + j) / 2) / (len(ordered) - 1)
        for k in range(i, j + 1):
            out[ordered[k][0]] = pct
        i = j + 1
    return out


def _sign(v) -> int:
    v = v or 0
    return 1 if v > 0 else -1 if v < 0 else 0


def score_pool(pool: list[dict], tickers: dict[str, dict], incumbents: set[str], min_volume: float = MIN_QUOTE_VOLUME) -> list[dict]:
    eligible = [
        c for c in pool
        if tickers.get(c["pair"], {}).get("quote_volume", 0) >= min_volume
    ]
    t = {c["pair"]: tickers[c["pair"]] for c in eligible}

    liquidity = _percentiles({p: v["quote_volume"] for p, v in t.items()})
    rng = _percentiles({p: min(v["range_pct"], RANGE_CEILING_PCT) for p, v in t.items()})
    momentum = _percentiles({
        c["pair"]: 0.6 * abs(t[c["pair"]]["price_change_pct"]) + 0.4 * abs(c.get("percent_change_1h") or 0) * 4
        for c in eligible
    })
    activity = _percentiles({c["pair"]: c.get("volume_change_24h") or 0 for c in eligible})

    ranked = []
    for c in eligible:
        pair = c["pair"]
        signs = {_sign(c.get("percent_change_1h")), _sign(t[pair]["price_change_pct"]),
                 _sign(c.get("percent_change_7d"))} - {0}
        coherence = 1.0 if len(signs) == 1 else 0.3

        range_score = rng[pair]
        if t[pair]["range_pct"] > RANGE_CEILING_PCT:
            range_score *= 0.5

        components = {
            "liquidity": liquidity[pair],
            "range": range_score,
            "momentum": momentum[pair],
            "coherence": coherence,
            "activity": activity[pair],
        }
        score = sum(WEIGHTS[k] * v for k, v in components.items())
        if pair in incumbents:
            score += INCUMBENT_BONUS

        ranked.append({
            **c,
            "tradeability": round(score, 3),
            "tradeability_detail": {
                **{k: round(v, 2) for k, v in components.items()},
                "quote_volume": round(t[pair]["quote_volume"]),
                "range_pct": round(t[pair]["range_pct"], 2),
                "incumbent": pair in incumbents,
            },
        })

    ranked.sort(key=lambda c: c["tradeability"], reverse=True)
    return ranked


async def select(
    pool: list[dict],
    size: int,
    incumbents: set[str],
    must_include: set[str],
    min_volume: float = MIN_QUOTE_VOLUME,
) -> tuple[list[dict], list[dict]]:
    """Returns (coins to scan, full ranking).

    The scan set is the top `size` by tradeability plus anything in `must_include` (open
    positions, pinned coins, live signals) — those need their 1-minute checks whatever
    their rank, but they don't take one of the ten slots.
    """
    pairs = [c["pair"] for c in pool]
    try:
        tickers = await cache.cached(
            # Keyed on the whole pool: size + first pair collided when the pool changed membership.
            cache.key("binance", "tickers24h", hashlib.sha1(",".join(sorted(pairs)).encode()).hexdigest()[:16]),
            TICKERS_TTL,
            lambda: fetch_tickers_24h(pairs),
        )
    except Exception as e:
        # No live stats — fall back to market-cap order rather than scanning nothing.
        logger.warning("Tradeability ranking unavailable (%s) — using market-cap order", e)
        head = pool[:size]
        extra = [c for c in pool if c["pair"] in must_include and c not in head]
        return head + extra, []

    ranked = score_pool(pool, tickers or {}, incumbents, min_volume)
    for i, c in enumerate(ranked, 1):
        c["tradeability_rank"] = i

    chosen = ranked[:size]
    chosen_pairs = {c["pair"] for c in chosen}
    by_pair = {c["pair"]: c for c in ranked} | {c["pair"]: c for c in pool if c["pair"] not in {r["pair"] for r in ranked}}
    held = [by_pair[p] | {"held_only": True} for p in must_include if p not in chosen_pairs and p in by_pair]

    logger.info(
        "Selected %d of %d: %s%s",
        len(chosen), len(pool),
        ", ".join(f"{c['symbol']}({c['tradeability']:.2f})" for c in chosen),
        f" + held {[c['symbol'] for c in held]}" if held else "",
    )
    return chosen + held, ranked
