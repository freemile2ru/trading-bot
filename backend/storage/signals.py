"""signals.db — the swing system's research record.

  oracle_signals    — every Claude oracle result (Signal tab, scanner escalations), no cap.
                      Replaces data/signal_history.json, which silently kept only the last 100.
  fired_signals     — every signal the scanner fired, permanently. The All Signals feed reads
                      the last 24h from here (it used to be a 24h Redis key, so older ones vanished).
  watchlist_events  — every watchlist add/remove with its reason, for churn analysis.
  scan_features     — every full-sweep scan: its scores plus the raw fundamentals inputs
                      (price, positioning, usage), for research/swing_lab.py. ~3k rows/day.
"""

import json
import threading
import time

from storage.db import SIGNALS_DB, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS oracle_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, symbol TEXT, source TEXT, price REAL, action TEXT, direction TEXT,
    confidence INTEGER, timeframe TEXT, entry_price REAL, stop_loss REAL, take_profit_1 REAL,
    composite_score REAL, elapsed_seconds REAL, sources_ok INTEGER, sources_total INTEGER,
    primary_thesis TEXT
);
CREATE INDEX IF NOT EXISTS ix_oracle_symbol ON oracle_signals(symbol, ts);

CREATE TABLE IF NOT EXISTS fired_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at REAL NOT NULL, symbol TEXT NOT NULL, action TEXT, confidence INTEGER,
    conviction REAL, entry_price REAL, stop_loss REAL, take_profit_1 REAL, take_profit_2 REAL,
    timeframe TEXT, data TEXT NOT NULL,
    UNIQUE (symbol, fired_at)
);
CREATE INDEX IF NOT EXISTS ix_fired_at ON fired_signals(fired_at);

CREATE TABLE IF NOT EXISTS watchlist_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, conviction REAL
);
CREATE INDEX IF NOT EXISTS ix_wl_ts ON watchlist_events(ts);

CREATE TABLE IF NOT EXISTS scan_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, symbol TEXT NOT NULL, price REAL, conviction REAL, composite REAL,
    ta_score REAL, fa_score REAL, fa_components TEXT, features TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_scan_features ON scan_features(symbol, ts);

-- Why each closed trade won or lost (trading/debrief.py): automatic tags, then Claude's review.
CREATE TABLE IF NOT EXISTS trade_debriefs (
    system TEXT NOT NULL, trade_id TEXT NOT NULL, symbol TEXT, closed_ts REAL,
    primary_tag TEXT, tags TEXT, metrics TEXT, claude TEXT, created_at REAL, claude_at REAL,
    PRIMARY KEY (system, trade_id)
);
CREATE INDEX IF NOT EXISTS ix_debriefs_closed ON trade_debriefs(closed_ts);

