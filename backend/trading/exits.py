"""Exit policies — how an open position is managed bar by bar, and the shadow book that
compares them.

A policy is stamped on each trade when it opens (so changing the default never alters a
running trade). `apply_bar` is the single place exits happen, for real trades and for
shadow trades alike, so the comparison between policies is like for like.

Policies:
  protect        — the live default since 2026-10-07. Breakeven-plus-costs once the trade has
                   been +1R, then trail 0.5R behind the best price; closed after 24 h at most.
                   Chosen after winners kept turning into losers: on the 199 closed swing trades
                   re-run on their real paths (research/exit_lab.py) it made −0.11R/trade against
                   the old default's −0.31R, and trades that were ≥ +1R then closed at a loss went
                   from 14% to under 1%. Over the year-long replay it cost ~0.04R/trade (it cuts
                   big trends short) — a trade-off for choppy markets, watched in the shadow book.
  trail          — the default until 2026-10-07. At TP1 (1.5R) move the stop to breakeven-plus-
                   costs, then trail 1R behind the best price with no fixed target, up to the
                   max-hold deadline. Best on the 78-signal replay (+1.21R/trade).
  trail_stale    — trail, plus cut trades that haven't reached +0.5R by the stale
                   deadline. The stale cut cost ~1R/trade on the replay (trades that
                   stalled often went on to win) — kept in shadow to keep measuring it.
  partial_trail  — bank half at TP1, trail the rest (replay: +0.92R).
  fixed          — breakeven-plus-costs at TP1, full exit at TP2 (2.5R).
  legacy         — the previous behaviour: stop to exact entry at TP1, exit at TP2, hard
                   expiry by timeframe (4h/8h/24h/72h). Also used for trades recorded
                   before policies existed.
  hold           — baseline: original stop or TP2 only, until max hold.
  manual_lock    — manual (the book decides the exit, no clock) plus a DOLLAR profit lock (2026-11-02, the user's
                   rule "secure profits over $100"): once the open P&L has reached LOCK_ARM_USD, the stop sits where
                   LOCK_KEEP of the peak open P&L is kept, ratcheting with the peak. Live on the copy books; a shadow
                   on every other manual-policy book (the weekly basket's lab and the reversal legs' paths both said
                   trailing exits earn less than holding, so those keep their rule until their own shadows disagree).
  protect_quick, protect_cut6, protect_trim — shadow-only candidates (see each one's note).

The replay covers one week of mostly-long signals in a rising market; the shadow book
exists because that is nowhere near enough evidence to settle which policy is best.

Evaluation is pessimistic throughout: within a bar the stop is checked before anything
favourable; stop moves (breakeven, trailing) take effect from the next bar; on the bar a
limit filled on, only the stop can trigger.
"""

import os
import time
from dataclasses import dataclass

from trading import futures

HOUR = 3600


@dataclass(frozen=True)
class Policy:
    name: str
    breakeven: str | None = "costs"       # None | "entry" | "costs"
    partial_at_tp1: float = 0.0           # fraction banked at TP1
    trail_r: float | None = None          # trail distance after TP1, in R; None = no trailing
    fixed_tp2: bool = True                # exit everything at TP2
    stale: bool = True                    # close if no progress by the stale deadline
    stale_min_r: float = 0.5
    legacy_expiry: bool = False           # the old per-timeframe hard expiry
    be_at_r: float | None = None          # breakeven once +X R (None: at TP1)
    trail_at_r: float | None = None       # start trailing once +X R (None: after TP1)
    max_hold_h: float | None = None       # overrides the timeframe's max hold
    stale_h: float | None = None          # overrides the timeframe's stale deadline (with stale=True)
    trim_at_r: float | None = None        # close trim_frac of the position once price is this far against it (e.g. −0.3)
    trim_frac: float = 0.5
    window_exit_h: float | None = None    # in the last X hours before the max hold, look for the exit (see 6b)
    window_trail_r: float = 0.15          # …leaving once price slips this far (in R) from the best print of the window
    run_if_r: float | None = None         # at the max hold, a trade at least this far in profit gets run_extend_h more
    run_extend_h: float = 24.0            # …each time, up to run_cap_h in total; it then leaves on its stop or trail
    run_cap_h: float = 168.0
    lock_arm_usd: float | None = None     # dollar profit lock: arm once the open P&L has been this high…
    lock_keep: float = 0.5                # …and keep this share of the peak open P&L (the stop ratchets with the peak)


