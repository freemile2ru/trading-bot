"""Copy trading — mirrors consistent lead traders from Binance's copy-trading board and Hyperliquid, on paper.

Why (2026-10-28): both venues publish traders' records and current positions. A leader chosen for consistency —
not headline ROI — and copied only when the trade hasn't already run is a bet on other people's edge, judged by
the same verdict as our own books.

Two sources, one engine:
  binance      the copy-trading leaderboard (data/copy_trading_client.py): profile stats, daily ROI curve, position
               history with open positions and their open time
  hyperliquid  on-chain wallets (data/hyperliquid_client.py): leaderboard windows, live positions, fills — a record
               that can't be curated. Positions carry no open time, so a position is "new" when it first appears
               between two polls; what a wallet already held when tracking began is never copied.
  (OKX's official copy-trading API was tested and rejected: a leader's current positions come back with the
  instrument, entry and size blanked, so there is nothing to mirror.)

Choosing leaders (daily, LEADERS_N per venue):
  binance   ≥ MIN_COPIERS copiers, ≥ MIN_AUM, copiers in profit, ≥ MIN_DAYS running, drawdown ≤ MAX_MDD, win rate ≥
            MIN_WIN; own curve: ≥ MIN_WEEKS_POS weeks positive and last 30 days positive
  hyperliquid  equity $50k–$20M (bigger is a market maker), month ROI 5–300% (more is deposits, not trading), week
            and all-time positive, monthly turnover 2–300× equity (more is a bot); then the fills say the rest
  both      a readable profile from closed trades: ≥ 10, median hold ≥ 1h (no scalps), losses on record and win
            rate ≤ 95% (the never-loses shape is a martingale); score = curve Sharpe × positive weeks − drawdown

Mirroring (every POLL_S):
  new       a position the leader opened that we haven't seen → judged before entry against THAT leader's record:
              too late (age)    older than LATE_AGE_FRAC of their median hold
              too late (price)  already moved ≥ LATE_MOVE_FRAC of their median winning move in their favour
              already failing   moved against them by more than their median losing move
            skipped trades are recorded and their outcome filled in when the leader closes — the rule is audited
  consensus several leaders (any venue) on the same side of a coin at once → size × CONSENSUS_X
  size      LEG_PCT of equity notional, the leader's leverage capped at MAX_LEVERAGE, a DISASTER_PCT price stop
  exit      the leader closes (or has closed ≥ half), MAX_HOLD_X × their median hold (at least a day, at most a week),
            or the stop. A leader who drops off the daily selection is still followed out of the copies we hold
"""
from __future__ import annotations

import logging
import os
import statistics
import time

from storage import state as kv
from trading import book_switches, monitor

logger = logging.getLogger("experiments.copy_trading")

BOOK = "copy_trading"
LABEL = "Copy trading (Binance & Hyperliquid leaders)"
VENUES = ("binance", "hyperliquid")
LEADERS_N = int(os.getenv("COPY_LEADERS_N", "5"))                 # per venue
REFRESH_LEADERS_S = 24 * 3600
POLL_S = int(os.getenv("COPY_POLL_S", "90"))
MIN_COPIERS, MIN_AUM, MIN_DAYS = 50, 100_000.0, 90
MAX_MDD, MIN_WIN, MIN_WEEKS_POS = 30.0, 50.0, 60.0
MIN_HOLD_H, MAX_WIN = 1.0, 95.0
HL_EQUITY = (50_000.0, 20_000_000.0)
HL_MONTH_ROI = (0.05, 3.0)
HL_TURNOVER = (2.0, 300.0)
LEG_PCT = float(os.getenv("COPY_LEG_PCT", "15"))                  # 15% a copy since 2026-10-29 (was 5)
MAX_LEVERAGE = int(os.getenv("COPY_MAX_LEVERAGE", "10"))          # 10× cap since 2026-10-29 (was 5)
MAX_OPEN = int(os.getenv("COPY_MAX_OPEN", "12"))
LATE_AGE_FRAC, LATE_MOVE_FRAC, ADVERSE_FRAC = 0.25, 0.5, 1.0
CONSENSUS_X = float(os.getenv("COPY_CONSENSUS_X", "1.5"))
DISASTER_PCT = float(os.getenv("COPY_DISASTER_PCT", "8"))
MAX_HOLD_X, MAX_HOLD_H = 3.0, 7 * 24
MIN_HOLD_CAP_H = 24.0                    # a copy of a fast leader still gets a day: their hold is a median, not a limit
SKIP_KEEP = 200

_state = {"last_poll": 0.0}
_perp_cache: dict = {"at": 0.0, "symbols": set()}

