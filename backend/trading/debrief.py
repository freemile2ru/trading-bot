"""Trade debriefs — why each closed trade won or lost, so patterns show up across many trades.

Two layers, run by the scheduler every 15 minutes:

1. Automatic tags (every closed trade in an active system, no Claude). Once the post-exit window
   has passed (24 h for swing trades, 1 h for trades held under an hour) the price path is fetched
   and the trade gets one primary tag and any secondary ones:
     clean win · win, exited early · fees ate it · gave back the gain · stopped, then reversed ·
     never worked · clean loss
   "Early" / "reversed" look at what price did AFTER the exit. R is measured against the risk at
   entry (not a stop moved later).
2. Claude debriefs (swing books only — Main, Autotrader, the Lab books, Claude swing): Claude
   compares each trade's thesis and invalidation with what actually happened and writes a verdict,
   what happened, and a one-line lesson. Batched (DEBRIEF_BATCH trades per call) under its own
   daily budget, separate from every other Claude cap. Claude swing reads its own recent lessons
   at every review; nothing else is fed back (the autotrader's Claude check is under a paired test).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from storage import signals as signal_log
from storage import state as kv

logger = logging.getLogger("debrief")

SWING_WINDOW_S = 24 * 3600
SHORT_WINDOW_S = 3600
SHORT_TRADE_MIN = 60                    # held under this many minutes → minute bars, 1-hour window
DAILY_CALLS = int(os.getenv("CLAUDE_DEBRIEF_DAILY_CALLS", "40"))
DEBRIEF_BATCH = 5
CLAUDE_SYSTEMS = {"main", "main_live", "auto", "auto_live", "auto_rules", "signals_claude", "signals_rules", "claude_swing"}
TAGS = ["clean win", "win, exited early", "fees ate it", "gave back the gain", "stopped, then reversed",
        "never worked", "clean loss"]


# ── Layer 1: automatic tags ─────────────────────────────────────────────────────

def tag(trade: dict, path: dict) -> dict:
    """trade: a unified trade row. path: {"best_r", "worst_r", "post_best_r", "post_back_through_entry"}.
    Returns {"primary", "tags", "metrics"}."""
    net, gross = trade.get("net_pnl") or 0.0, trade.get("gross_pnl") or 0.0
    best, post = path.get("best_r"), path.get("post_best_r")
    stopped = "stop" in (trade.get("exit_reason") or "") or (trade.get("exit_reason") or "") == "liquidated"
    tags = []
    if net > 0:
        tags.append("win, exited early" if post is not None and post >= 1 else "clean win")
    else:
        if gross > 0:
            tags.append("fees ate it")
        if best is not None and best >= 1:
            tags.append("gave back the gain")
        if stopped and path.get("post_back_through_entry"):
            tags.append("stopped, then reversed")
        if best is not None and best < 0.3:
            tags.append("never worked")
        if not tags:
            tags.append("clean loss")
        if post is not None and post >= 1 and "win, exited early" not in tags:
            tags.append("exited early")
    return {"primary": tags[0], "tags": tags, "metrics": {**path, "r": trade.get("r"), "net_pnl": net, "gross_pnl": gross,
                                                        "fees": trade.get("fees"), "exit_reason": trade.get("exit_reason")}}


async def _path(trade: dict) -> dict | None:
    """Excursions during the trade and after the exit, in R of the risk taken at entry."""
    from data.derivatives_client import fetch_klines_range
    entry, qty, risk = trade.get("entry_price"), trade.get("quantity"), trade.get("risk_usd")
    if not entry or not qty or not risk or not trade.get("opened_ts") or not trade.get("closed_ts"):
        return None
    r_unit = risk / qty                                  # price distance of the entry risk
    s = 1 if trade["direction"] == "long" else -1
    short = (trade.get("hold_min") or 0) < SHORT_TRADE_MIN
    window = SHORT_WINDOW_S if short else SWING_WINDOW_S
    interval, step = ("1m", 60) if short else ("1h", 3600)
    opened, closed = trade["opened_ts"], trade["closed_ts"]
    bars = await fetch_klines_range(trade["symbol"], interval, int((opened - step) * 1000), int((closed + window) * 1000))
    during = [b for b in bars if opened - step < b[0] / 1000 <= closed]
    after = [b for b in bars if closed < b[0] / 1000 + step and b[0] / 1000 <= closed + window]
    fav = lambda b: (b[2] if s > 0 else b[3])            # high for a long, low for a short
    adv = lambda b: (b[3] if s > 0 else b[2])
    exit_px = trade.get("exit_price") or entry
    out = {"r_unit": r_unit, "window_h": window / 3600,
           "best_r": round(max(((fav(b) - entry) * s / r_unit for b in during), default=0.0), 3) if during else None,
           "worst_r": round(min(((adv(b) - entry) * s / r_unit for b in during), default=0.0), 3) if during else None,
           "post_best_r": round(max(((fav(b) - exit_px) * s / r_unit for b in after), default=0.0), 3) if after else None,
           "post_worst_r": round(min(((adv(b) - exit_px) * s / r_unit for b in after), default=0.0), 3) if after else None}
    # Did price go back through the entry, in the trade's favour, after the exit?
    out["post_back_through_entry"] = bool(after) and any((fav(b) - entry) * s > 0 for b in after)
    if trade.get("mfe_r") is not None and out["best_r"] is None:
        out["best_r"] = trade["mfe_r"]
    return out


async def run_tags() -> int:
    """Tag every closed trade whose post-exit window has passed. Returns how many were tagged."""
    from trading import overview
    rows, _ = overview.all_rows()
    done = signal_log.debriefed_keys()
    now = time.time()
    # Pair books (Hold-72, Fade-24) are two legs with no stop, and carry is hedged — single-price path tags don't describe them.
    due = [r for r in rows if r["status"] == "closed" and r["system"] not in overview.RETIRED_SYSTEMS
           and r["system"] not in ("hold72", "carry", "fade24", "events", "carry_q", "unlocks", "vol_premium")
           and (r["system"], r["id"]) not in done and r.get("closed_ts")
           and now - r["closed_ts"] >= (SHORT_WINDOW_S if (r.get("hold_min") or 0) < SHORT_TRADE_MIN else SWING_WINDOW_S)]
    written = 0
    for r in sorted(due, key=lambda r: r["closed_ts"])[:60]:          # paced: at most 60 per run
        try:
            path = await _path(r)
        except Exception as e:
            logger.warning("Debrief path for %s %s failed: %s", r["system"], r["id"], e)
            continue
        d = tag(r, path or {})
        signal_log.save_debrief(r["system"], r["id"], r["symbol"], r["closed_ts"], d["primary"], d["tags"], d["metrics"])
        written += 1
    return written


# ── Layer 2: Claude debriefs ────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are debriefing closed crypto futures trades (Binance USDT-M perpetuals, paper and testnet).
For each trade you get what was planned (direction, entry, stop, target, thesis, invalidation, setups, market
regime) and what happened (the price path during the trade and after the exit, in R — multiples of the risk
taken at entry — plus the exit reason and net result after costs).

Be concrete and honest. A single trade is mostly noise, so don't over-learn from one outcome: say whether
the THESIS was right or wrong on its own terms, separately from whether the trade made money (a right call
can lose to costs or a tight stop; a wrong call can win by luck). Then give one lesson that would change a
future decision — or "none — a reasonable trade that didn't work" when that's the truth.

Respond with ONLY a JSON array, one object per trade, in the order given, no prose, no code fences:
[{"id": "<id>", "thesis": "right" | "wrong" | "partly right" | "no thesis",
  "what_happened": "<one or two sentences>",
  "cause": "entry timing" | "stop placement" | "exit management" | "thesis" | "costs" | "market noise" | "worked as planned",
  "lesson": "<one actionable sentence, or 'none — a reasonable trade that didn't work'>"}]"""


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _budget_key() -> str:
    return kv.key("debrief", "calls", _utc_day())


