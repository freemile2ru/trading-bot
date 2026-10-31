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
"""
from __future__ import annotations

import os

LANES: dict[str, str] = {
    "ta": "Scanner + Claude",
    "events": "Event shorts (listings & delistings)",
    "unlocks": "Unlock shorts (cliff unlocks)",
    "weekly": "Weekly near-high / near-low basket",
}
CELL_N = int(os.getenv("AUTOTRADER_LANE_CELL_N", "20"))      # rolling trades per cell
MIN_N = int(os.getenv("AUTOTRADER_LANE_MIN_N", "10"))        # trades before a cell can change the size
MEASURE = float(os.getenv("AUTOTRADER_LANE_MEASURE_X", "0.25"))
FULL = 1.0
RULE = (f"Each lane's size follows its last {CELL_N} trades in the current regime (or on the coin): under {MIN_N} trades → "
        f"equal size; average R > 0 → full size; ≤ 0 → {MEASURE:g}× measuring size. Set 2026-10-29, before it ran.")


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
    return "ta"


def _r(trade: dict, hedges: dict[str, float]) -> float | None:
    """Net R of a closed trade; a pair's coin leg carries its hedge leg's P&L. Hedge legs score nothing themselves."""
    if trade.get("event_leg") == "hedge" or trade.get("unlock_leg") == "hedge":
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
    rs: dict[str, dict] = {lane: {"all": [], "by_regime": {}, "by_symbol": {}} for lane in LANES}
    for t in sorted(closed, key=lambda t: t.get("closed_ts") or t.get("opened_ts") or 0):
        r = _r(t, hedges)
        if r is None:
            continue
        lane = lane_of(t)
        rs[lane]["all"].append(r)
        rs[lane]["by_regime"].setdefault(regime_key(t.get("regime_at_entry")), []).append(r)
        rs[lane]["by_symbol"].setdefault(t.get("symbol"), []).append(r)
    return {lane: {"all": _cell(v["all"]),
                   "by_regime": {k: _cell(x) for k, x in v["by_regime"].items()},
                   "by_symbol": {k: _cell(x) for k, x in v["by_symbol"].items()}} for lane, v in rs.items()}


def multiplier(lane: str, regime: str, symbol: str | None, card: dict) -> tuple[float, str]:
    """The size multiplier for a new entry in `lane`, and the reason, under RULE."""
    c = card.get(lane) or {}
    cell = (c.get("by_regime") or {}).get(regime)
    where = f"in {regime}"
    if not cell or cell["n"] < MIN_N:
        cell = (c.get("by_symbol") or {}).get(symbol) if symbol else None
        where = f"on {symbol}"
    if not cell or cell["n"] < MIN_N:
        have = ((c.get("by_regime") or {}).get(regime) or {}).get("n", 0)
        return FULL, f"equal size — {have} of {MIN_N} trades {('in ' + regime)} so far"
    if cell["avg_r"] > 0:
        return FULL, f"full size — {cell['n']} trades {where} averaging {cell['avg_r']:+.2f}R"
    return MEASURE, f"measuring size — {cell['n']} trades {where} averaging {cell['avg_r']:+.2f}R"


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
                      "open": sum(1 for t in open_trades if lane_of(t) == lane
                                  and t.get("event_leg") != "hedge" and t.get("unlock_leg") != "hedge"),
                      "regimes": c["by_regime"]})
    return {"regime": key, "regime_label": (regime or {}).get("label"), "rule": RULE, "cell_n": CELL_N, "min_n": MIN_N,
            "measure_x": MEASURE, "lanes": lanes}
