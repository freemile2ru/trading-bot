"""The scalper's own databases (SQLite, WAL) — separate from the swing system entirely.

  scalper_trades.db  — signals (with features + forward-return labels), orders (queue,
                       time-to-fill, adverse selection), trades (MFE/MAE, fees, funding,
                       R), equity snapshots, health snapshots, engine events, state.
  scalper_market.db  — 1-second bars per symbol (mid OHLC, spread, depth imbalance,
                       aggressor volumes, trades, VWAP) for research and replay.

All writes go through one background thread in batches so the event loop never blocks
on disk. Reads open their own connection (WAL allows concurrent readers).
"""

import json
import logging
import os
import queue
import sqlite3
import threading
import time

from scalper import config as C

logger = logging.getLogger("scalper.store")

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    decision TEXT NOT NULL,            -- entered | skipped | blocked
    reason TEXT,
    mid REAL, spread_bps REAL, median_spread_bps REAL, obi REAL,
    flow_10s REAL, flow_30s REAL, flow_z REAL, atr_bps REAL, ema9 REAL, ema21 REAL,
    vwap_dev_atr REAL, rv_60s_bps REAL, micro_skew_bps REAL, trades_per_s REAL,
    fwd_5 REAL, fwd_15 REAL, fwd_30 REAL, fwd_60 REAL, fwd_120 REAL   -- signed bps, + = in the signal's favour
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS ix_signals_symbol ON signals(symbol, ts);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, symbol TEXT, side TEXT, purpose TEXT,     -- entry | tp
    price REAL, qty REAL, placed_ts REAL, ttl REAL,
    queue_ahead_at_place REAL, mid_at_place REAL, spread_at_place REAL,
    status TEXT,                        -- pending | filled | cancelled
    fill_ts REAL, fill_price REAL, time_to_fill REAL, cancel_reason TEXT,
    signal_id INTEGER, position_id TEXT,
    adv_1 REAL, adv_5 REAL, adv_30 REAL  -- signed bps mid move after the fill, + = favourable
);
CREATE INDEX IF NOT EXISTS ix_orders_ts ON orders(placed_ts);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY, symbol TEXT, side TEXT,
    entry_ts REAL, exit_ts REAL, hold_s REAL,
    entry_price REAL, exit_price REAL, exit_trigger REAL, qty REAL, notional REAL,
    leverage INTEGER, margin REAL, liq_price REAL,
    tp REAL, sl REAL, tp_bps REAL, sl_bps REAL,
    exit_reason TEXT, entry_maker INTEGER, exit_maker INTEGER,
    gross_pnl REAL, fees REAL, funding REAL, net_pnl REAL, net_bps REAL, risk_usd REAL, r REAL,
    mfe_bps REAL, mae_bps REAL, spread_entry_bps REAL, spread_exit_bps REAL,
    time_to_fill REAL, queue_ahead REAL, signal_id INTEGER, features TEXT,
    entry_fee REAL, exit_fee REAL, slippage_usd REAL, cost_bps REAL
);
CREATE INDEX IF NOT EXISTS ix_trades_exit ON trades(exit_ts);

CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, balance REAL, equity REAL,
    open_pnl REAL, margin_used REAL, positions INTEGER, day_pnl REAL);
