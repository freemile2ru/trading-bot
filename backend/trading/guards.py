"""Guards for the crypto swing books — stop giving back winning streaks, and rethink after losses.

Built after the 2026-10-08 reversal: a short streak's gains went straight back when the market
turned, and the signal books had piled up 12–14 correlated shorts. Three layers:

1. Direction gate. A book opens longs (or shorts) only while the last GATE_N closed trades in that
   direction, across the crypto swing books, are net positive. On our own 249 trades it halved the
   loss per trade (−0.19R → −0.09R; the trades it would have skipped averaged −0.24R), and it was
   the only streak rule that helped both halves of the year-long replay (−0.08 → −0.055R and
   +0.02 → +0.054R). It reacts after a turn, never before — and it isn't an edge on its own.
2. Open-risk cap. A book's open positions together may lose at most MAX_OPEN_RISK_PCT of equity at
   their stops (the autotrader already had this; the signal books didn't).
3. Rethink. When a FAMILY (books paired in a test share one, so both sides get identical rules) has
   STREAK_LOSSES losses in a row, or its last 10 trades net ≤ −REVIEW_R_LOSS R: risk per trade is
   halved for ADJUST_HOURS at once, and Claude reviews the family's recent trades and picks bounded
   adjustments — pause a direction, cut risk further, require stronger conviction, a lower open-risk
   cap — which also expire after ADJUST_HOURS. At most one review per family a day. Every
   adjustment is logged with Claude's reasoning and can be cleared from the Books tab.
Skipped entries are recorded (and, in the signal books, tracked as shadow trades) so whether the
guards help is measured, not assumed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from storage import state as kv

logger = logging.getLogger("trading.guards")

ENABLED = os.getenv("GUARDS_ENABLED", "true").lower() != "false"
GATE_N = int(os.getenv("GUARD_DIRECTION_LAST_N", "10"))
MAX_OPEN_RISK_PCT = float(os.getenv("GUARD_MAX_OPEN_RISK_PCT", "4.0"))
STREAK_LOSSES = int(os.getenv("GUARD_STREAK_LOSSES", "5"))
REVIEW_R_LOSS = float(os.getenv("GUARD_REVIEW_R_LOSS", "4.0"))
ADJUST_HOURS = float(os.getenv("GUARD_ADJUST_HOURS", "48"))
CLAUDE_DAILY = int(os.getenv("GUARD_CLAUDE_DAILY_CALLS", "4"))
SHADOW_DEDUPE_S = 6 * 3600     # a skipped setup is one market move, however many books or sweeps wanted it
GATE_BOOKS = ("auto", "auto_live", "auto_rules", "signals_claude", "signals_rules", "main", "main_live", "claude_swing")
FAMILIES = {"autotrader": ("auto", "auto_live", "auto_rules"), "signals": ("signals_claude", "signals_rules")}
BOUNDS = {"risk_mult": (0.25, 1.0), "conviction_add": (0.0, 0.15), "max_open_risk_pct": (2.0, MAX_OPEN_RISK_PCT),
          "pause_hours": (0.0, 48.0)}
_cache: dict = {"at": 0.0, "trades": []}


def family_of(book: str) -> str | None:
    return next((f for f, bs in FAMILIES.items() if book in bs), None)


# ── Closed trades (pooled) ─────────────────────────────────────────────────────

def _wallets() -> dict:
    from experiments import books
    from live import swing as live_swing
    out = {f"{s}{'_live' if w.label.endswith('_live') else ''}": w for s in ("main", "auto") for w in live_swing.wallets(s)}
    out.update({n: books.WALLETS[n] for n in ("auto_rules", "signals_claude", "signals_rules", "claude_swing")})
    return out


def closed_trades(max_age_s: float = 60) -> list[dict]:
    """Closed trades of the crypto swing books, oldest close first: {book, direction, r, closed_ts}.
    Includes the shadows of entries the guards kept out ("gated": True) — without them a closed
    gate would never see its direction start working again, and would never reopen."""
    if time.time() - _cache["at"] < max_age_s:
        return _cache["trades"]
    from trading import exits
    rows = []
    for book, w in _wallets().items():
        for r in w.shadow_report()["results"]:
            if r.get("claude_verdict") == "gated" and r.get("policy") == exits.DEFAULT_POLICY:
                rows.append({"book": book, "direction": r["direction"], "r": r["r"], "closed_ts": r["closed_ts"],
                             "symbol": r["symbol"], "close_reason": r.get("reason"), "peak_r": None, "setups": [],
                             "regime": None, "gated": True})
        for t in w.all_trades()["closed"]:
            if not t.get("risk_usd") or not t.get("closed_at"):
                continue
            rows.append({"book": book, "direction": t["direction"], "r": t["pnl_usd"] / t["risk_usd"],
                         "closed_ts": datetime.fromisoformat(t["closed_at"]).timestamp(), "symbol": t["symbol"],
                         "close_reason": t.get("close_reason"), "peak_r": t.get("peak_r"),
                         "setups": t.get("setups_at_entry") or [], "regime": (t.get("regime_at_entry") or {}).get("label")})
    rows.sort(key=lambda r: r["closed_ts"])
    rows = _dedupe_shadows(rows)
    _cache.update(at=time.time(), trades=rows)
    return rows


def _dedupe_shadows(rows: list[dict]) -> list[dict]:
    """Count each skipped setup once. When the gate blocks a move, every book that wanted it — and every
    sweep while it lasts — records a shadow, so one move on one coin showed up as six -1R "trades" and
    held the gate shut. A shadow is dropped if the same symbol and direction already has a kept trade
    (shadow or real) closing within SHADOW_DEDUPE_S before it. Real trades are always kept."""
    last: dict[tuple[str, str], float] = {}
    out = []
    for r in rows:
        k = (r.get("symbol"), r["direction"])
        if r.get("gated") and r["closed_ts"] - last.get(k, float("-inf")) < SHADOW_DEDUPE_S:
            continue
        last[k] = r["closed_ts"]
        out.append(r)
    return out


def direction_state() -> dict:
    """Each direction's last GATE_N closed trades (all crypto swing books, plus the shadows of skipped
    entries): net R and whether it's open."""
    trades = [t for t in closed_trades() if t["book"] in GATE_BOOKS]
    out = {}
    for d in ("long", "short"):
        window = [t for t in trades if t["direction"] == d][-GATE_N:]
        last = [t["r"] for t in window]
        net = sum(last)
        out[d] = {"n": len(last), "net_r": round(net, 2), "open": len(last) < GATE_N or net > 0,
                  # What the next close must make to reopen it: the oldest trade drops out of the window.
                  "next_needs_r": round(last[0] - net, 2) if len(last) == GATE_N and net <= 0 else None,
                  "shadows": sum(1 for t in window if t.get("gated")),
                  "trades": [{"book": t["book"], "symbol": t.get("symbol", ""), "r": round(t["r"], 2), "closed_ts": t["closed_ts"],
                              "gated": bool(t.get("gated"))} for t in reversed(window)]}
    return out