def calls_today() -> int:
    return int(kv.get_sync(_budget_key()) or 0)


def _record(system: str, trade_id: str) -> dict | None:
    from experiments import books
    from live import swing as live_swing
    if system in books.LABELS:
        w = books.WALLETS[system]
    else:
        base = system.removesuffix("_live")
        w = live_swing.LIVE[base] if system.endswith("_live") else live_swing.paper(base)
    return next((t for t in w.all_trades()["closed"] if t["id"] == trade_id), None)


def _px(v) -> str:
    return f"{v:.6g}" if isinstance(v, (int, float)) else "n/a"


def _brief(system: str, t: dict, d: dict) -> str:
    m = d.get("metrics") or {}
    reg = (t.get("regime_at_entry") or {}).get("label") or "not recorded"
    return (f"### id {t['id']} — {system}: {t['direction']} {t['symbol']}\n"
            f"Planned: entry {_px(t.get('entry_price'))}, stop {_px(t.get('original_stop_loss') or t.get('stop_loss'))}, "
            f"target {_px(t.get('take_profit_2'))}, planned hold {t.get('hold_duration') or t.get('hold_days')}. Regime: {reg}.\n"
            f"Thesis: {t.get('claude_thesis') or t.get('oracle_reasoning') or '(none — rules-only entry)'}\n"
            f"Invalidation: {t.get('invalidation') or '(not stated)'}. Setups: {', '.join(t.get('setups_at_entry') or []) or 'n/a'}.\n"
            f"Happened: best {m.get('best_r')}R, worst {m.get('worst_r')}R during the trade; exit by {t.get('close_reason')} "
            f"at {_px(t.get('current_price'))}, net {t.get('pnl_usd', 0):+.2f} USDT ({m.get('r')}R, fees {t.get('fees_usd') or 0:.2f}); "
            f"after the exit, within {m.get('window_h')}h: best {m.get('post_best_r')}R / worst {m.get('post_worst_r')}R from the exit, "
            f"back through entry: {m.get('post_back_through_entry')}. Automatic tags: {', '.join(d.get('tags') or [])}.")


