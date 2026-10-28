# Crypto Trading Oracle

AI-powered crypto trading signal engine combining live Technical Analysis, Fundamental Analysis, macro data, on-chain metrics, and news sentiment — synthesized by Claude into precise trade recommendations with entry, stop loss, take profit, and hold duration.

## Architecture

```
Data Sources          Analysis Layer        Oracle Brain         Output
──────────────────    ──────────────────    ────────────────    ──────────────────
Binance API           TA Engine             Claude Opus 5.5     Trade Recommendation
CoinGecko          →  (RSI/MACD/BB/        (synthesizes all  → Entry / SL / TP
CryptoPanic           ADX/ATR/EMA)          signals into a      Direction / TF
NewsAPI               Sentiment Scoring     structured call)    Hold Duration
FRED (macro)          Macro Scoring                             Confidence %
Etherscan          →  Signal Package   →
mempool.space         (composite -1/+1)
Twitter/X
Fear & Greed
```

## Quick Start

### 1. Add API keys

```bash
cd backend
cp .env.example .env
# Edit .env — minimum required: ANTHROPIC_API_KEY
```

### 2. Start everything

```bash
./start.sh
```

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000
- Swagger docs: http://localhost:8000/docs

### 3. Manual setup

**Backend:**
```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python main.py
```

**Frontend:**
```bash
cd frontend
npm install && npm run dev
```

## API Keys (all free tiers available)

| Service | Key | Purpose |
|---------|-----|---------|
| Anthropic | `ANTHROPIC_API_KEY` | **Required** — Claude oracle brain |
| CryptoPanic | `CRYPTOPANIC_API_KEY` | Crypto news sentiment |
| NewsAPI | `NEWS_API_KEY` | Macro/geopolitical news |
| FRED | `FRED_API_KEY` | US macro data (rates, VIX) |
| Etherscan | `ETHERSCAN_API_KEY` | ETH on-chain data |
| Twitter | `TWITTER_BEARER_TOKEN` | Social sentiment |
| CoinGecko | `COINGECKO_API_KEY` | Optional (free tier works without) |

Binance, mempool.space, Fear & Greed Index, and CoinGecko base data require **no API keys**.

## Features

- Multi-timeframe TA: RSI, MACD, Bollinger Bands, EMA cloud, ADX, ATR, Stochastic, OBV, Support/Resistance
- News sentiment: CryptoPanic + NewsAPI (crypto, macro, geopolitical)
- Macro environment: Fed funds rate, VIX, yield curve, DXY, M2 supply
- On-chain: BTC mempool/hashrate, ETH gas/supply
- Social: Twitter/X sentiment analysis
- Fear & Greed index
- Claude oracle: synthesizes all signals → structured JSON trade call
- Paper trading: open/close simulated trades, track P&L, win rate, profit factor
- Auto-execute: oracle runs and opens a paper trade if confidence ≥ 60%

## Supported Pairs

BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, ADAUSDT (easily extendable)

## Node.js requirement

Node v20+ required. Use nvm: `nvm install --lts && nvm use --lts`