# ── Adjustments (per family) ───────────────────────────────────────────────────

def _key(family: str) -> str:
    return kv.key("guards", family)


def adjustments(family: str | None) -> dict:
    """Active adjustments for a family (expired ones are dropped)."""
    if not family:
        return {}
    a = kv.get_sync(_key(family)) or {}
    if a.get("until", 0) < time.time():
        return {}
    paused = {d: until for d, until in (a.get("paused") or {}).items() if until > time.time()}
    return {**a, "paused": paused}


def set_adjustments(family: str, adj: dict, reason: str, source: str) -> dict:
    until = time.time() + ADJUST_HOURS * 3600
    clean = {"risk_mult": min(max(float(adj.get("risk_mult", 1.0)), BOUNDS["risk_mult"][0]), BOUNDS["risk_mult"][1]),
             "conviction_add": min(max(float(adj.get("conviction_add", 0.0)), BOUNDS["conviction_add"][0]), BOUNDS["conviction_add"][1]),
             "max_open_risk_pct": min(max(float(adj.get("max_open_risk_pct", MAX_OPEN_RISK_PCT)), BOUNDS["max_open_risk_pct"][0]),
                                      BOUNDS["max_open_risk_pct"][1]),
             "paused": {d: time.time() + min(max(float(h), 0.0), BOUNDS["pause_hours"][1]) * 3600
                        for d, h in (adj.get("pause_hours") or {}).items() if d in ("long", "short") and float(h) > 0},
             "until": until, "reason": reason[:2000], "source": source, "set_at": time.time()}
    kv.set_sync(_key(family), clean, ADJUST_HOURS * 3600 + 3600)
    log = kv.get_sync(kv.key("guards", "log")) or []
    kv.set_sync(kv.key("guards", "log"), ([{"family": family, **clean}] + log)[:50])
    logger.warning("Guards %s: %s (%s)", family, json.dumps({k: v for k, v in clean.items() if k not in ("reason",)}), reason[:120])
    return clean


