"""Watchlist membership — which symbols earn the 1-minute check instead of the 5-minute one.

The list is deliberately hard to get onto and slow to fall off. Promotion is cheap to
trigger (any one reason) because the cost of watching a quiet coin is one extra cached
scan a minute; demotion requires *every* condition to clear, plus a minimum dwell time,
because a symbol that flickers on and off the list is worse than useless — it produces
gaps exactly when a setup is resolving.
"""

import logging
import time

from autotrader.engine import open_symbols as autotrader_open_symbols
from storage import signals as signal_log
from storage import state

logger = logging.getLogger("scanner.watchlist")

KEY = state.key("watchlist")

MAX_SIZE = 6                    # bounds the 1-minute tier's cost deterministically
PROMOTE_CONVICTION = 0.30       # "approaching a signal" floor
SIGNAL_CONVICTION = 0.45        # the escalation threshold it's approaching
DEMOTE_CONVICTION = 0.20
DEMOTE_CONSECUTIVE_CHECKS = 3   # must decay for 3 checks, not one dip
MIN_DWELL_SECONDS = 600         # 10 min — anti-flapping floor
MAX_DWELL_SECONDS = 14400       # 4h — stops stale entries accumulating
# Anti-churn: with more qualifying coins than slots, near-equal convictions used to swap
# places every sweep (SOL was added 19× and removed 27× in 7.5h, and each removal closed
# an autotrader position). A newcomer now has to be clearly better, incumbents get time
# to prove themselves, and a displaced coin can't bounce straight back in.
DISPLACE_MARGIN = 0.10          # newcomer |conviction| must beat the incumbent's by this
MIN_TENURE_SECONDS = 1200       # 20 min before an entry can be displaced or cap-trimmed
REJOIN_COOLDOWN_SECONDS = 900   # 15 min before a displaced/trimmed coin may be re-added
VOLATILITY_EXPANSION = 1.40     # ATR% vs its previous reading
VOLUME_SPIKE = 2.0
VOLUME_NORMAL = 1.2


def _direction_of(label: str | None) -> int:
    if not label:
        return 0
    low = label.lower()
    if "bull" in low or "buy" in low:
        return 1
    if "bear" in low or "sell" in low:
        return -1
    return 0


def _rejoin_key(symbol: str) -> str:
    return state.key("watchlist", "left", symbol)


async def _mark_left(symbol: str) -> None:
    await state.set(_rejoin_key(symbol), time.time(), REJOIN_COOLDOWN_SECONDS)


async def get_all() -> dict[str, dict]:
    return await state.hgetall(KEY)


async def symbols() -> list[str]:
    return list((await get_all()).keys())


async def _open_trade_symbols() -> set[str]:
    try:
        from live import swing as live_swing
        # Only the main wallet (paper or live) protects a symbol. The autotrader deliberately
        # does not: leaving the watchlist tightens its positions' stops instead.
        return {t.get("symbol") for w in live_swing.wallets("main") for t in w.get_portfolio()["open_trades"]}
    except Exception as e:
        logger.debug("Could not read open trades: %s", e)
        return set()


def promotion_reasons(
    scan: dict,
    previous: dict | None,
    has_open_trade: bool,
    recently_fired: bool = False,
) -> list[str]:
    """Every reason this symbol deserves 1-minute attention. Empty means it doesn't."""
    reasons: list[str] = []
    conviction = abs(scan.get("conviction", 0.0))
    triggers = scan.get("triggers", {})
    prev_triggers = (previous or {}).get("triggers", {})

    if has_open_trade:
        reasons.append("open paper position — SL/TP needs 1-minute monitoring")

    # A fired signal must stay visible while it is still tradeable, and its follow-through
    # or invalidation is exactly what you want checked every minute.
    if recently_fired:
        reasons.append("signal fired recently — tracking follow-through")

    # Approaching the escalation threshold, and still building.
    if PROMOTE_CONVICTION <= conviction < SIGNAL_CONVICTION:
        prev_conviction = abs((previous or {}).get("conviction", 0.0))
        if previous is None or conviction > prev_conviction:
            reasons.append(f"conviction {conviction:.2f} rising toward signal threshold")

    # A setup that is armed but not yet resolved — timing matters at this point.
    if triggers.get("bb_squeeze"):
        reasons.append("Bollinger squeeze — breakout pending")
    if triggers.get("divergence") in ("bullish", "bearish"):
        reasons.append(f"{triggers['divergence']} RSI divergence forming")
    if triggers.get("near_level"):
        reasons.append(
            f"price {triggers.get('distance_to_level_pct', 0):.2f}% from a key level"
        )

    # Something is happening right now.
    volume_ratio = triggers.get("volume_ratio", 1.0)
    if volume_ratio > VOLUME_SPIKE:
        reasons.append(f"volume {volume_ratio:.1f}x average")

    atr = triggers.get("atr_pct", 0.0)
    prev_atr = prev_triggers.get("atr_pct", 0.0)
    if prev_atr and atr > prev_atr * VOLATILITY_EXPANSION:
        reasons.append(f"volatility expanding ({prev_atr:.2f}% → {atr:.2f}% ATR)")

    # A timeframe changed its mind since the last sweep — earliest warning available.
    if previous:
        for tf, label in (triggers.get("tf_labels") or {}).items():
            prev_label = (prev_triggers.get("tf_labels") or {}).get(tf)
            now_dir, prev_dir = _direction_of(label), _direction_of(prev_label)
            if now_dir and prev_dir and now_dir != prev_dir:
                reasons.append(f"{tf} flipped {prev_label} → {label}")
                break

    return reasons