# ── The record (data/copy_trading.db): what every leader held at every poll, the price then, and what we did ──
# Kept so the too-late rule, the leaders' own edge and alternative copy rules can be studied later on real history.
LOG_DB = os.path.join(os.path.dirname(__file__), "..", "data", "copy_trading.db")
LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    poll_ts REAL NOT NULL, leader_id TEXT NOT NULL, venue TEXT, position_id TEXT NOT NULL, symbol TEXT, side TEXT,
    entry REAL, leverage REAL, remaining REAL, mark REAL, moved_pct REAL, opened_ts REAL, state TEXT,
    PRIMARY KEY (poll_ts, leader_id, position_id));
CREATE INDEX IF NOT EXISTS ix_obs_pos ON observations(leader_id, position_id, poll_ts);
CREATE TABLE IF NOT EXISTS leader_snapshots (ts REAL NOT NULL, leader_id TEXT NOT NULL, venue TEXT, data TEXT NOT NULL, PRIMARY KEY (ts, leader_id));
"""


def _log_con():
    import sqlite3
    con = sqlite3.connect(LOG_DB, timeout=10)
    con.executescript(LOG_SCHEMA)
    return con


def log_observations(now: float, by_leader: dict, venues: dict[str, str]) -> int:
    rows = [(now, lid, venues.get(lid), p.get("position_id"), p["symbol"], p["side"], p.get("entry"), p.get("leverage"), p.get("remaining"),
             p.get("mark"), p.get("moved_pct"), p.get("opened_ts"), p.get("state")) for lid, ps in by_leader.items() for p in ps]
    if not rows:
        return 0
    con = _log_con()
    with con:
        con.executemany("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.close()
    return len(rows)


def log_leaders(now: float, chosen: list[dict]) -> None:
    import json
    con = _log_con()
    with con:
        con.executemany("INSERT OR IGNORE INTO leader_snapshots VALUES (?,?,?,?)",
                        [(now, L["id"], L.get("venue"), json.dumps({k: v for k, v in L.items() if k != "curve"}, default=str)) for L in chosen])
    con.close()


def log_status() -> dict:
    try:
        con = _log_con()
        n, first, last = con.execute("SELECT COUNT(*), MIN(poll_ts), MAX(poll_ts) FROM observations").fetchone()
        positions = con.execute("SELECT COUNT(DISTINCT leader_id || ':' || position_id) FROM observations").fetchone()[0]
        snaps = con.execute("SELECT COUNT(DISTINCT ts) FROM leader_snapshots").fetchone()[0]
        con.close()
    except Exception as e:
        logger.debug("copy log unavailable: %s", e)
        return {"observations": 0, "positions": 0, "since": None, "through": None, "selections": 0, "db": LOG_DB}
    return {"observations": n, "positions": positions, "since": first, "through": last, "selections": snaps, "db": LOG_DB}


def observations(leader_id: str | None = None, since: float | None = None, limit: int = 5000) -> list[dict]:
    """Rows from the record, oldest first — the input for a later lab."""
    con = _log_con()
    con.row_factory = __import__("sqlite3").Row
    q, args = "SELECT * FROM observations WHERE poll_ts >= ?", [since or 0.0]
    if leader_id:
        q += " AND leader_id = ?"
        args.append(leader_id)
    rows = [dict(r) for r in con.execute(q + " ORDER BY poll_ts LIMIT ?", args + [limit])]
    con.close()
    return rows


def _k(*parts: str) -> str:
    return kv.key("copy", *parts)


async def _perps(now: float) -> set[str]:
    if now - _perp_cache["at"] > 3600 or not _perp_cache["symbols"]:
        from data.binance_client import fetch_perpetual_symbols
        try:
            _perp_cache.update(at=now, symbols=set(await fetch_perpetual_symbols()))
        except Exception as e:
            logger.debug("perp list unavailable: %s", e)
    return _perp_cache["symbols"]


# ── Leaders ────────────────────────────────────────────────────────────────────

def leaders() -> list[dict]:
    return (kv.get_sync(_k("leaders")) or {}).get("leaders") or []


def leaders_refreshed_at() -> float | None:
    return (kv.get_sync(_k("leaders")) or {}).get("at")


def profile_from_history(rows: list[dict]) -> dict:
    """What closed positions say: typical hold and typical price moves. Binance rows carry ROI on margin (÷ leverage
    gives the price move); Hyperliquid episodes carry the move directly."""
    closed = [r for r in rows if r.get("closed_ts") and r.get("opened_ts")]
    if len(closed) < 10:
        return {"n": len(closed), "median_hold_h": None, "win_move_pct": None, "loss_move_pct": None, "win_rate": None}
    holds = [(r["closed_ts"] - r["opened_ts"]) / 3600 for r in closed]
    moves = [r["move_pct"] if "move_pct" in r else r["roi"] / (r.get("leverage") or 1.0) * 100 for r in closed]
    wins, losses = [m for m in moves if m > 0], [-m for m in moves if m <= 0]
    span_days = max((max(r["closed_ts"] for r in closed) - min(r["opened_ts"] for r in closed)) / 86400, 1)
    return {"n": len(closed), "median_hold_h": round(statistics.median(holds), 1),
            "win_move_pct": round(statistics.median(wins), 3) if wins else None,
            "loss_move_pct": round(statistics.median(losses), 3) if losses else None,
            "win_rate": round(100 * len(wins) / len(closed), 1), "trades_per_day": round(len(closed) / span_days, 2)}


def passes(row: dict, stats: dict) -> str | None:
    """Why a Binance leaderboard row fails the consistency bar, or None when it passes."""
    if row["copiers"] < MIN_COPIERS:
        return f"{row['copiers']} copiers"
    if row["aum"] < MIN_AUM:
        return f"AUM ${row['aum']:,.0f}"
    if row["copier_pnl"] <= 0:
        return "copiers losing"
    if row["mdd"] > MAX_MDD:
        return f"drawdown {row['mdd']:.0f}%"
    if row["win_rate"] < MIN_WIN:
        return f"win rate {row['win_rate']:.0f}%"
    return curve_passes(stats)


def curve_passes(stats: dict) -> str | None:
    if (stats.get("days") or 0) < 30:
        return f"{stats.get('days')} days of curve"
    if (stats.get("weeks_positive_pct") or 0) < MIN_WEEKS_POS:
        return f"{stats.get('weeks_positive_pct')}% of weeks positive"
    if (stats.get("last_30d_pct") or 0) <= 0:
        return "last 30 days negative"
    return None


def hl_passes(row: dict) -> str | None:
    """Why a Hyperliquid wallet fails the cheap bar (before its fills are read), or None."""
    eq = row["equity"]
    if not (HL_EQUITY[0] <= eq <= HL_EQUITY[1]):
        return f"equity ${eq:,.0f}"
    m = row["month"]["roi"]
    if not (HL_MONTH_ROI[0] <= m <= HL_MONTH_ROI[1]):
        return f"month ROI {m * 100:.0f}%"
    if row["week"]["roi"] <= 0 or row["all"]["roi"] <= 0 or row["month"]["pnl"] <= 0:
        return "a window negative"
    turn = row["month"]["volume"] / eq if eq else 0
    if not (HL_TURNOVER[0] <= turn <= HL_TURNOVER[1]):
        return f"turnover {turn:.0f}× a month"
    return None


def profile_passes(prof: dict) -> str | None:
    """Shapes we can't or shouldn't copy: too few closed trades, scalps faster than our poll, or the never-loses
    pattern (every trade a winner, no recorded loss) that marks a grid/martingale that holds losers until it blows up."""
    if (prof.get("n") or 0) < 10:
        return f"{prof.get('n')} closed trades"
    if not prof.get("median_hold_h") or prof["median_hold_h"] < MIN_HOLD_H:
        return f"median hold {prof.get('median_hold_h')}h — scalper"
    if prof.get("loss_move_pct") is None or (prof.get("win_rate") or 0) > MAX_WIN:
        return f"win rate {prof.get('win_rate')}% with no recorded losses — martingale shape"
    return None


def score(stats: dict) -> float:
    return round((stats.get("sharpe") or 0) * (stats.get("weeks_positive_pct") or 0) / 100 - (stats.get("mdd_pct") or 0) / 100, 3)


async def _select_binance(now: float) -> list[dict]:
    from data import copy_trading_client as cc
    pool: dict[str, dict] = {}
    for sort in ("ROI", "PNL", "SHARP_RATIO", "COPIER_PNL"):
        try:
            for r in await cc.leaderboard("90D", sort, 1, 50):
                pool.setdefault(r["id"], r)
        except Exception as e:
            logger.warning("Binance leaderboard %s unavailable: %s", sort, e)
    scored = []
    for r in pool.values():
        stats = cc.curve_stats(r["curve"])
        if passes(r, stats):
            continue
        thin = r["curve"][:: max(1, len(r["curve"]) // 45)]
        scored.append({**{k: v for k, v in r.items() if k != "curve"}, "curve": thin, "stats": stats, "score": score(stats), "venue": "binance"})
    scored.sort(key=lambda x: -x["score"])
    chosen = []
    for r in scored[:LEADERS_N * 3]:
        try:
            det = await cc.detail(r["id"])
            if (now - det["start_ts"]) / 86400 < MIN_DAYS or det["status"] != "ACTIVE":
                continue
            hist = await cc.positions(r["id"], pages=2)
        except Exception as e:
            logger.debug("Leader %s detail unavailable: %s", r["id"], e)
            continue
        prof = profile_from_history(hist)
        if profile_passes(prof):
            continue
        chosen.append({**r, "nickname_en": det.get("nickname_en"), "days_running": round((now - det["start_ts"]) / 86400),
                       "leverage_tier": det.get("leverage_tier"), "badge": det.get("badge"), "profile": prof})
        if len(chosen) >= LEADERS_N:
            break
    return chosen


async def _select_hyperliquid(now: float) -> list[dict]:
    from data import copy_trading_client as cc
    from data import hyperliquid_client as hl
    try:
        board = await hl.leaderboard()
    except Exception as e:
        logger.warning("Hyperliquid leaderboard unavailable: %s", e)
        return []
    import asyncio
    cands = [r for r in board if hl_passes(r) is None]
    cands.sort(key=lambda r: -(r["month"]["roi"] * min(r["all"]["roi"], 3.0)))
    chosen = []
    for i, r in enumerate(cands[:LEADERS_N * 8]):
        if i:
            await asyncio.sleep(0.6)                      # the public info API rate-limits bursts
        try:
            f = await hl.fills(r["address"], hl.now_ms() - 60 * 86400 * 1000)
        except Exception as e:
            logger.debug("Hyperliquid fills for %s unavailable: %s", r["address"], e)
            continue
        prof = profile_from_history(hl.episodes(f))
        if profile_passes(prof):
            continue
        curve = hl.pnl_curve(f, r["equity"])
        stats = cc.curve_stats(curve, active_only=True)
        if curve_passes(stats) or (stats.get("active_weeks") or 0) < 4:
            continue
        chosen.append({"id": r["address"], "venue": "hyperliquid", "nickname": r.get("name") or f"{r['address'][:6]}…{r['address'][-4:]}",
                       "nickname_en": r.get("name"), "roi": round(r["month"]["roi"] * 100, 2), "mdd": stats.get("mdd_pct") or 0.0,
                       "win_rate": prof["win_rate"], "copiers": 0, "aum": r["equity"], "copier_pnl": r["month"]["pnl"],
                       "sharpe": stats.get("sharpe"), "curve": curve[:: max(1, len(curve) // 45)], "stats": stats, "score": score(stats),
                       "days_running": None, "leverage_tier": None, "badge": "on-chain", "profile": prof,
                       "turnover": round(r["month"]["volume"] / r["equity"], 1) if r["equity"] else None})
        if len(chosen) >= LEADERS_N:
            break
    chosen.sort(key=lambda x: -x["score"])
    return chosen


def remember(L: dict) -> None:
    kv.set_sync(_k("leader", L["id"]), {k: v for k, v in L.items() if k != "curve"}, 30 * 86400)


def remembered(leader_id: str) -> dict | None:
    return kv.get_sync(_k("leader", leader_id))


def leaders_to_poll() -> list[dict]:
    """The current selection plus any past leader whose copies we still hold — they get followed out."""
    Ls = leaders()
    have = {L["id"] for L in Ls}
    for t in _wallet().get_portfolio()["open_trades"]:
        lid = t.get("leader_id")
        if lid and lid not in have:
            L = remembered(lid)
            if L:
                Ls.append({**L, "retired": True})
                have.add(lid)
    return Ls


async def select_leaders(now: float | None = None) -> list[dict]:
    now = now or time.time()
    chosen = await _select_binance(now) + await _select_hyperliquid(now)
    for L in chosen:
        remember(L)
    kv.set_sync(_k("leaders"), {"at": now, "leaders": chosen}, 7 * 86400)
    try:
        log_leaders(now, chosen)
    except Exception as e:
        logger.debug("copy leader log failed: %s", e)
    logger.info("Copy trading: %d leaders chosen — %s", len(chosen),
                ", ".join(f"{c.get('nickname_en') or c['nickname']} [{c['venue']}, {c['score']}]" for c in chosen))
    return chosen


# ── Judging an entry ───────────────────────────────────────────────────────────

def evaluate(pos: dict, profile: dict, mark: float, now: float) -> tuple[bool, str, float, float]:
    """(enter?, reason, price move since the leader's entry in their favour %, age h)."""
    side = 1 if pos["side"] == "long" else -1
    moved = (mark / pos["entry"] - 1) * side * 100 if pos.get("entry") else 0.0
    age_h = (now - pos["opened_ts"]) / 3600
    hold = profile.get("median_hold_h") or 24.0
    win_move, loss_move = profile.get("win_move_pct") or 1.0, profile.get("loss_move_pct") or 1.0
    if age_h > LATE_AGE_FRAC * hold:
        return False, f"too late: {age_h:.1f}h old, leader's median hold {hold:.0f}h", moved, age_h
    if moved >= LATE_MOVE_FRAC * win_move:
        return False, f"too late: already {moved:+.2f}% in their favour (their median win {win_move:.2f}%)", moved, age_h
    if moved <= -ADVERSE_FRAC * loss_move:
        return False, f"already failing: {moved:+.2f}% against them (their median loss {loss_move:.2f}%)", moved, age_h
    return True, f"entered {moved:+.2f}% from the leader's price, {age_h:.1f}h after them", moved, age_h


# ── Positions per venue, in one shape ─────────────────────────────────────────
#   {position_id, symbol (Binance perp), side, opened_ts, closed_ts, entry, close_price, leverage, remaining, roi}

async def _binance_rows(L: dict) -> list[dict]:
    from data import copy_trading_client as cc
    return await cc.positions(L["id"], pages=2)             # an open position can sit past the first 50 rows of an active leader


async def _hyperliquid_rows(L: dict, now: float) -> list[dict]:
    """Live positions, with open times from when each first appeared; vanished ones come back once as closed."""
    from data import hyperliquid_client as hl
    perps = await _perps(now)
    live = await hl.positions(L["id"])
    key = _k("hl", L["id"])
    tracked: dict = kv.get_sync(key) or {}
    first_time = not tracked
    out, seen_now = [], {}
    for p in live:
        sym = hl.binance_symbol(p["coin"], perps)
        if not sym:
            continue
        prev = tracked.get(p["coin"])
        if prev and prev["side"] == p["side"]:
            rec = {**prev, "max_size": max(prev.get("max_size", p["size"]), p["size"]), "size": p["size"]}
        else:
            rec = {"first_seen": now if not first_time else 0.0, "side": p["side"], "entry": p["entry"], "size": p["size"],
                   "max_size": p["size"], "pid": f"{p['coin']}:{int(now)}", "symbol": sym, "leverage": p["leverage"]}
        seen_now[p["coin"]] = rec
        out.append({"position_id": rec["pid"], "symbol": sym, "side": p["side"], "opened_ts": rec["first_seen"] or 0.0, "closed_ts": None,
                    "entry": p["entry"], "close_price": None, "leverage": p["leverage"], "remaining": p["size"] / rec["max_size"] if rec["max_size"] else 0.0,
                    "roi": p["roe"], "status": "Open", "untracked": rec["first_seen"] == 0.0})
    for coin, prev in tracked.items():
        if coin not in seen_now:                                   # gone since last poll → closed
            px = await monitor.live_price(prev["symbol"])
            out.append({"position_id": prev["pid"], "symbol": prev["symbol"], "side": prev["side"], "opened_ts": prev["first_seen"] or 0.0,
                        "closed_ts": now, "entry": prev["entry"], "close_price": px, "leverage": prev.get("leverage"), "remaining": 0.0,
                        "roi": ((px / prev["entry"] - 1) * (1 if prev["side"] == "long" else -1) * (prev.get("leverage") or 1)) if px and prev.get("entry") else 0.0,
                        "status": "All Closed"})
    kv.set_sync(key, seen_now, 30 * 86400)
    return out


async def leader_rows(L: dict, now: float) -> list[dict]:
    return await (_hyperliquid_rows(L, now) if L.get("venue") == "hyperliquid" else _binance_rows(L))


# ── Mirroring ──────────────────────────────────────────────────────────────────

def _wallet():
    from experiments import books
    return books.WALLETS[BOOK]


def _seen(leader_id: str) -> set[str]:
    return set(kv.get_sync(_k("seen", leader_id)) or [])


def _mark_seen(leader_id: str, ids: set[str]) -> None:
    kv.set_sync(_k("seen", leader_id), sorted(ids)[-300:], 30 * 86400)


def skipped() -> list[dict]:
    return kv.get_sync(_k("skipped")) or []


def _record_skip(entry: dict) -> None:
    kv.set_sync(_k("skipped"), (skipped() + [entry])[-SKIP_KEEP:], 60 * 86400)


def consensus(rows_by_leader: dict[str, list[dict]]) -> dict[tuple[str, str], int]:
    """How many leaders hold each (symbol, side) right now."""
    out: dict[tuple[str, str], set] = {}
    for lid, rows in rows_by_leader.items():
        for r in rows:
            if r["closed_ts"] is None and r["remaining"] > 0:
                out.setdefault((r["symbol"], r["side"]), set()).add(lid)
    return {k: len(v) for k, v in out.items()}


async def mirror(now: float | None = None) -> dict:
    from experiments import books
    now = now or time.time()
    w = _wallet()
    opened, closed = [], []
    ours = {t.get("leader_position_id"): t for t in w.get_portfolio()["open_trades"] if t.get("leader_position_id")}
    all_rows: dict[str, dict] = {}
    rows_by_leader: dict[str, list[dict]] = {}
    Ls = leaders_to_poll()
    for L in Ls:
        if not L.get("retired"):
            remember(L)                                     # so a leader dropped tomorrow is still followed out
        try:
            rows_by_leader[L["id"]] = await leader_rows(L, now)
        except Exception as e:
            logger.debug("Positions for %s unavailable: %s", L["id"], e)
    agree = consensus(rows_by_leader)
    kv.set_sync(_k("consensus"), [{"symbol": s, "side": d, "leaders": n} for (s, d), n in agree.items() if n >= 2], 3600)
    for L in Ls:
        rows = rows_by_leader.get(L["id"])
        if rows is None:
            continue
        for r in rows:
            all_rows[r["position_id"]] = {**r, "leader_id": L["id"]}
        seen = _seen(L["id"])
        name = L.get("nickname_en") or L["nickname"]
        for r in rows:
            if r["closed_ts"] is not None or r["remaining"] <= 0 or r["position_id"] in seen or r["position_id"] in ours:
                continue
            seen.add(r["position_id"])
            if r.get("untracked") or L.get("retired") or not (books.ENABLED and book_switches.enabled(BOOK)):
                continue                                     # held before we were watching, an ex-leader, or the book is off
            mark = await monitor.live_price(r["symbol"])
            if not mark:
                _record_skip({"ts": now, "leader": name, "venue": L.get("venue"), "symbol": r["symbol"], "side": r["side"],
                              "reason": "no price feed", "mark": None, "position_id": r["position_id"]})
                continue
            ok, why, moved, age_h = evaluate(r, L.get("profile") or {}, mark, now)
            n_agree = agree.get((r["symbol"], r["side"]), 1)
            if not ok or len(w.get_portfolio()["open_trades"]) >= MAX_OPEN:
                _record_skip({"ts": now, "leader": name, "venue": L.get("venue"), "symbol": r["symbol"], "side": r["side"],
                              "reason": why if not ok else "book full", "mark": mark, "position_id": r["position_id"],
                              "leader_entry": r["entry"], "moved_pct": round(moved, 3), "age_h": round(age_h, 1), "consensus": n_agree})
                continue
            s = 1 if r["side"] == "long" else -1
            equity = w.get_portfolio()["total_equity"]
            size_x = CONSENSUS_X if n_agree >= 2 else 1.0
            t = w.open_trade(symbol=r["symbol"], direction=r["side"], entry_price=mark, stop_loss=mark * (1 - s * DISASTER_PCT / 100),
                             take_profit_1=mark * (1 + s * 0.5), take_profit_2=mark * (1 + s * 0.6), notional_usd=equity * LEG_PCT / 100 * size_x,
                             timeframe="copy", leverage=max(1, min(int(r.get("leverage") or 1), MAX_LEVERAGE)),
                             extra={"strategy": BOOK, "exit_policy": "manual", "shadows": False, "claude_verdict": "skipped",
                                    "leader_id": L["id"], "leader": name, "venue": L.get("venue"), "leader_position_id": r["position_id"],
                                    "leader_entry": r["entry"], "leader_leverage": r.get("leverage"), "moved_pct_at_entry": round(moved, 3),
                                    "age_h_at_entry": round(age_h, 1), "consensus": n_agree, "entry_note": why})
            if t.get("error"):
                logger.info("Copy %s %s refused: %s", r["side"], r["symbol"], t["error"])
            else:
                opened.append(t)
                logger.info("Copied %s [%s]: %s %s @ %.6g (%d leader(s) agree) — %s", name, L.get("venue"), r["side"], r["symbol"], mark, n_agree, why)
        _mark_seen(L["id"], seen)
    # exits: the leader closed or halved, or we've held too long
    for pid, t in ours.items():
        r = all_rows.get(pid)
        L = next((x for x in Ls if x["id"] == t.get("leader_id")), None)
        hold = (L or {}).get("profile", {}).get("median_hold_h") or 24.0
        age_h = (now - _ts(t.get("opened_at"))) / 3600
        reason = None
        if L is None:
            reason = "leader_dropped"
        elif L["id"] not in rows_by_leader:
            reason = None                                       # feed down this poll — hold
        elif r is None:
            # Fell off their list. Once could be paging or a hiccup; twice in a row is a close.
            miss = kv.get_sync(_k("missing", pid)) or 0
            if miss >= 1:
                reason = "leader_closed"
            else:
                kv.set_sync(_k("missing", pid), miss + 1, 3600)
        elif r["closed_ts"] or r["remaining"] < 0.5:
            reason = "leader_closed" if r["closed_ts"] or r["remaining"] == 0 else "leader_reduced"
        elif age_h > min(max(MAX_HOLD_X * hold, MIN_HOLD_CAP_H), MAX_HOLD_H):
            reason = "max_hold"
        if r is not None:
            kv.delete_sync(_k("missing", pid))
        if reason:
            px = await monitor.live_price(t["symbol"]) or t.get("current_price")
            if px and w.close_trade_manually(t["id"], px, reason):
                closed.append({**t, "close_reason": reason})
    # what each leader holds right now, and what we did about it — for the Copy tab
    ours_now = {t.get("leader_position_id") for t in w.get_portfolio()["open_trades"] if t.get("leader_position_id")}
    sk_by_pid = {e["position_id"]: e for e in skipped() if e.get("position_id")}
    snapshot = {}
    for L in Ls:
        live_rows = []
        for r in rows_by_leader.get(L["id"]) or []:
            if r["closed_ts"] is not None or r["remaining"] <= 0:
                continue
            mark = await monitor.live_price(r["symbol"])
            side = 1 if r["side"] == "long" else -1
            if r["position_id"] in ours_now:
                state = "copied"
            elif r.get("untracked"):
                state = "held before tracking"
            elif r["position_id"] in sk_by_pid:
                state = "skipped: " + sk_by_pid[r["position_id"]]["reason"]
            else:
                state = "seen"
            live_rows.append({"position_id": r["position_id"], "symbol": r["symbol"], "side": r["side"], "entry": r["entry"], "leverage": r.get("leverage"),
                              "opened_ts": r["opened_ts"] or None, "remaining": round(r["remaining"], 2), "mark": mark,
                              "moved_pct": round((mark / r["entry"] - 1) * side * 100, 3) if mark and r.get("entry") else None, "state": state})
        snapshot[L["id"]] = live_rows
    kv.set_sync(_k("positions"), {"at": now, "by_leader": snapshot}, 3600)
    try:
        log_observations(now, snapshot, {L["id"]: L.get("venue") for L in Ls})
    except Exception as e:
        logger.debug("copy log write failed: %s", e)
    # what the skipped trades went on to do
    sk, changed = skipped(), False
    for e in sk:
        if e.get("outcome_pct") is not None or not e.get("mark"):
            continue
        r = all_rows.get(e["position_id"])
        if r and r["closed_ts"] and r.get("close_price"):
            s = 1 if e["side"] == "long" else -1
            e["outcome_pct"] = round((r["close_price"] / e["mark"] - 1) * s * 100, 3)
            e["leader_roi"] = r.get("roi")
            changed = True
    if changed:
        kv.set_sync(_k("skipped"), sk[-SKIP_KEEP:], 60 * 86400)
    return {"opened": opened, "closed": closed}


def _ts(iso) -> float:
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(iso)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return time.time()


async def tick(now: float | None = None) -> dict:
    from experiments import books
    if not books.ENABLED:
        return {}
    now = now or time.time()
    if now - (leaders_refreshed_at() or 0) >= REFRESH_LEADERS_S and not _state.get("selecting"):
        import asyncio
        _state["selecting"] = True

        async def _run():
            try:
                await select_leaders(now)
            except Exception as e:
                logger.warning("Leader selection failed: %s", e)
            finally:
                _state["selecting"] = False
        asyncio.create_task(_run())                   # minutes of HTTP — never on the mirror's clock
    if now - _state["last_poll"] < POLL_S:
        return {}
    _state["last_poll"] = now
    return await mirror(now)


# ── Reporting ──────────────────────────────────────────────────────────────────

def status() -> dict:
    w = _wallet()
    p = w.get_portfolio(closed_limit=40)
    open_ = p["open_trades"]
    closed_all = w.all_trades()["closed"]
    wins = [t for t in closed_all if (t.get("pnl_usd") or 0) > 0]
    sk = skipped()
    judged = [e for e in sk if e.get("outcome_pct") is not None]
    snap = kv.get_sync(_k("positions")) or {}
    by_leader = snap.get("by_leader") or {}
    return {"leaders": [{**{k: L.get(k) for k in ("id", "venue", "nickname", "nickname_en", "roi", "mdd", "win_rate", "copiers", "aum", "copier_pnl",
                                                    "score", "days_running", "leverage_tier", "badge", "profile", "stats", "curve", "turnover", "retired")},
                         "positions": by_leader.get(L["id"], [])} for L in leaders_to_poll()],
            "positions_at": snap.get("at"),
            "why_not": _why_not(by_leader, open_),
            "log": log_status(),
            "leaders_refreshed_at": leaders_refreshed_at(),
            "consensus": kv.get_sync(_k("consensus")) or [],
            "book": {"equity": p["total_equity"], "initial": p["initial_balance"], "open": len(open_), "closed": len(closed_all),
                     "win_rate": round(100 * len(wins) / len(closed_all), 1) if closed_all else None,
                     "net_pnl": round(sum(t.get("pnl_usd") or 0 for t in closed_all), 2),
                     "recent": [{"symbol": t["symbol"], "direction": t["direction"], "leader": t.get("leader"), "venue": t.get("venue"), "pnl_usd": t.get("pnl_usd"),
                                 "roe_pct": t.get("roe_pct"), "close_reason": t.get("close_reason"), "opened_at": t.get("opened_at"),
                                 "closed_at": t.get("closed_at"), "moved_pct_at_entry": t.get("moved_pct_at_entry")} for t in p["closed_trades"]]},
            "open": [{"symbol": t["symbol"], "direction": t["direction"], "leader": t.get("leader"), "venue": t.get("venue"), "entry": t["entry_price"],
                      "leader_entry": t.get("leader_entry"), "moved_pct_at_entry": t.get("moved_pct_at_entry"), "age_h_at_entry": t.get("age_h_at_entry"),
                      "consensus": t.get("consensus"), "pnl_usd": t.get("pnl_usd"), "opened_at": t.get("opened_at")} for t in open_],
            "skipped": sk[-25:][::-1],
            "skip_rule": {"judged": len(judged), "avg_outcome_pct": round(sum(e["outcome_pct"] for e in judged) / len(judged), 3) if judged else None,
                          "would_have_won_pct": round(100 * sum(e["outcome_pct"] > 0 for e in judged) / len(judged), 1) if judged else None},
            "config": config()}


def _why_not(by_leader: dict, open_: list[dict]) -> dict:
    """Plain-words account of why the leaders' current positions aren't (all) copied."""
    counts = {"total": 0, "copied": 0, "pre_tracking": 0, "too_late_age": 0, "too_late_price": 0, "already_failing": 0, "no_feed": 0, "book_full": 0, "other": 0}
    for rows in by_leader.values():
        for p in rows:
            counts["total"] += 1
            s = p["state"]
            if s == "copied":
                counts["copied"] += 1
            elif s == "held before tracking":
                counts["pre_tracking"] += 1
            elif "old" in s:
                counts["too_late_age"] += 1
            elif "in their favour" in s:
                counts["too_late_price"] += 1
            elif "against them" in s:
                counts["already_failing"] += 1
            elif "no price feed" in s:
                counts["no_feed"] += 1
            elif "book full" in s:
                counts["book_full"] += 1
            else:
                counts["other"] += 1
    parts = []
    if counts["pre_tracking"]:
        parts.append(f"{counts['pre_tracking']} were already open when we started watching that leader — a position is only copied when we see it open")
    if counts["too_late_age"]:
        parts.append(f"{counts['too_late_age']} were too old by the time we saw them (older than a quarter of that leader's typical hold)")
    if counts["too_late_price"]:
        parts.append(f"{counts['too_late_price']} had already run half of that leader's typical win")
    if counts["already_failing"]:
        parts.append(f"{counts['already_failing']} were already moving against the leader")
    if counts["no_feed"]:
        parts.append(f"{counts['no_feed']} are on pairs we have no price feed for")
    if counts["book_full"]:
        parts.append(f"{counts['book_full']} came when the book was full")
    if counts["other"]:
        parts.append(f"{counts['other']} were seen while the book was switched off")
    if counts["total"] == 0:
        text = "The leaders hold nothing at the moment — there is nothing to copy."
    elif counts["copied"] == counts["total"]:
        text = "Every position the leaders hold is copied."
    else:
        text = f"Of the {counts['total']} positions the leaders hold, {counts['copied']} are copied; " + "; ".join(parts) + "."
    text += " Copies happen the moment a leader opens a NEW position that passes the checks — the poll runs every 90 seconds."
    return {**counts, "text": text}


def config() -> dict:
    return {"leaders_n": LEADERS_N, "venues": list(VENUES), "leg_pct": LEG_PCT, "max_leverage": MAX_LEVERAGE, "max_open": MAX_OPEN, "disaster_pct": DISASTER_PCT,
            "late_age_frac": LATE_AGE_FRAC, "late_move_frac": LATE_MOVE_FRAC, "adverse_frac": ADVERSE_FRAC, "max_hold_x": MAX_HOLD_X, "consensus_x": CONSENSUS_X,
            "bar": {"min_copiers": MIN_COPIERS, "min_aum": MIN_AUM, "min_days": MIN_DAYS, "max_mdd": MAX_MDD, "min_win": MIN_WIN, "min_weeks_pos": MIN_WEEKS_POS,
                    "hl_equity": list(HL_EQUITY), "hl_month_roi": list(HL_MONTH_ROI), "hl_turnover": list(HL_TURNOVER)}}
