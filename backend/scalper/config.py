"""Short-term engine configuration (scalper/) — now only the 1-second market recorder.

Two scalpers ran on this engine and both were retired for having no edge after costs: the
order-flow scalper (2026-09-29) and the impulse scalper (2026-10-06) — see the Retired tab.
Their trade records stay on disk (paths below) and on the Trades page.
"""

import os


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


# Highest-volume USDⓈ-M crypto perpetuals with spreads ≲1.4 bps (Sep 2026). Excludes the
# non-crypto contracts (CL, SOXL, XAU) and wide-spread books (NEAR, WLD) — the spread is
# paid on every market exit.
SYMBOLS = [s.strip().upper() for s in os.getenv(
    "SCALPER_SYMBOLS",
    "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ZECUSDT,ENAUSDT,SUIUSDT,TAOUSDT,HYPEUSDT,UNIUSDT,LINKUSDT",
).split(",") if s.strip()]

# Binance splits futures streams: order book on /public, trades & mark price on /market.
WS_PUBLIC = "wss://fstream.binance.com/public/stream"
WS_MARKET = "wss://fstream.binance.com/market/stream"

INITIAL_BALANCE = _f("SCALPER_BALANCE", 10_000)

# ── Signal ────────────────────────────────────────────────────────────────────
EVAL_INTERVAL = _f("SCALPER_EVAL_INTERVAL", 0.5)        # seconds between strategy evaluations
WARMUP_SECONDS = _i("SCALPER_WARMUP", 600)              # need this much 1s history for z-scores

# ── Execution ─────────────────────────────────────────────────────────────────
ENTRY_TTL = _f("SCALPER_ENTRY_TTL", 8.0)                # seconds a post-only entry may rest
# Non-urgent exits (time stop, flow reversal, soft stop) first rest at the touch as maker:
# 0.02% instead of 0.05% + slippage, and the spread is earned instead of paid. The hard
# stop never waits — it is always a market exit.
PASSIVE_EXITS = os.getenv("SCALPER_PASSIVE_EXITS", "true").lower() != "false"
PASSIVE_EXIT_TTL = _f("SCALPER_PASSIVE_EXIT_TTL", 15.0)  # then fall back to a market exit
# A resting exit that price has moved away from is re-placed at the new touch (keeping the
# original deadline), so it stays first in line instead of being stranded.
PASSIVE_EXIT_REPEG = os.getenv("SCALPER_PASSIVE_EXIT_REPEG", "true").lower() != "false"

# ── Risk ──────────────────────────────────────────────────────────────────────
RISK_PCT = _f("SCALPER_RISK_PCT", 0.25)                 # % of equity lost at the stop
# The scalper's own leverage ceiling (the swing wallets keep FUTURES_MAX_LEVERAGE). Sizing is
# by risk, so this changes margin locked and distance to liquidation — not the loss at the stop.
MAX_LEVERAGE = _i("SCALPER_MAX_LEVERAGE", 20)
# Loosened for paper data collection: 6 × 0.25% = at most 1.5% of equity at risk at once.
MAX_POSITIONS = _i("SCALPER_MAX_POSITIONS", 6)
MAX_TRADES_PER_HOUR = _i("SCALPER_MAX_TRADES_PER_HOUR", 60)
DAILY_LOSS_LIMIT_PCT = _f("SCALPER_DAILY_LOSS_LIMIT", 10.0)  # halt new entries for the UTC day — raised for paper data collection; bring back to ~2% before real money
COOLDOWN_AFTER_WIN = _f("SCALPER_COOLDOWN_WIN", 30.0)
COOLDOWN_AFTER_LOSS = _f("SCALPER_COOLDOWN_LOSS", 60.0)
STALE_FEED_SECONDS = _f("SCALPER_STALE_FEED", 5.0)      # no new entries past this
FLATTEN_STALE_SECONDS = _f("SCALPER_FLATTEN_STALE", 15.0)    # close positions past this

# ── Research labelling ────────────────────────────────────────────────────────
FORWARD_HORIZONS = (5, 15, 30, 60, 120)                 # seconds: mid return after each signal
ADVERSE_HORIZONS = (1, 5, 30)                           # seconds: mid move after each fill

# ── Storage ───────────────────────────────────────────────────────────────────
_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
TRADES_DB = os.getenv("SCALPER_TRADES_DB", os.path.join(_DATA, "scalper_trades.db"))
MARKET_DB = os.getenv("SCALPER_MARKET_DB", os.path.join(_DATA, "scalper_market.db"))
# The retired impulse scalper's record (trades and the extreme moves it logged).
IMPULSE_TRADES_DB = os.getenv("IMPULSE_TRADES_DB", os.path.join(_DATA, "impulse_trades.db"))
IMPULSE_BALANCE = 10_000.0
# The market recorder's own bookkeeping (feed health, events); its bars go to MARKET_DB.
RECORDER_DB = os.getenv("RECORDER_DB", os.path.join(_DATA, "recorder.db"))
# 1s bars run ~80 MB/day for 12 coins. 30 days keeps enough for the rare-event studies in
# research/scalper_lab.py (strong candles, liquidation bursts need weeks to count).
MARKET_RETENTION_DAYS = _i("SCALPER_MARKET_RETENTION_DAYS", 30)