def clear(family: str) -> None:
    kv.delete_sync(_key(family))


# ── The entry check ────────────────────────────────────────────────────────────

def entry_block(book: str, direction: str, conviction: float | None = None,
                open_risk_pct: float | None = None, new_risk_pct: float = 0.0) -> str | None:
    """Why this entry must not be taken now, or None."""
    if not ENABLED:
        return None
    adj = adjustments(family_of(book))
    if direction in (adj.get("paused") or {}):
        return f"{direction}s paused by the strategy review until {datetime.fromtimestamp(adj['paused'][direction], timezone.utc):%d %b %H:%M} UTC"
    st = direction_state()[direction]
    if not st["open"]:
        return f"direction gate: the last {GATE_N} {direction}s are net {st['net_r']:+.1f}R — waiting for {direction}s to work again"
    need = adj.get("conviction_add") or 0.0
    if need and conviction is not None:
        from autotrader import strategy
        if abs(conviction) < strategy.MIN_CONVICTION + need:
            return f"strategy review asks for conviction ≥ {strategy.MIN_CONVICTION + need:.2f} (this is {abs(conviction):.2f})"
    cap = adj.get("max_open_risk_pct") or MAX_OPEN_RISK_PCT
    if open_risk_pct is not None and open_risk_pct + new_risk_pct > cap:
        return f"open risk {open_risk_pct:.1f}% + {new_risk_pct:.1f}% would pass the {cap:.1f}% cap"
    return None


def risk_mult(book: str) -> float:
    return float(adjustments(family_of(book)).get("risk_mult") or 1.0) if ENABLED else 1.0


def record_skip(book: str, symbol: str, direction: str, why: str) -> bool:
    """Remember what the guards kept out (once per coin/direction an hour, so a minute-by-minute
    re-check doesn't flood the log). True when this is a new skip — the caller tracks its shadow."""
    k = kv.key("guards", "skip", book, symbol, direction)
    if kv.get_sync(k):
        return False
    kv.set_sync(k, 1, 3600)
    log = kv.get_sync(kv.key("guards", "skips")) or []
    kv.set_sync(kv.key("guards", "skips"), ([{"ts": time.time(), "book": book, "symbol": symbol, "direction": direction,
                                              "why": why}] + log)[:200])
    return True


# ── Rethink after consistent losses ────────────────────────────────────────────

def _review_due(family: str) -> str | None:
    trades = [t for t in closed_trades(0) if t["book"] in FAMILIES[family] and not t.get("gated")]
    last = trades[-10:]
    if not last:
        return None
    streak = 0
    for t in reversed(trades):
        if t["r"] < 0:
            streak += 1
        else:
            break
    since = kv.get_sync(kv.key("guards", family, "reviewed_at")) or 0
    if time.time() - since < 86400:
        return None
    if streak >= STREAK_LOSSES:
        return f"{streak} losses in a row"
    if len(last) >= 10 and sum(t["r"] for t in last) <= -REVIEW_R_LOSS:
        return f"last 10 trades net {sum(t['r'] for t in last):+.1f}R"
    return None


REVIEW_PROMPT = """You are the risk manager of a small crypto futures desk. One of its strategy families has been losing
consistently. You get its recent closed trades and the market regime. Diagnose what is going wrong and choose temporary
adjustments (they expire after {hours:.0f} hours) from this menu ONLY, within the bounds:
  risk_mult          0.25–1.0   multiply risk per trade (it is already at 0.5 while you review)
  conviction_add     0.0–0.15   require this much more conviction than the normal 0.20 before entering
  max_open_risk_pct  2.0–4.0    cap on the open positions' combined loss at their stops, % of equity
  pause_hours        {{"long": h, "short": h}}, each 0–48  — stop opening that direction for h hours
Prefer the smallest change that addresses the diagnosis. If the losses look like ordinary variance, say so and keep
risk_mult at 0.5 with nothing else.
Reply with ONLY JSON: {{"diagnosis": "<2–3 sentences, specific to the trades>", "adjustments": {{...}},
"what_to_watch": "<one sentence: what would show the family has recovered>"}}"""


