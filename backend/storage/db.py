"""SQLite plumbing shared by the swing system's databases.

  state.db          — durable coordination state (storage/state.py)
  swing_trading.db  — both swing wallets: trades, orders, shadows, equity (trading/paper_trader.py)
  signals.db        — oracle signal history, fired scanner signals, watchlist changes (storage/signals.py)

The scalper keeps its own two databases (scalper/store.py).
"""

import os
import sqlite3

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
STATE_DB = os.getenv("STATE_DB", os.path.join(DATA_DIR, "state.db"))
SWING_DB = os.getenv("SWING_DB", os.path.join(DATA_DIR, "swing_trading.db"))
SIGNALS_DB = os.getenv("SIGNALS_DB", os.path.join(DATA_DIR, "signals.db"))


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=10, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con