POLICIES: dict[str, Policy] = {
    # window_exit_h (2026-10-27): from 6h before the 24h bell the trade rides the price while it improves and leaves once
    # it slips 0.15R from the best close seen in the window (or at the bell). On 222 replayed decisions: +0.032R a trade
    # vs waiting for the bell, both halves positive (+0.039/+0.025), 90% range +0.013…+0.051 — better than leaving at the
    # first breakeven (+0.025) and steadier than 8h windows.
    "protect": Policy("protect", be_at_r=1.0, trail_at_r=1.0, trail_r=0.5, fixed_tp2=False, stale=False, max_hold_h=24,
                      window_exit_h=6.0, window_trail_r=0.15),
    "trail": Policy("trail", trail_r=1.0, fixed_tp2=False, stale=False),
    "trail_stale": Policy("trail_stale", trail_r=1.0, fixed_tp2=False),
    "partial_trail": Policy("partial_trail", partial_at_tp1=0.5, trail_r=1.0, fixed_tp2=False, stale=False),
    "fixed": Policy("fixed", stale=False),
    "legacy": Policy("legacy", breakeven="entry", stale=False, legacy_expiry=True),
    "hold": Policy("hold", breakeven=None, stale=False),
    # Trades placed by hand on the Trade tab: the stop and (optional) target you set, and
    # nothing else — no breakeven moves, trailing, stale cut or expiry.
    "manual": Policy("manual", breakeven=None, stale=False),
    "manual_lock": Policy("manual_lock", breakeven=None, stale=False,
                          lock_arm_usd=float(os.getenv("LOCK_ARM_USD", "100")), lock_keep=float(os.getenv("LOCK_KEEP", "0.5"))),
    # Shadow-only candidates from research/exit_rethink.py (2026-10-19): on 209 replayed decisions they
    # beat protect by +0.05 / +0.06R, too little and too uneven across halves to switch on — so they
    # run forward on every new entry. Rule, set now: after 60 entries opened from 2026-10-20, adopt one as
    # the live policy only if it beats protect by >= +0.05R a trade with its 90% bootstrap range above 0.
    "protect_quick": Policy("protect_quick", be_at_r=0.5, trail_at_r=1.0, trail_r=0.5, fixed_tp2=False,
                            stale=True, stale_h=6, stale_min_r=0.25, max_hold_h=24),
    "protect_cut6": Policy("protect_cut6", be_at_r=1.0, trail_at_r=1.0, trail_r=0.5, fixed_tp2=False,
                           stale=True, stale_h=6, stale_min_r=0.5, max_hold_h=24),
    # Shadow candidate from research/scale_in_out_lab.py (2026-10-26): trim half a losing trade at −0.3R.
    # +0.022R a trade on 222 replayed decisions, both halves positive, but 90% range −0.02…+0.06 and it may
    # overlap the thesis-faded exit. Rule, set now: after 60 entries opened from 2026-10-27, adopt only if it
    # beats protect by >= +0.02R a trade with its 90% bootstrap range above 0.
    "protect_trim": Policy("protect_trim", be_at_r=1.0, trail_at_r=1.0, trail_r=0.5, fixed_tp2=False, stale=False,
                           max_hold_h=24, trim_at_r=-0.3, trim_frac=0.5),
    # Shadow candidate (2026-10-28): 17 of the last 30 days' 32 wins were "expired" at the 24h bell averaging +0.07R,
    # while losses ran to the full stop. As protect, but a trade at least +0.5R at the bell gets another day under
    # the trail (up to a week) instead of leaving flat. Rule, set now: after 60 entries opened from 2026-10-28,
    # adopt only if it beats protect by >= +0.05R a trade with its 90% bootstrap range above 0.
    "protect_run": Policy("protect_run", be_at_r=1.0, trail_at_r=1.0, trail_r=0.5, fixed_tp2=False, stale=False,
                          max_hold_h=24, run_if_r=0.5, run_extend_h=24, run_cap_h=168),
}
DEFAULT_POLICY = os.getenv("EXIT_POLICY", "protect")
SHADOW_POLICIES = [p for p in os.getenv("EXIT_SHADOW_POLICIES", ",".join(k for k in POLICIES if k not in ("manual", "manual_lock"))).split(",")
                   if p in POLICIES]