CREATE TABLE IF NOT EXISTS health (ts REAL PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""

MARKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1s (
    symbol TEXT NOT NULL, ts INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, spread_bps REAL, obi REAL,
    buy_vol REAL, sell_vol REAL, trades INTEGER, vwap REAL,
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;

-- Every forced liquidation on Binance USDⓈ-M, all symbols (from !forceOrder@arr). Binance
-- sends at most the latest one per symbol per second, so bursts are undercounted — fine as
-- a burst indicator, not as a volume total. side = the liquidation order's side: SELL means
-- a long was liquidated, BUY a short.
CREATE TABLE IF NOT EXISTS liquidations (
    ts REAL NOT NULL, symbol TEXT NOT NULL, side TEXT, price REAL, avg_price REAL, qty REAL, notional REAL
);
CREATE INDEX IF NOT EXISTS ix_liq ON liquidations(symbol, ts);
"""


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=10, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


class Store:
    def __init__(self, trades_path: str = C.TRADES_DB, market_path: str = C.MARKET_DB,
                 horizons: tuple[int, ...] = C.FORWARD_HORIZONS):
        self.trades_path, self.market_path = trades_path, market_path
        self.horizons = horizons          # forward-return labels this store keeps (fwd_<h> columns)
        for path, schema in ((trades_path, TRADES_SCHEMA), (market_path, MARKET_SCHEMA)):
            con = _connect(path)
            con.executescript(schema)
            if path == trades_path:
                # Columns added after the first release — upgrade existing databases in place.
                have = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
                for col in ("entry_fee", "exit_fee", "slippage_usd", "cost_bps"):
                    if col not in have:
                        con.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
                if "mode" not in have:          # "live" for real Binance trades; NULL/"paper" otherwise
                    con.execute("ALTER TABLE trades ADD COLUMN mode TEXT")
                # Longer labels and the move that triggered an event (the retired impulse scalper's signals).
                sig = {r[1] for r in con.execute("PRAGMA table_info(signals)")}
                for col in [f"fwd_{h}" for h in horizons] + ["move_bps", "move_z", "vol_mult", "window_s"]:
                    if col not in sig:
                        con.execute(f"ALTER TABLE signals ADD COLUMN {col} REAL")
                for table in ("equity", "orders"):
                    if "mode" not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN mode TEXT")
            con.commit()
            con.close()
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._writer, name="scalper-store", daemon=True)
        self._thread.start()
        self.write_errors = 0
        self.mode = "paper"           # set by the engine: tags every trade/order/equity row it writes

    # ── Writer thread ─────────────────────────────────────────────────────────

    def _writer(self) -> None:
        cons = {"trades": _connect(self.trades_path), "market": _connect(self.market_path)}
        while not self._stop.is_set() or not self._q.empty():
            batch = []
            try:
                batch.append(self._q.get(timeout=0.5))
                while len(batch) < 2000:
                    batch.append(self._q.get_nowait())
            except queue.Empty:
                pass
            if not batch:
                continue
            touched = set()
            for db, sql, params, many, done in batch:
                try:
                    cur = cons[db].executemany(sql, params) if many else cons[db].execute(sql, params)
                    touched.add(db)
                    if done is not None:
                        done["id"] = cur.lastrowid
                        done["event"].set()
                except sqlite3.Error as e:
                    self.write_errors += 1
                    logger.warning("Scalper store write failed: %s — %s", e, sql[:80])
                    if done is not None:
                        done["event"].set()
            for db in touched:
                cons[db].commit()
        for con in cons.values():
            con.close()

    def _put(self, db: str, sql: str, params, many: bool = False) -> None:
        self._q.put((db, sql, params, many, None))

    def insert_returning_id(self, db: str, sql: str, params) -> int | None:
        """Blocking insert that returns the rowid — call via asyncio.to_thread."""
        done = {"event": threading.Event(), "id": None}
        self._q.put((db, sql, params, False, done))
        done["event"].wait(5)
        return done["id"]

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    # ── Writes ────────────────────────────────────────────────────────────────

    SIGNAL_COLS = ("ts", "symbol", "side", "decision", "reason", "mid", "spread_bps", "median_spread_bps",
                   "obi", "flow_10s", "flow_30s", "flow_z", "atr_bps", "ema9", "ema21", "vwap_dev_atr",
                   "rv_60s_bps", "micro_skew_bps", "trades_per_s", "move_bps", "move_z", "vol_mult", "window_s")

    def add_signal(self, row: dict) -> int | None:
        cols = self.SIGNAL_COLS
        sql = f"INSERT INTO signals ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        return self.insert_returning_id("trades", sql, [row.get(c) for c in cols])

    def label_signal(self, signal_id: int, horizon: int, value: float) -> None:
        if horizon in self.horizons:
            self._put("trades", f"UPDATE signals SET fwd_{horizon}=? WHERE id=?", (value, signal_id))

    def upsert_order(self, o: dict) -> None:
        cols = ("id", "symbol", "side", "purpose", "price", "qty", "placed_ts", "ttl", "queue_ahead_at_place",
                "mid_at_place", "spread_at_place", "status", "fill_ts", "fill_price", "time_to_fill",
                "cancel_reason", "signal_id", "position_id", "mode")
        sql = (f"INSERT INTO orders ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
               f"ON CONFLICT(id) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in cols[1:]))
        self._put("trades", sql, [o.get(c) if c != "mode" else o.get("mode") or self.mode for c in cols])

    def cancel_stale_orders(self, keep: set[str]) -> None:
        """At startup: orders still 'pending' in the DB weren't carried over in memory."""
        placeholders = ",".join("?" * len(keep)) or "''"
        self._put("trades",
                  f"UPDATE orders SET status='cancelled', cancel_reason='engine restart' "
                  f"WHERE status='pending' AND id NOT IN ({placeholders})", tuple(keep))

    def label_adverse(self, order_id: str, horizon: int, value: float) -> None:
        if horizon in C.ADVERSE_HORIZONS:
            self._put("trades", f"UPDATE orders SET adv_{horizon}=? WHERE id=?", (value, order_id))

    TRADE_COLS = ("id", "symbol", "side", "entry_ts", "exit_ts", "hold_s", "entry_price", "exit_price",
                  "exit_trigger", "qty", "notional", "leverage", "margin", "liq_price", "tp", "sl", "tp_bps",
                  "sl_bps", "exit_reason", "entry_maker", "exit_maker", "gross_pnl", "fees", "funding",
                  "net_pnl", "net_bps", "risk_usd", "r", "mfe_bps", "mae_bps", "spread_entry_bps",
                  "spread_exit_bps", "time_to_fill", "queue_ahead", "signal_id", "features",
                  "entry_fee", "exit_fee", "slippage_usd", "cost_bps", "mode")

    def add_trade(self, t: dict) -> None:
        row = dict(t)
        row["features"] = json.dumps(row.get("features") or {})
        row.setdefault("mode", self.mode)
        cols = self.TRADE_COLS
        self._put("trades", f"INSERT OR REPLACE INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  [row.get(c) for c in cols])

    def add_equity(self, ts: float, balance: float, equity: float, open_pnl: float,
                   margin_used: float, positions: int, day_pnl: float, mode: str = "paper") -> None:
        self._put("trades", "INSERT OR REPLACE INTO equity (ts, balance, equity, open_pnl, margin_used, positions, "
                  "day_pnl, mode) VALUES (?,?,?,?,?,?,?,?)",
                  (ts, balance, equity, open_pnl, margin_used, positions, day_pnl, mode))

    def add_health(self, ts: float, data: dict) -> None:
        self._put("trades", "INSERT OR REPLACE INTO health VALUES (?,?)", (ts, json.dumps(data)))

    def add_event(self, kind: str, data: dict) -> None:
        self._put("trades", "INSERT INTO events (ts, kind, data) VALUES (?,?,?)",
                  (time.time(), kind, json.dumps(data, default=str)))

    def set_state(self, key: str, value) -> None:
        self._put("trades", "INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def add_bars(self, rows: list[tuple]) -> None:
        if rows:
            self._put("market", "INSERT OR REPLACE INTO bars_1s VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows, many=True)

    def add_liquidation(self, row: tuple) -> None:
        self._put("market", "INSERT INTO liquidations VALUES (?,?,?,?,?,?,?)", row)

    def prune_market(self) -> None:
        cutoff = int(time.time()) - C.MARKET_RETENTION_DAYS * 86400
        self._put("market", "DELETE FROM bars_1s WHERE ts < ?", (cutoff,))
        self._put("market", "DELETE FROM liquidations WHERE ts < ?", (cutoff,))

    # ── Reads (own connection; call via asyncio.to_thread) ────────────────────

    def query(self, db: str, sql: str, params=()) -> list[dict]:
        con = sqlite3.connect(self.trades_path if db == "trades" else self.market_path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def get_state(self, key: str, default=None):
        rows = self.query("trades", "SELECT value FROM state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def sizes(self) -> dict:
        return {name: (os.path.getsize(p) if os.path.exists(p) else 0)
                for name, p in (("trades_db", self.trades_path), ("market_db", self.market_path))}