-- What price did after each scan reading (perpetual hourly closes), for signal scoring.
CREATE TABLE IF NOT EXISTS scan_labels (
    scan_id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, ts REAL NOT NULL, p0 REAL,
    fwd_4h REAL, fwd_24h REAL, fwd_72h REAL,             -- % move of the coin
    xs_4h REAL, xs_24h REAL, xs_72h REAL,                -- % move minus BTC's over the same span
    labelled_at REAL
);
"""

ORACLE_COLS = ("ts", "symbol", "source", "price", "action", "direction", "confidence", "timeframe",
               "entry_price", "stop_loss", "take_profit_1", "composite_score", "elapsed_seconds",
               "sources_ok", "sources_total", "primary_thesis")

_lock = threading.Lock()
_con = None
_path = SIGNALS_DB


def _db():
    global _con
    if _con is None:
        _con = connect(_path)
        _con.executescript(SCHEMA)
        _con.commit()
    return _con


def use(path: str) -> None:
    global _con, _path
    with _lock:
        if _con is not None:
            _con.close()
        _con, _path = None, path


def _rows(sql: str, params=()) -> list[dict]:
    with _lock:
        cur = _db().execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── Oracle signal history ──────────────────────────────────────────────────────

def add_oracle_signal(entry: dict) -> None:
    with _lock:
        con = _db()
        con.execute(f"INSERT INTO oracle_signals ({','.join(ORACLE_COLS)}) VALUES ({','.join('?' * len(ORACLE_COLS))})",
                    [entry.get(c) for c in ORACLE_COLS])
        con.commit()


def oracle_count() -> int:
    return _rows("SELECT COUNT(*) AS n FROM oracle_signals")[0]["n"]


# ── Fired scanner signals ──────────────────────────────────────────────────────

def add_fired(signal: dict) -> None:
    with _lock:
        con = _db()
        con.execute(
            "INSERT OR IGNORE INTO fired_signals (fired_at, symbol, action, confidence, conviction, entry_price, "
            "stop_loss, take_profit_1, take_profit_2, timeframe, data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (signal.get("fired_at"), signal.get("symbol"), signal.get("action"), signal.get("confidence"),
             signal.get("conviction"), signal.get("entry_price"), signal.get("stop_loss"),
             signal.get("take_profit_1"), signal.get("take_profit_2"), signal.get("timeframe"),
             json.dumps(signal, default=str)),
        )
        con.commit()


def recent_fired(seconds: float = 86400, limit: int = 20) -> list[dict]:
    rows = _rows("SELECT data FROM fired_signals WHERE fired_at >= ? ORDER BY fired_at DESC LIMIT ?",
                 (time.time() - seconds, limit))
    return [json.loads(r["data"]) for r in rows]


def fired_tally() -> dict[str, dict]:
    """How often each coin has fired a signal: last 24h, last 7 days, all time, by direction."""
    now = time.time()
    rows = _rows("""
        SELECT symbol,
               SUM(fired_at >= ?) AS d1, SUM(fired_at >= ?) AS d7, COUNT(*) AS total,
               SUM(action = 'BUY') AS buys, SUM(action = 'SELL') AS sells, MAX(fired_at) AS last
        FROM fired_signals GROUP BY symbol""", (now - 86400, now - 7 * 86400))
    return {r["symbol"]: {k: r[k] for k in ("d1", "d7", "total", "buys", "sells", "last")} for r in rows}


# ── Watchlist changes ──────────────────────────────────────────────────────────

def log_watchlist(action: str, symbol: str, reason: str | None = None, conviction: float | None = None) -> None:
    with _lock:
        con = _db()
        con.execute("INSERT INTO watchlist_events (ts, symbol, action, reason, conviction) VALUES (?,?,?,?,?)",
                    (time.time(), symbol, action, reason, conviction))
        con.commit()


# ── Scan features (research) ───────────────────────────────────────────────────

def _news_features(scan: dict) -> dict:
    n = ((scan.get("signal_package") or {}).get("news_summary")) or {}
    if n.get("score") is None:
        return {}
    return {"news_score": n["score"], "news_burst": n.get("burst"), "news_stories_6h": n.get("stories_6h")}


def add_scan_features(scans: list[dict]) -> None:
    rows = []
    for s in scans:
        fa = s.get("fa") or {}
        rows.append((s.get("scanned_at") or time.time(), s["symbol"], s.get("price"), s.get("conviction"),
                     s.get("composite_score"), s.get("ta_score"), fa.get("score"),
                     json.dumps({k: v.get("score") for k, v in (fa.get("components") or {}).items()}),
                     json.dumps({**(fa.get("features") or {}), "confidence": s.get("confidence"),
                                 "setups": (s.get("conviction_detail") or {}).get("aligned_setups") or [],
                                 "regime": (s.get("regime_at_scan") or {}).get("label"),
                                 "btc_trend": (s.get("regime_at_scan") or {}).get("btc_trend"),
                                 # Scraped-news readings, so signal scoring measures them like any input.
                                 **_news_features(s)})))
    with _lock:
        con = _db()
        con.executemany("INSERT INTO scan_features (ts, symbol, price, conviction, composite, ta_score, fa_score, "
                        "fa_components, features) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.commit()


def latest_convictions(symbol: str, since: float, n: int = 2) -> list[tuple[float, float]]:
    """A coin's last n logged convictions at or after `since`, newest first: (ts, conviction)."""
    rows = _rows("SELECT ts, conviction FROM scan_features WHERE symbol=? AND ts>=? ORDER BY ts DESC LIMIT ?", (symbol, since, n))
    return [(r["ts"], r["conviction"]) for r in rows]


def scan_features(since: float = 0) -> list[dict]:
    rows = _rows("SELECT * FROM scan_features WHERE ts >= ? ORDER BY ts", (since,))
    for r in rows:
        r["fa_components"] = json.loads(r["fa_components"] or "{}")
        r["features"] = json.loads(r["features"])
    return rows


# ── Scan labels (signal scoring) ───────────────────────────────────────────────