async def review(family: str, trigger: str) -> dict | None:
    """Halve risk at once; ask Claude for a diagnosis and bounded adjustments (its own budget)."""
    kv.set_sync(kv.key("guards", family, "reviewed_at"), time.time(), 3 * 86400)
    set_adjustments(family, {"risk_mult": 0.5}, f"automatic cool-off: {trigger}", "automatic")
    day = kv.key("guards", "claude", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    if int(kv.get_sync(day) or 0) >= CLAUDE_DAILY:
        return None
    await kv.incr_with_expire(day, 172800)
    from oracle.claude_oracle import MODEL, get_client, parse_json, response_text
    from trading import regime
    trades = [t for t in closed_trades(0) if t["book"] in FAMILIES[family] and not t.get("gated")][-25:]
    body = {"family": family, "books": list(FAMILIES[family]), "trigger": trigger, "regime_now": await regime.current(),
            "direction_gate": direction_state(),
            "recent_trades": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items() if k != "closed_ts"} |
                              {"closed": datetime.fromtimestamp(t["closed_ts"], timezone.utc).strftime("%m-%d %H:%M")} for t in trades]}
    try:
        from trading import debrief
        body["recent_lessons"] = [d["claude"]["lesson"] for b in FAMILIES[family] for d in debrief.lessons(b, 3)]
    except Exception:
        pass
    try:
        resp = await get_client().messages.create(model=MODEL, max_tokens=6000, system=REVIEW_PROMPT.format(hours=ADJUST_HOURS),
                                                  messages=[{"role": "user", "content": json.dumps(body, default=str)}])
        ans = parse_json(response_text(resp))
    except Exception as e:
        logger.warning("Strategy review for %s failed: %s", family, e)
        return None
    adj = {"risk_mult": 0.5, **(ans.get("adjustments") or {})}
    reason = f"{trigger}. Claude: {ans.get('diagnosis', '')} Watch: {ans.get('what_to_watch', '')}"
    try:
        return set_adjustments(family, adj, reason, "claude review")
    except (TypeError, ValueError) as e:                 # a malformed menu choice keeps the automatic cool-off only
        logger.warning("Strategy review for %s returned unusable adjustments (%s): %s", family, e, adj)
        return None


def family_active(family: str) -> bool:
    """A family with every book switched off or retired has nothing to review (the signals family kept
    triggering reviews — and Claude calls — for weeks after both its books were retired)."""
    from trading import book_switches
    return any(book_switches.enabled(b.removesuffix("_live")) for b in FAMILIES[family])


async def check() -> None:
    """Scheduler hook: start a review for any family whose losses call for one."""
    if not ENABLED:
        return
    for family in FAMILIES:
        if not family_active(family):
            continue
        trigger = _review_due(family)
        if trigger:
            logger.warning("Guards: %s family — %s; reviewing", family, trigger)
            await review(family, trigger)


def status() -> dict:
    return {"enabled": ENABLED, "gate_n": GATE_N, "max_open_risk_pct": MAX_OPEN_RISK_PCT,
            "directions": direction_state(),
            "families": {f: {"books": list(bs), "adjustments": adjustments(f), "active": family_active(f),
                             "review_due": _review_due(f) if family_active(f) else None}
                         for f, bs in FAMILIES.items()},
            "log": (kv.get_sync(kv.key("guards", "log")) or [])[:20],
            "skips": (kv.get_sync(kv.key("guards", "skips")) or [])[:50],
            "triggers": {"losses_in_a_row": STREAK_LOSSES, "last10_net_r": -REVIEW_R_LOSS, "adjust_hours": ADJUST_HOURS},
            "thesis_exit": _thesis_report()}


def _thesis_report() -> dict | None:
    from trading import thesis_exit
    try:
        return thesis_exit.report()
    except Exception as e:
        logger.debug("Thesis-exit report failed: %s", e)
        return None