# The profit lock on the books that hold to their own rule: "live" stamps manual_lock on every manual trade (the
# user's "all our trades" switch), "shadow" (default) runs it beside each such trade and scores it in the shadow book.
LOCK_MODE = os.getenv("EXIT_LOCK_MODE", "shadow")
LOCK_BOOKS = set(os.getenv("EXIT_LOCK_BOOKS", "copy").split(","))     # labels (or prefixes) where the lock is live regardless


def lock_policy_for(label: str | None, policy_name: str | None) -> str | None:
    """The policy a book's "manual" trade actually opens under: manual_lock where the lock is live, else as asked."""
    if policy_name != "manual":
        return policy_name
    if LOCK_MODE == "live" or (label and any(label == b or label.startswith(b) for b in LOCK_BOOKS)):
        return "manual_lock"
    return policy_name

# (stale deadline, max hold) by the trade's timeframe. Winners are allowed to run far past
# the stale deadline; only trades going nowhere are cut there.
WINDOWS = {
    "15m": (3 * HOUR, 24 * HOUR),
    "30m": (6 * HOUR, 48 * HOUR),
    "1h": (12 * HOUR, 72 * HOUR),
    "4h": (36 * HOUR, 7 * 24 * HOUR),
    "1d": (7 * 24 * HOUR, 60 * 24 * HOUR),
    "manual": (365 * 24 * HOUR, 365 * 24 * HOUR),     # held until you close it
}
LEGACY_EXPIRY = {"15m": 4 * HOUR, "30m": 8 * HOUR, "1h": 24 * HOUR, "4h": 72 * HOUR}


def policy_of(trade: dict) -> Policy:
    name = trade.get("exit_policy")
    if name in POLICIES:
        return POLICIES[name]
    # Recorded before policies existed: reproduce what it was opened under.
    return Policy("pre_policy", breakeven="entry" if trade.get("breakeven_on_tp1") else None,
                  stale=False)


def cost_offset(entry: float) -> float:
    """Price distance that covers round-trip fees and slippage — a true breakeven."""
    return entry * (2 * futures.TAKER_FEE + 2 * futures.SLIPPAGE)


def stamp(trade: dict, policy_name: str | None = None, now: float | None = None) -> dict:
    """Attach the exit policy and everything bar-by-bar evaluation needs."""
    now = time.time() if now is None else now
    p = POLICIES.get(policy_name or DEFAULT_POLICY, POLICIES["protect"])
    # A "manual" trade is held until its owner closes it (you, Claude swing, a trend flip) — never by a
    # timeframe's clock. (Keyed on the timeframe, trend trades marked "1d" fell back to the 1h window
    # and were force-closed after 72 h — fixed 2026-10-12.)
    stale_s, max_hold_s = WINDOWS["manual"] if p.name in ("manual", "manual_lock") else \
        WINDOWS.get(trade.get("timeframe") or "1h", WINDOWS["1h"])
    trade["exit_policy"] = p.name
    trade["opened_ts"] = now
    trade["r_unit"] = abs(trade["entry_price"] - trade["stop_loss"])
    trade["peak_r"] = 0.0
    trade["peak_price"] = trade["entry_price"]
    trade["tp1_done"] = False
    trade["stale_after_s"] = (p.stale_h * HOUR if p.stale_h else stale_s) if p.stale else None
    trade["stale_min_r"] = p.stale_min_r
    if p.legacy_expiry:
        trade["expires_at"] = now + LEGACY_EXPIRY.get(trade.get("timeframe") or "1h", 24 * HOUR)
    elif p.max_hold_h is not None:
        trade["expires_at"] = now + p.max_hold_h * HOUR
    else:
        trade["expires_at"] = now + max_hold_s
    return trade


def _sign(t: dict) -> int:
    return 1 if t["direction"] == "long" else -1