def unlabelled_scans(ready_before: float, limit: int = 20000) -> list[dict]:
    """Scans whose shortest horizon has passed but whose labels aren't complete yet."""
    return _rows("""
        SELECT f.id, f.ts, f.symbol FROM scan_features f LEFT JOIN scan_labels l ON l.scan_id = f.id
        WHERE f.ts <= ? AND (l.scan_id IS NULL OR l.fwd_72h IS NULL) ORDER BY f.ts LIMIT ?""", (ready_before, limit))


def save_labels(rows: list[tuple]) -> None:
    """rows: (scan_id, symbol, ts, p0, fwd_4h, fwd_24h, fwd_72h, xs_4h, xs_24h, xs_72h)."""
    if not rows:
        return
    now = time.time()
    with _lock:
        con = _db()
        con.executemany("INSERT OR REPLACE INTO scan_labels VALUES (?,?,?,?,?,?,?,?,?,?,?)", [(*r, now) for r in rows])
        con.commit()


def labelled_scans(since: float = 0) -> list[dict]:
    rows = _rows("""
        SELECT f.id, f.ts, f.symbol, f.conviction, f.composite, f.ta_score, f.fa_score, f.features,
               l.fwd_4h, l.fwd_24h, l.fwd_72h, l.xs_4h, l.xs_24h, l.xs_72h
        FROM scan_features f JOIN scan_labels l ON l.scan_id = f.id WHERE f.ts >= ? ORDER BY f.ts""", (since,))
    for r in rows:
        r["features"] = json.loads(r["features"] or "{}")
    return rows


# ── Trade debriefs ─────────────────────────────────────────────────────────────

def debriefed_keys() -> set[tuple[str, str]]:
    return {(r["system"], r["trade_id"]) for r in _rows("SELECT system, trade_id FROM trade_debriefs")}


def save_debrief(system: str, trade_id: str, symbol: str, closed_ts: float, primary: str, tags: list, metrics: dict) -> None:
    with _lock:
        con = _db()
        con.execute("INSERT OR REPLACE INTO trade_debriefs (system, trade_id, symbol, closed_ts, primary_tag, tags, metrics, "
                    "claude, created_at) VALUES (?,?,?,?,?,?,?,NULL,?)",
                    (system, trade_id, symbol, closed_ts, primary, json.dumps(tags), json.dumps(metrics, default=str), time.time()))
        con.commit()


def save_claude_debrief(system: str, trade_id: str, claude: dict) -> None:
    with _lock:
        con = _db()
        con.execute("UPDATE trade_debriefs SET claude=?, claude_at=? WHERE system=? AND trade_id=?",
                    (json.dumps(claude), time.time(), system, trade_id))
        con.commit()


def _debrief_row(r: dict) -> dict:
    return {"system": r["system"], "trade_id": r["trade_id"], "symbol": r["symbol"], "closed_ts": r["closed_ts"],
            "primary": r["primary_tag"], "tags": json.loads(r["tags"] or "[]"), "metrics": json.loads(r["metrics"] or "{}"),
            "claude": json.loads(r["claude"]) if r["claude"] else None, "created_at": r["created_at"]}


def debriefs(system: str | None = None, limit: int = 200) -> list[dict]:
    q = "SELECT * FROM trade_debriefs" + (" WHERE system=?" if system else "") + " ORDER BY closed_ts DESC LIMIT ?"
    return [_debrief_row(r) for r in _rows(q, ((system, limit) if system else (limit,)))]


def debrief(system: str, trade_id: str) -> dict | None:
    rows = _rows("SELECT * FROM trade_debriefs WHERE system=? AND trade_id=?", (system, trade_id))
    return _debrief_row(rows[0]) if rows else None


def delete_debriefs(system: str, trade_ids: list[str]) -> int:
    """Drop the debriefs of erased trades, so nothing is learned from positions that are no longer on the record."""
    if not trade_ids:
        return 0
    with _lock:
        con = _db()
        n = con.execute(f"DELETE FROM trade_debriefs WHERE system=? AND trade_id IN ({','.join('?' * len(trade_ids))})",
                        (system, *trade_ids)).rowcount
        con.commit()
    return n


def debriefs_pending_claude(limit: int = 500) -> list[dict]:
    return [_debrief_row(r) for r in _rows("SELECT * FROM trade_debriefs WHERE claude IS NULL ORDER BY closed_ts DESC LIMIT ?", (limit,))]
