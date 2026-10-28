"""Live-trading settings. Keys are read from .env and never logged or sent anywhere but Binance."""

import hashlib
import os

from dotenv import dotenv_values

# Live settings are read from backend/.env itself, and that file wins over the process environment.
# A long-running dev server (uvicorn --reload) keeps the environment it started with, so values
# edited in .env later — a new key, say — would otherwise be silently ignored until a full restart.
# Tests set LIVE_ENV_FILE="" so only their pinned environment counts.
ENV_FILE = os.getenv("LIVE_ENV_FILE", os.path.join(os.path.dirname(__file__), "..", ".env"))


def _get(name: str, default: str = "") -> str:
    file_vals = dotenv_values(ENV_FILE) if ENV_FILE and os.path.exists(ENV_FILE) else {}
    v = file_vals.get(name)
    return v if v is not None else os.getenv(name, default)


ALLOWED = _get("LIVE_TRADING_ALLOWED", "false").lower() == "true"     # read at startup: restart to change
ENV = _get("BINANCE_ENV", "testnet").lower()                          # testnet | mainnet

URLS = {
    "testnet": {"rest": _get("BINANCE_TESTNET_REST", "https://testnet.binancefuture.com"),
                "ws": _get("BINANCE_TESTNET_WS", "wss://fstream.binancefuture.com")},
    "mainnet": {"rest": "https://fapi.binance.com", "ws": "wss://fstream.binance.com"},
}
REST = URLS.get(ENV, URLS["testnet"])["rest"]
WS = URLS.get(ENV, URLS["testnet"])["ws"]

SYSTEMS = ("main", "auto")          # the live order-flow scalper was retired on 2026-09-29


def keys(system: str) -> tuple[str, str] | None:
    """(api key, secret) for a system. Each system can have its own key (a Binance
    sub-account keeps positions apart); otherwise it falls back to the shared key.
    Testnet and mainnet keys are separate variables so one can't be used for the other."""
    env = "TESTNET_" if ENV == "testnet" else ""
    for prefix in (f"BINANCE_{env}{system.upper()}_", f"BINANCE_{env}"):
        # Read fresh on every call, so a key changed in .env is picked up without a restart.
        k, s = _get(prefix + "API_KEY").strip(), _get(prefix + "API_SECRET").strip()
        if k and s:
            return k, s
    return None


def account_id(system: str) -> str | None:
    """A short fingerprint of the key a system trades with — systems sharing it share an account."""
    k = keys(system)
    return hashlib.sha256(k[0].encode()).hexdigest()[:10] if k else None


# ── Limits (same as paper, plus a daily loss stop) ─────────────────────────────
DAILY_LOSS_PCT = float(_get("LIVE_DAILY_LOSS_PCT", "2.0"))        # of account equity, per system, per UTC day
MAX_LEVERAGE = int(_get("LIVE_MAX_LEVERAGE", _get("FUTURES_MAX_LEVERAGE", "20")))
RECV_WINDOW_MS = 5000
DB = os.getenv("LIVE_DB", os.path.join(os.path.dirname(__file__), "..", "data", "live.db"))