def _can_demote(
    scan: dict, entry: dict, has_open_trade: bool, recently_fired: bool = False
) -> tuple[bool, str]:
    """Demotion needs every condition to clear at once."""
    if entry.get("pinned"):
        return False, "pinned"
    if has_open_trade:
        return False, "open position"
    if recently_fired:
        return False, "signal still live"

    age = time.time() - entry.get("added_at", 0)
    if age < MIN_DWELL_SECONDS:
        return False, "within minimum dwell"

    conviction = abs(scan.get("conviction", 0.0))
    if age > MAX_DWELL_SECONDS and conviction < PROMOTE_CONVICTION:
        return True, f"max dwell {age / 3600:.1f}h exceeded"

    if entry.get("checks_below", 0) < DEMOTE_CONSECUTIVE_CHECKS:
        return False, "decay not sustained"

    triggers = scan.get("triggers", {})
    if triggers.get("bb_squeeze") or triggers.get("divergence") in ("bullish", "bearish"):
        return False, "setup still armed"
    if triggers.get("near_level"):
        return False, "still at a key level"
    if triggers.get("volume_ratio", 1.0) >= VOLUME_NORMAL:
        return False, "volume still elevated"

    return True, f"conviction below {DEMOTE_CONVICTION} for {entry['checks_below']} checks"


async def reconcile(
    scans: list[dict],
    previous: dict[str, dict],
    fired_symbols: set[str] | None = None,
) -> dict:
    """Apply promote/demote rules across a completed sweep. Returns what changed."""
    entries = await get_all()
    open_symbols = await _open_trade_symbols()
    # Autotrader positions are shielded from competition (displacement, cap trims) but
    # not from their own demotion or from leaving the top ten — those are real exits.
    auto_held = autotrader_open_symbols()
    fired_symbols = fired_symbols or set()
    added, removed, kept = [], [], []

    for scan in scans:
        symbol = scan["symbol"]
        entry = entries.get(symbol)
        has_open = symbol in open_symbols
        fired = symbol in fired_symbols
        reasons = promotion_reasons(scan, previous.get(symbol), has_open, fired)

        if entry:
            conviction = abs(scan.get("conviction", 0.0))
            entry["checks_below"] = (
                entry.get("checks_below", 0) + 1 if conviction < DEMOTE_CONVICTION else 0
            )
            entry["conviction"] = scan.get("conviction", 0.0)
            entry["last_seen"] = time.time()
            if reasons:
                entry["reasons"] = reasons

            should_go, why = _can_demote(scan, entry, has_open, fired)
            if should_go:
                await state.hdel(KEY, symbol)
                removed.append({"symbol": symbol, "reason": why})
            else:
                await state.hset(KEY, symbol, entry)
                kept.append(symbol)
            continue

        if not reasons:
            continue
        # Recently displaced or trimmed: wait out the cooldown unless there's a live reason.
        if not (has_open or fired) and await state.get(_rejoin_key(symbol)):
            continue

        entries_now = await get_all()
        unpinned = {s: e for s, e in entries_now.items() if not e.get("pinned")}
        if len(unpinned) >= MAX_SIZE:
            # Full — the newcomer must beat the weakest displaceable incumbent on conviction.
            now = time.time()
            displaceable = {
                s: e for s, e in unpinned.items()
                if s not in auto_held and now - e.get("added_at", 0) >= MIN_TENURE_SECONDS
            }
            if not displaceable:
                continue
            weakest = min(displaceable.items(), key=lambda kv: abs(kv[1].get("conviction", 0.0)))
            if abs(scan.get("conviction", 0.0)) < abs(weakest[1].get("conviction", 0.0)) + DISPLACE_MARGIN:
                continue
            await state.hdel(KEY, weakest[0])
            await _mark_left(weakest[0])
            removed.append({"symbol": weakest[0], "reason": f"displaced by {symbol} (clearly higher conviction)"})

        new_entry = {
            "symbol": symbol,
            "added_at": time.time(),
            "last_seen": time.time(),
            "conviction": scan.get("conviction", 0.0),
            "reasons": reasons,
            "checks_below": 0,
            "pinned": False,
        }
        await state.hset(KEY, symbol, new_entry)
        added.append({"symbol": symbol, "reasons": reasons})

    removed.extend(await _enforce_cap(protected=fired_symbols | open_symbols | auto_held))

    for a in added:
        signal_log.log_watchlist("add", a["symbol"], "; ".join(a.get("reasons") or []))
    for r in removed:
        signal_log.log_watchlist("remove", r["symbol"], r.get("reason"))

    if added or removed:
        logger.info(
            "Watchlist: +%s  -%s  (now %d)",
            [a["symbol"] for a in added] or "none",
            [f"{r['symbol']} ({r['reason']})" for r in removed] or "none",
            len(await get_all()),
        )

    return {"added": added, "removed": removed, "kept": kept}


