"""Lanes: the autotrader's entry sources, run side by side and sized by what each has earned where.

  ta       the scanner's conviction with Claude's confirmation — the original autotrader
  events   Binance listing / delisting shorts, mirrored from the event book
  unlocks  cliff-unlock shorts, mirrored from the unlock book
  weekly   the Monday near-high / near-low basket, mirrored from the weekly book

Every trade carries its lane and the market regime at entry (BTC trend × 30-day volatility bucket). The
allocator keeps a rolling scorecard per lane, per regime and per coin, and sets each new entry's size:

  rule (set 2026-10-29, before any of it ran)
    * a lane's cell is its last CELL_N closed trades in the current regime (or, failing that, on the coin)
    * fewer than MIN_N trades in the cell → equal size (1.0×): nothing is decided on three trades
    * cell average R > 0 → full size (1.0×); ≤ 0 → measuring size (MEASURE×), so the lane keeps producing
      evidence without costing much. A lane that stays negative everywhere is, in effect, retired by its own record.

The scorecard is recomputed from the ledger on every call — nothing is remembered that the trades don't show.

  amendment (2026-11-02, after the executor's first two days: every closed loser was a long, every closed short won,
  and the signal books' own records said the same of their long sides)
    * a side is sized by its own record only: the lane's last CELL_N longs (or shorts) in the current regime; failing
      MIN_N there, that side over every regime; failing that, equal size. A mixed cell never sizes a side. So a lane whose longs lose while its
      shorts pay keeps its shorts at full size and its longs at measuring size.
    * a mirror lane's record starts with its signal book's own closed trades from before the lane began (the lane
      executes that book's rule, so the book's history is the rule's history) — tagged `prehistory`.
    * the ledger's NET exposure (longs − shorts, every position) is capped at NET_CAP_PCT of equity: a leg that would
      push it further past the cap in its own direction is refused. Six longs against two shorts on a flat tape cost
      −$800 while BTC moved −0.3%.
"""
from __future__ import annotations

import os

LANES: dict[str, str] = {
    "ta": "Scanner + Claude (retired 2026-11-03)",
    "reversal": "Reversal score (the scanner's readings, faded)",
    "events": "Event shorts (listings & delistings)",
    "unlocks": "Unlock shorts (cliff unlocks)",
    "weekly": "Weekly near-high / near-low basket",
    "cascade": "Cascade fade (+1h, 4h)",
    "factor": "Multi-factor basket",
}
# Which plan entry funds each lane (trading/capital.PLAN): the executed books keep their plan entries as lane budgets.
LANE_BOOK: dict[str, str] = {"ta": "auto", "reversal": "auto", "events": "events", "unlocks": "unlocks", "weekly": "weekly_rs",
                             "cascade": "cascade", "factor": "multi_factor"}
CELL_N = int(os.getenv("AUTOTRADER_LANE_CELL_N", "20"))      # rolling trades per cell
MIN_N = int(os.getenv("AUTOTRADER_LANE_MIN_N", "10"))        # trades before a cell can change the size
MEASURE = float(os.getenv("AUTOTRADER_LANE_MEASURE_X", "0.25"))
FULL = 1.0
NET_CAP_PCT = float(os.getenv("AUTOTRADER_NET_CAP_PCT", "25"))   # |longs − shorts| of the ledger's equity, every position counted
RULE = (f"Each lane's size follows its last {CELL_N} trades on the same side in the current regime (then the regime, the coin, the side): "
        f"under {MIN_N} trades → equal size; average R > 0 → full size; ≤ 0 → {MEASURE:g}× measuring size. Set 2026-10-29, sides added "
        f"2026-11-02. Net exposure is capped at {NET_CAP_PCT:g}% of equity.")


def regime_key(r: dict | None) -> str:
    """'up/calm', 'down/wild', … from a trading.regime reading; 'unknown' when BTC data is missing."""
    if not r or not r.get("btc_trend"):
        return "unknown"
    vol = r.get("btc_vol_30d")
    bucket = "?" if vol is None else "calm" if vol < 40 else "normal" if vol < 70 else "wild"
    return f"{r['btc_trend']}/{bucket}"


def lane_of(trade: dict) -> str:
    """A trade's lane — recorded on entry; older trades are placed by what they carry."""
    lane = trade.get("lane")
    if lane in LANES:
        return lane
    if trade.get("event_id"):
        return "events"
    if trade.get("unlock_id"):
        return "unlocks"
    if trade.get("weekly_id"):
        return "weekly"
    if trade.get("cascade_id"):
        return "cascade"
    if trade.get("factor_id"):
        return "factor"
    if trade.get("reversal_id"):
        return "reversal"
    return "ta"


