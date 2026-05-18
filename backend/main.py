"""Crypto Trading Oracle — FastAPI application entry point."""

import logging
import os
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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router

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
logger = logging.getLogger("oracle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Crypto Trading Oracle starting on port %s", os.getenv("PORT", 8000))
    # Warn about optional (but useful) API keys
    optional = {
        "CRYPTOPANIC_API_KEY": "crypto news sentiment",
        "NEWS_API_KEY": "macro/geo news",
        "FRED_API_KEY": "US macro data (rates, VIX, yield curve)",
        "TWITTER_BEARER_TOKEN": "social sentiment",
        "ETHERSCAN_API_KEY": "ETH on-chain data",
    }
    missing_opt = [f"{k} ({desc})" for k, desc in optional.items() if not os.getenv(k)]
    if missing_opt:
        logger.warning(
            "Optional API keys not set — some data sources will be skipped:\n  %s",
            "\n  ".join(missing_opt),
        )
    yield
    logger.info("Oracle shutting down.")


app = FastAPI(
    title="Crypto Trading Oracle",
    description="AI-powered crypto trading signal engine combining TA, FA, macro, and on-chain data",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/")
async def root():
    return {"message": "Crypto Trading Oracle API", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok", "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY"))}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