async def _enforce_cap(protected: set[str] | None = None) -> list[dict]:
    """Trim to MAX_SIZE, weakest conviction first.

    The per-insert check can't hold the cap on its own: the two sweep tiers reconcile
    under separate locks, so a promotion in one can interleave with the other's reads.
    Trimming at the end is order-independent and also heals a list that overflowed
    before this ran.

    Pinned symbols, open positions and live signals are never trimmed — evicting those
    would hide a trade the user is currently in or could still take.
    """
    protected = protected or set()
    entries = await get_all()
    evictable = [
        (s, e) for s, e in entries.items()
        if not e.get("pinned") and s not in protected
    ]
    if len(entries) <= MAX_SIZE or not evictable:
        return []

    # Newest entries last: trim settled, weak incumbents before brand-new arrivals.
    now = time.time()
    evictable.sort(key=lambda kv: (now - kv[1].get("added_at", 0) < MIN_TENURE_SECONDS,
                                   abs(kv[1].get("conviction", 0.0))))
    evicted = []
    for symbol, _ in evictable[: min(len(entries) - MAX_SIZE, len(evictable))]:
        await state.hdel(KEY, symbol)
        await _mark_left(symbol)
        evicted.append({"symbol": symbol, "reason": "watchlist full — lowest conviction"})
    return evicted


async def prune_outside(scanned: set[str], protected: set[str] | None = None) -> list[dict]:
    """Drop watched symbols that are no longer in the scanned set (fell out of the top ten).

    Pinned symbols and open positions stay; so does anything in `protected`.
    """
    protected = (protected or set()) | await _open_trade_symbols()
    removed = []
    for symbol, entry in (await get_all()).items():
        if symbol in scanned or entry.get("pinned") or symbol in protected:
            continue
        await state.hdel(KEY, symbol)
        removed.append({"symbol": symbol, "reason": "dropped out of the top tradeable ten"})
    for r in removed:
        signal_log.log_watchlist("remove", r["symbol"], r["reason"])
    if removed:
        logger.info("Watchlist pruned (left the top ten): %s", [r["symbol"] for r in removed])
    return removed


async def pin(symbol: str) -> dict:
    """Manually pin — exempt from auto-demotion and from the size cap."""
    entries = await get_all()
    entry = entries.get(symbol) or {
        "symbol": symbol,
        "added_at": time.time(),
        "conviction": 0.0,
        "checks_below": 0,
        "reasons": ["manually pinned"],
    }
    entry["pinned"] = True
    entry["last_seen"] = time.time()
    await state.hset(KEY, symbol, entry)
    return entry


async def unpin(symbol: str) -> bool:
    """Remove entirely — an unpinned symbol can be re-added by the next sweep on merit."""
    entries = await get_all()
    if symbol not in entries:
        return False
    await state.hdel(KEY, symbol)
    return True
