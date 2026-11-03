"""Crypto Trading Oracle — FastAPI application entry point."""

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

# ── Validate required env vars before anything else imports ───────────────────

_REQUIRED = {"ANTHROPIC_API_KEY": "Get yours at https://console.anthropic.com/"}
_missing = {k: hint for k, hint in _REQUIRED.items() if not os.getenv(k)}
if _missing:
    for key, hint in _missing.items():
        print(f"ERROR: missing required env var {key!r}  ({hint})", file=sys.stderr)
    print("Add the above key(s) to backend/.env and restart.", file=sys.stderr)
    sys.exit(1)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import router
from api.scalper_routes import recorder_router
from api.positions_routes import router as positions_router
from api.live_routes import router as live_router
from api.trade_routes import router as trade_router
from api.news_routes import router as news_router
from api.books_routes import router as books_router
from api.markets_routes import router as markets_router
from api.guards_routes import router as guards_router
from api.ops_routes import router as ops_router
from data import cache
from scanner import scheduler

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
# httpx and twilio log every HTTP request/response at INFO — too noisy; only show warnings+
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("twilio").setLevel(logging.WARNING)
logging.getLogger("twilio.http_client").setLevel(logging.WARNING)

# Warnings and errors from the background loops also go to a rotating file (logs/app.log) — the console they
# print to is a terminal nobody reads, so a failing loop could go unnoticed for days (2026-10-27).
def _file_log() -> None:
    from logging.handlers import RotatingFileHandler
    if "pytest" in sys.modules:
        return                                       # the tests' simulated failures must not land in the real log
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "app.log")
    root = logging.getLogger()
    if any(getattr(h, "baseFilename", "") == os.path.abspath(path) for h in root.handlers):
        return                                       # this module is imported twice under uvicorn's reloader
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        h = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3)
        h.setLevel(logging.WARNING)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
    except OSError as e:
        logging.getLogger("oracle").warning("No file log (%s)", e)


_file_log()
logger = logging.getLogger("oracle")


async def _stop_bounded(name: str, coro, timeout: float) -> None:
    """Run a shutdown step with a deadline, so one stuck component can't keep the process alive."""
    try:
        await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        logger.warning("Shutdown: %s did not stop within %ss — abandoning it", name, timeout)
    except Exception as e:
        logger.warning("Shutdown: %s failed (%s)", name, e)


def _close_streams_on_exit() -> None:
    """Chain onto uvicorn's SIGINT/SIGTERM handlers so open SSE streams end on shutdown.

    Installed here rather than at import because uvicorn sets its own handlers before
    loading the app; wrapping them now keeps uvicorn's behaviour and adds ours first.
    """
    from scanner import bus

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)

        def handler(signum, frame, previous=previous):
            bus.close_all()
            if callable(previous):
                previous(signum, frame)

        try:
            signal.signal(sig, handler)
        except ValueError:   # not the main thread (e.g. under a test runner)
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Crypto Trading Oracle starting on port %s", os.getenv("PORT", 8000))
    # Warn about optional (but useful) API keys
    optional = {
        "CRYPTOPANIC_API_KEY": "crypto news sentiment",
        "FRED_API_KEY": "US macro data (rates, VIX, yield curve)",
        "TWITTER_BEARER_TOKEN": "social sentiment",
    }
    missing_opt = [f"{k} ({desc})" for k, desc in optional.items() if not os.getenv(k)]
    if missing_opt:
        logger.warning(
            "Optional API keys not set — some data sources will be skipped:\n  %s",
            "\n  ".join(missing_opt),
        )

    if await cache.get_client() is None:
        logger.warning("Redis unreachable — running without cache (every scan refetches)")
    else:
        logger.info("Redis cache connected at %s", os.getenv("REDIS_URL", "redis://localhost:6379"))

    if os.getenv("SCANNER_ENABLED", "true").lower() != "false":
        # Durable state now lives in SQLite; bring over anything still in Redis/JSON first.
        from storage import migrate
        await migrate.run()
        scheduler.start()
    else:
        logger.info("Scanner disabled via SCANNER_ENABLED=false")

    # The 1-second bar and liquidation recorder for the research labs (scalper/recorder.py).
    from scalper import recorder
    try:
        await recorder.start()
    except Exception as e:
        logger.error("Market recorder failed to start: %s", e)

    _close_streams_on_exit()
    yield

    # Bounded (2026-10-27): a reload once hung in this shutdown — the old worker never exited, uvicorn's reloader waited
    # on it, and the API was down for 15 minutes until the worker was killed. A step that doesn't finish is abandoned.
    await _stop_bounded("market recorder", recorder.stop(), 20)
    await _stop_bounded("scanner", scheduler.stop(), 20)
    await _stop_bounded("cache client", cache.close_client(), 5)
    logger.info("Oracle shutting down.")


app = FastAPI(
    title="Crypto Trading Oracle",
    description="AI-powered crypto trading signal engine combining TA, FA, macro, and on-chain data",
    version="1.0.0",
    lifespan=lifespan,
)

# The app can place real Binance orders and has no login, so only this machine's own UI may
# change anything. CORS alone doesn't stop another website from *sending* a simple POST (a
# kill switch, a close) to localhost — it only hides the reply — so unsafe requests carrying a
# foreign Origin are refused outright. Add origins (e.g. a LAN address) via ALLOWED_ORIGINS.
ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:5173"] + [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def refuse_foreign_writes(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": f"requests from {origin} are not allowed to change anything"}, status_code=403)
    return await call_next(request)


app.include_router(router, prefix="/api")
app.include_router(recorder_router, prefix="/api")
app.include_router(positions_router, prefix="/api")
app.include_router(live_router, prefix="/api")
app.include_router(trade_router, prefix="/api")
app.include_router(news_router, prefix="/api")
app.include_router(books_router, prefix="/api")
app.include_router(markets_router, prefix="/api")
app.include_router(guards_router, prefix="/api")
app.include_router(ops_router, prefix="/api")


@app.get("/")
async def root():
    return {"message": "Crypto Trading Oracle API", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok", "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY"))}


if __name__ == "__main__":
    import uvicorn
    # Local only by default: anyone who can reach this port can trade the connected Binance account.
    # The reloader is for development; in the container (APP_ENV=production) docker-entrypoint.sh runs one plain worker.
    uvicorn.run("main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", 8000)),
                reload=os.getenv("APP_ENV", "development") != "production")