def stop_reason(t: dict, stop: float) -> str:
    """Name a stop exit by where the stop actually sat — not merely whether it had moved.

    A stop that was tightened but is still on the losing side of entry is a loss
    ("tightened_stop"), not a breakeven; labelling it "breakeven" hid real losses.
    """
    s = 1 if t["direction"] == "long" else -1
    locked_in = (stop - t["entry_price"]) * s >= 0          # at or beyond entry: no loss on price
    if locked_in and t.get("trailing"):
        return "trailing_stop"
    if locked_in:
        return "breakeven"
    if t.get("stop_moved"):
        return "tightened_stop"
    return "stop_loss"


def _to_breakeven(t: dict, p: Policy, stop: float, s: int) -> None:
    be = t["entry_price"] + (s * cost_offset(t["entry_price"]) if p.breakeven == "costs" else 0.0)
    if (be - stop) * s > 0:
        t.setdefault("original_stop_loss", stop)
        t["stop_loss"] = round(be, 8)
        t["stop_moved"] = True


def apply_bar(
    t: dict, o: float, h: float, l: float, c: float, bar_end: float, stop_only: bool = False,
) -> tuple[str, float] | None:
    """Advance one position by one price bar. Returns (close_reason, trigger_price) or None.

    Mutates `t`: stop moves (breakeven, trailing), TP1 partials (realized P&L, maker fee,
    quantity), peak tracking. A live tick is a bar with o=h=l=c.
    """
    s, long = _sign(t), t["direction"] == "long"
    p = policy_of(t)
    stop, tp1, tp2 = t["stop_loss"], t["take_profit_1"], t["take_profit_2"]
    liq = t.get("liquidation_price")

    # 1. Gap past liquidation (otherwise the stop, well inside it, is reached first).
    if liq and ((o <= liq) if long else (o >= liq)):
        return "liquidated", liq

    # 2. Stop.
    if (l <= stop) if long else (h >= stop):
        trigger = min(o, stop) if long else max(o, stop)
        return stop_reason(t, stop), trigger
    if stop_only:
        return None

    # 3. Fixed target.
    if p.fixed_tp2 and ((h >= tp2) if long else (l <= tp2)):
        return "take_profit_2", tp2

    # 3b. Trim a loser: close trim_frac at trim_at_r against the trade (taker), once.
    if p.trim_at_r is not None and not t.get("trimmed") and t.get("quantity", 0) > 0:
        r_unit = t.get("r_unit") or abs(t.get("initial_entry_price", t["entry_price"]) - t.get("original_stop_loss", stop))
        level = t.get("initial_entry_price", t["entry_price"]) + (1 if long else -1) * p.trim_at_r * r_unit
        if r_unit and ((l <= level) if long else (h >= level)):
            px = min(o, level) if long else max(o, level)
            part = t["quantity"] * p.trim_frac
            t["realized_pnl_usd"] = round(t.get("realized_pnl_usd", 0.0) + (px - t["entry_price"]) * s * part, 4)
            t["fees_usd"] = round(t.get("fees_usd", 0.0) + futures.fee(px * part, maker=False), 4)
            t["quantity"] = round(t["quantity"] - part, 8)
            t["trimmed"] = True

    # 4. TP1: bank a partial and/or move the stop — effective from the next bar.
    best = h if long else l
    if not t.get("tp1_done") and ((best >= tp1) if long else (best <= tp1)):
        t["tp1_done"] = True
        t["close_reason"] = "tp1_hit"
        if p.partial_at_tp1 > 0 and t["quantity"] > 0:
            part = t["quantity"] * p.partial_at_tp1
            t["realized_pnl_usd"] = round(t.get("realized_pnl_usd", 0.0) + (tp1 - t["entry_price"]) * s * part, 4)
            t["fees_usd"] = round(t.get("fees_usd", 0.0) + futures.fee(tp1 * part, maker=True), 4)
            t["quantity"] = round(t["quantity"] - part, 8)
            t.setdefault("adjustments", []).append({
                "ts": bar_end, "action": "reduce", "kind": "tp1_partial", "price": tp1,
                "quantity": round(part, 8), "reason": f"banked {p.partial_at_tp1:.0%} at TP1",
                "new_stop": None, "quantity_after": t["quantity"], "avg_entry_after": t["entry_price"],
            })
        if p.breakeven and p.be_at_r is None:
            _to_breakeven(t, p, stop, s)

    # 5. Track the best price reached; trail behind it once TP1 has traded.
    r_unit = t.get("r_unit") or abs(t.get("initial_entry_price", t["entry_price"]) - t.get("original_stop_loss", stop))
    ref = t.get("initial_entry_price", t["entry_price"])
    if (best - t.get("peak_price", ref)) * s > 0:
        t["peak_price"] = best
    if r_unit:
        t["peak_r"] = max(t.get("peak_r", 0.0), (best - ref) * s / r_unit)
    # R-based breakeven (e.g. "protect": once +1R) — like the TP1 move, effective from the next bar.
    if p.breakeven and p.be_at_r is not None and t.get("peak_r", 0.0) >= p.be_at_r:
        _to_breakeven(t, p, t["stop_loss"], s)
    trailing = t.get("peak_r", 0.0) >= p.trail_at_r if p.trail_at_r is not None else t.get("tp1_done")
    if p.trail_r and trailing and r_unit:
        trail = t["peak_price"] - s * p.trail_r * r_unit
        if (trail - t["stop_loss"]) * s > 0:
            t.setdefault("original_stop_loss", stop)
            t["stop_loss"] = round(trail, 8)
            t["trailing"] = True
            t["stop_moved"] = True
    # 5b. Dollar profit lock: once the best open P&L has reached lock_arm_usd, keep lock_keep of it — effective from the next bar.
    if p.lock_arm_usd is not None and t.get("quantity", 0) > 0:
        peak_pnl = (t["peak_price"] - t["entry_price"]) * s * t["quantity"] - t.get("fees_usd", 0.0) - t.get("funding_usd", 0.0)
        if peak_pnl >= p.lock_arm_usd:
            keep = t["entry_price"] + s * (p.lock_keep * peak_pnl + t.get("fees_usd", 0.0) + t.get("funding_usd", 0.0)) / t["quantity"]
            # Never a stop the price has already crossed: on a trade whose peak predates the policy (HYPE, 2026-11-02,
            # −$385) that "locked" a level above the market and the next tick stopped it out at the current price.
            if (keep - t["stop_loss"]) * s > 0 and (c - keep) * s > 0:
                t.setdefault("original_stop_loss", stop)
                t["stop_loss"] = round(keep, 8)
                t["trailing"] = True
                t["stop_moved"] = True
                t["locked_usd"] = round(p.lock_keep * peak_pnl, 2)

    # 6. Time: cut trades going nowhere; cap how long anything is held.
    if t.get("stale_after_s") and bar_end - t.get("opened_ts", bar_end) >= t["stale_after_s"] \
            and t.get("peak_r", 0.0) < t.get("stale_min_r", 0.5):
        return "stale", c
    if t.get("expires_at") and bar_end >= t["expires_at"]:
        r_now = (c - ref) * s / r_unit if r_unit else 0.0
        if p.run_if_r is not None and r_now >= p.run_if_r \
                and bar_end - t.get("opened_ts", bar_end) < p.run_cap_h * HOUR:
            # In profit at the bell: another stretch under the trail rather than leaving flat.
            t["expires_at"] = bar_end + p.run_extend_h * HOUR
            t["window_best_r"] = None
            t["extended"] = t.get("extended", 0) + 1
        else:
            return "expired", c
    # 6b. Nearing the max hold: ride the price while it improves, leave once it slips window_trail_r from the
    # best close seen inside the window (the bell closes whatever is left).
    if p.window_exit_h and t.get("expires_at") and r_unit and bar_end >= t["expires_at"] - p.window_exit_h * HOUR:
        r_now = (c - ref) * s / r_unit
        best = t.get("window_best_r")
        if best is None or r_now > best:
            t["window_best_r"] = r_now
        elif best - r_now >= p.window_trail_r:
            return "window_exit", c
    return None