async def run_claude() -> int:
    """Claude debriefs for tagged swing trades that don't have one, under the daily budget."""
    from oracle.claude_oracle import MODEL, get_client, parse_json, response_text
    pending = [d for d in signal_log.debriefs_pending_claude() if d["system"] in CLAUDE_SYSTEMS]
    written = 0
    while pending and calls_today() < DAILY_CALLS:
        batch, pending = pending[:DEBRIEF_BATCH], pending[DEBRIEF_BATCH:]
        items = [(d, _record(d["system"], d["trade_id"])) for d in batch]
        items = [(d, t) for d, t in items if t]
        if not items:
            continue
        await kv.incr_with_expire(_budget_key(), 172800)
        try:
            resp = await get_client().messages.create(
                model=MODEL, max_tokens=4000 + 600 * len(items),
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": "\n\n".join(_brief(d["system"], t, d) for d, t in items)}])
            answers = {a.get("id"): a for a in parse_json(response_text(resp))}
        except Exception as e:
            logger.warning("Claude debrief failed: %s", e)
            return written
        for d, t in items:
            a = answers.get(t["id"])
            if a:
                signal_log.save_claude_debrief(d["system"], d["trade_id"], {k: a.get(k) for k in ("thesis", "what_happened", "cause", "lesson")})
                written += 1
    return written


async def run() -> None:
    tagged = await run_tags()
    debriefed = await run_claude()
    if tagged or debriefed:
        logger.info("Debriefs: %d trades tagged, %d debriefed by Claude", tagged, debriefed)


# ── Read side ───────────────────────────────────────────────────────────────────

def lessons(system: str, limit: int = 6) -> list[dict]:
    """Claude's most recent lessons for one book — fed into Claude swing's reviews."""
    return [d for d in signal_log.debriefs(system=system, limit=50) if d.get("claude")][:limit]


def summary(system: str | None = None) -> dict:
    from trading import overview
    ds = signal_log.debriefs(system=system, limit=5000)
    ds = [d for d in ds if d["system"] not in overview.RETIRED_SYSTEMS]
    by_tag = []
    for tg in TAGS + ["exited early"]:
        hit = [d for d in ds if tg in d["tags"]]
        rs = [d["metrics"].get("r") for d in hit if d["metrics"].get("r") is not None]
        if hit:
            by_tag.append({"tag": tg, "trades": len(hit), "primary": sum(1 for d in hit if d["primary"] == tg),
                           "share_pct": round(len(hit) / len(ds) * 100, 1), "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
                           "net": round(sum(d["metrics"].get("net_pnl") or 0 for d in hit), 2)})
    causes: dict[str, int] = {}
    for d in ds:
        c = (d.get("claude") or {}).get("cause")
        if c:
            causes[c] = causes.get(c, 0) + 1
    return {"trades": len(ds), "by_tag": by_tag, "causes": sorted(({"cause": k, "trades": v} for k, v in causes.items()),
                                                                   key=lambda x: -x["trades"]),
            "recent": [d for d in ds if d.get("claude")][:20], "calls_today": calls_today(), "daily_calls": DAILY_CALLS,
            "claude_systems": sorted(CLAUDE_SYSTEMS)}