ADMIN_CLOSES = {"resize", "dedup", "test_artifact", "stale_mirror"}      # bookkeeping closes, not the lane's trades: they score nothing


def _r(trade: dict, hedges: dict[str, float]) -> float | None:
    """Net R of a closed trade; a pair's coin leg carries its hedge leg's P&L. Hedge legs score nothing themselves."""
    if trade.get("event_leg") == "hedge" or trade.get("unlock_leg") == "hedge" or trade.get("close_reason") in ADMIN_CLOSES:
        return None
    risk = trade.get("risk_usd") or 0
    if risk <= 0:
        return None
    pnl = trade.get("pnl_usd") or 0.0
    pid = trade.get("event_id") or trade.get("unlock_id")
    if pid:
        pnl += hedges.get(pid, 0.0)
    return pnl / risk


def _cell(rs: list[float]) -> dict:
    rs = rs[-CELL_N:]
    return {"n": len(rs), "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "net_r": round(sum(rs), 2)}


def scorecard(closed: list[dict]) -> dict:
    """{lane: {"all": cell, "by_regime": {key: cell}, "by_symbol": {sym: cell}}} from closed trades, oldest first."""
    hedges: dict[str, float] = {}
    for t in closed:
        pid = t.get("event_id") or t.get("unlock_id")
        if pid and (t.get("event_leg") == "hedge" or t.get("unlock_leg") == "hedge"):
            hedges[pid] = hedges.get(pid, 0.0) + (t.get("pnl_usd") or 0.0)
    rs: dict[str, dict] = {lane: {"all": [], "by_regime": {}, "by_symbol": {}, "by_side": {}, "by_regime_side": {}} for lane in LANES}
    for t in sorted(closed, key=lambda t: t.get("closed_ts") or t.get("opened_ts") or 0):
        r = _r(t, hedges)
        if r is None:
            continue
        lane = lane_of(t)
        key = regime_key(t.get("regime_at_entry"))
        side = t.get("direction") or "?"
        rs[lane]["all"].append(r)
        rs[lane]["by_regime"].setdefault(key, []).append(r)
        rs[lane]["by_symbol"].setdefault(t.get("symbol"), []).append(r)
        rs[lane]["by_side"].setdefault(side, []).append(r)
        rs[lane]["by_regime_side"].setdefault(f"{key}|{side}", []).append(r)
    return {lane: {"all": _cell(v["all"]),
                   **{dim: {k: _cell(x) for k, x in v[dim].items()} for dim in ("by_regime", "by_symbol", "by_side", "by_regime_side")}}
            for lane, v in rs.items()}


def multiplier(lane: str, regime: str, symbol: str | None, card: dict, direction: str | None = None) -> tuple[float, str]:
    """The size multiplier for a new entry in `lane`, and the reason, under RULE: the first cell with MIN_N trades among
    (regime, side) → regime → coin → side decides."""
    c = card.get(lane) or {}
    # A side is sized by its own record only (2026-11-03): the side in this regime, then the side over every regime,
    # else equal size. A cell that mixes sides never sizes one — the reversal shorts (+0.14R over 7) were cut to
    # measuring size by the regime cell (−0.07R over 12), which was the longs' losses. Without a side (the panel's
    # lane-level number) the regime's cell and the coin's decide as before.
    tries = []
    if direction:
        tries.append(((c.get("by_regime_side") or {}).get(f"{regime}|{direction}"), f"{direction}s in {regime}"))
        tries.append(((c.get("by_side") or {}).get(direction), f"{direction}s in every regime"))
    else:
        tries.append(((c.get("by_regime") or {}).get(regime), f"in {regime}"))
        if symbol:
            tries.append(((c.get("by_symbol") or {}).get(symbol), f"on {symbol}"))
    for cell, where in tries:
        if cell and cell["n"] >= MIN_N:
            if cell["avg_r"] > 0:
                return FULL, f"full size — {cell['n']} {where} averaging {cell['avg_r']:+.2f}R"
            return MEASURE, f"measuring size — {cell['n']} {where} averaging {cell['avg_r']:+.2f}R"
    have = (tries[0][0] or {}).get("n", 0)
    return FULL, f"equal size — {have} of {MIN_N} {tries[0][1]} so far"


def net_exposure(open_trades: list[dict]) -> float:
    """Longs minus shorts, in notional dollars, every position counted (a hedge is exposure too)."""
    return sum((t.get("notional_usd") or 0.0) * (1 if t.get("direction") == "long" else -1) for t in open_trades)


def net_cap_reason(direction: str, notional: float, open_trades: list[dict], equity: float) -> str | None:
    """Why a new leg breaks the net-exposure cap, or None. A leg that REDUCES the imbalance is always allowed."""
    net = net_exposure(open_trades)
    after = net + (notional if direction == "long" else -notional)
    cap = equity * NET_CAP_PCT / 100
    if abs(after) > cap and abs(after) > abs(net):
        return (f"net exposure cap: the ledger is net {'long' if net >= 0 else 'short'} ${abs(net):,.0f} "
                f"({abs(net) / equity * 100:.0f}% of equity); this {direction} would make it ${abs(after):,.0f}, the cap is {NET_CAP_PCT:g}%")
    return None


def status(closed: list[dict], open_trades: list[dict], regime: dict | None) -> dict:
    """What the panel shows: the regime now, and each lane's size, reason and record."""
    card = scorecard(closed)
    key = regime_key(regime)
    lanes = []
    for lane, label in LANES.items():
        mult, why = multiplier(lane, key, None, card)
        c = card[lane]
        lanes.append({"lane": lane, "label": label, "mult": mult, "why": why, "all": c["all"],
                      "regime_cell": c["by_regime"].get(key) or {"n": 0, "avg_r": None, "net_r": 0.0},
                      "sides": {d: {"mult": multiplier(lane, key, None, card, d)[0], "all": c["by_side"].get(d) or {"n": 0, "avg_r": None, "net_r": 0.0},
                                    "regime": c["by_regime_side"].get(f"{key}|{d}") or {"n": 0, "avg_r": None, "net_r": 0.0}} for d in ("long", "short")},
                      "open": sum(1 for t in open_trades if lane_of(t) == lane
                                  and t.get("event_leg") != "hedge" and t.get("unlock_leg") != "hedge"),
                      "regimes": c["by_regime"]})
    from autotrader import hold_check, sizing
    return {"regime": key, "regime_label": (regime or {}).get("label"), "rule": RULE, "cell_n": CELL_N, "min_n": MIN_N,
            "measure_x": MEASURE, "net_cap_pct": NET_CAP_PCT, "net_exposure_usd": round(net_exposure(open_trades), 2), "lanes": lanes,
            "sizing": {**sizing.describe(), "heat_used_usd": round(sizing.heat_usd(open_trades), 2)}, "hold": hold_check.describe(),
            "reversal_audit": reversal_exit_audit([t for t in closed if lane_of(t) == "reversal"])}


def reversal_exit_audit(closed: list[dict]) -> dict:
    """The reversal legs' exits, actual against two lock rules replayed on each leg's recorded peak (an upper bound: the
    peak is a print, the lock acts from the next bar): the rule before 2026-11-09 (arm 3% of notional, keep 50%) and the
    one since (arm 1%, keep 65%). By exit reason, with the hold check's rules named where they closed a leg."""
    def cf(arm_pct: float, keep: float) -> float:
        out = 0.0
        for t in closed:
            s = 1 if t.get("direction") == "long" else -1
            q, e, peak = t.get("quantity") or 0, t.get("entry_price") or 0, t.get("peak_price")
            pnl = t.get("pnl_usd") or 0.0
            peak_pnl = (peak - e) * s * q if peak and e else 0.0
            n = t.get("notional_usd") or (e * q) or 1
            out += max(pnl, keep * peak_pnl - (t.get("fees_usd") or 0.0)) if peak_pnl / n * 100 >= arm_pct else pnl
        return round(out, 2)
    by_exit: dict[str, dict] = {}
    for t in closed:
        k = t.get("hold_rule") or t.get("close_reason") or "?"
        d = by_exit.setdefault(k, {"n": 0, "pnl": 0.0, "wins": 0})
        d["n"] += 1
        d["pnl"] = round(d["pnl"] + (t.get("pnl_usd") or 0.0), 2)
        d["wins"] += 1 if (t.get("pnl_usd") or 0) > 0 else 0
    sized = [t for t in closed if t.get("sizing")]
    return {"n": len(closed), "actual": round(sum(t.get("pnl_usd") or 0.0 for t in closed), 2), "old_lock_cf": cf(3.0, 0.5), "new_lock_cf": cf(1.0, 0.65),
            "by_exit": by_exit, "sized_n": len(sized), "sized_pnl": round(sum(t.get("pnl_usd") or 0.0 for t in sized), 2)}