# ── Shadow book ─────────────────────────────────────────────────────────────────

_SHADOW_KEYS = (
    "symbol", "direction", "entry_price", "stop_loss", "take_profit_1", "take_profit_2",
    "quantity", "timeframe", "leverage", "liquidation_price", "margin_usd", "risk_usd",
    "fees_usd", "checked_from_ms", "fill_bar_ms", "funding_through_ms",
    "claude_verdict", "claude_confidence", "fa_at_entry", "maker_order_id",
)
# Shadows of trades that were never taken — kept out of the exit-policy comparison.
NOT_TAKEN = {"declined", "market_twin", "gated", "fade_twin"}


def make_shadows(trade: dict, wallet_label: str, policies: list[str] | None = None) -> list[dict]:
    """One virtual copy of a freshly opened trade per policy, sharing its fill and costs. A trade marked
    shadows=False gets none — the trend books hold for weeks on a daily rule, where intraday exit policies
    mean nothing (they had 100+ shadows fetching minute bars for nothing)."""
    lock_shadow = trade.get("exit_policy") == "manual" and LOCK_MODE == "shadow" and policies is None
    if trade.get("shadows") is False and policies is None:
        # A manual-policy book still shadows the profit lock, so the user's rule is scored on its own trades.
        if lock_shadow:
            policies = ["manual_lock"]
        else:
            return []
    elif lock_shadow:
        policies = [*SHADOW_POLICIES, "manual_lock"]
    shadows = []
    for name in policies or SHADOW_POLICIES:
        sh = {k: trade.get(k) for k in _SHADOW_KEYS}
        sh.update(id=f"{trade['id']}:{name}", trade_id=trade["id"], wallet=wallet_label,
                  realized_pnl_usd=0.0, funding_usd=0.0, opened_at=trade["opened_at"])
        if sh["checked_from_ms"] is None:
            # Market entry: the first full minute after opening, as for the real trade.
            sh["checked_from_ms"] = (int(trade["opened_ts"] * 1000) // 60_000 + 1) * 60_000
        stamp(sh, name, trade.get("opened_ts"))
        shadows.append(sh)
    return shadows


def summarize(results: list[dict], live_policy: str = DEFAULT_POLICY) -> list[dict]:
    """Per-policy scoreboard from closed shadows."""
    rows = []
    for name in POLICIES:
        rs = [r for r in results if r["policy"] == name]
        if not rs:
            rows.append({"policy": name, "live": name == live_policy, "trades": 0})
            continue
        rr = [r["r"] for r in rs]
        reasons: dict[str, int] = {}
        for r in rs:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        rows.append({
            "policy": name,
            "live": name == live_policy,
            "trades": len(rs),
            "avg_r": round(sum(rr) / len(rr), 3),
            "total_r": round(sum(rr), 2),
            "win_rate": round(sum(1 for x in rr if x > 0) / len(rr) * 100, 1),
            "avg_hold_h": round(sum(r["hold_h"] for r in rs) / len(rs), 1),
            "exits": reasons,
        })
    return rows


CONFIDENCE_BANDS = ((0, 60, "<60%"), (60, 65, "60–64%"), (65, 70, "65–69%"), (70, 101, "70%+"))


def claude_calibration(results: list[dict], policy: str = DEFAULT_POLICY) -> dict:
    """Does Claude's approval predict results? Compared under one exit policy so only the
    entry decision differs: approved (traded) vs declined (tracked as rules-only)."""
    rows = [r for r in results if r["policy"] == policy and r.get("claude_verdict")]

    def stats(group: list[dict]) -> dict:
        if not group:
            return {"trades": 0}
        rr = [g["r"] for g in group]
        return {"trades": len(group), "avg_r": round(sum(rr) / len(rr), 3),
                "win_rate": round(sum(1 for x in rr if x > 0) / len(rr) * 100, 1),
                "total_r": round(sum(rr), 2)}

    by_verdict = {v: stats([r for r in rows if r["claude_verdict"] == v]) for v in ("approved", "declined")}
    by_confidence = []
    for lo, hi, label in CONFIDENCE_BANDS:
        grp = [r for r in rows if r.get("claude_confidence") is not None and lo <= r["claude_confidence"] < hi]
        by_confidence.append({"band": label, **stats(grp)})
    return {"policy": policy, "by_verdict": by_verdict, "by_confidence": by_confidence}
