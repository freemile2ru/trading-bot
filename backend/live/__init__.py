"""Live trading on Binance USDⓈ-M futures — built, OFF by default.

Nothing here sends an order unless BOTH are true:
  1. LIVE_TRADING_ALLOWED=true in .env (the master switch; remove it to block everything)
  2. that system's mode is "live" (a per-system toggle in the UI, stored in state.db)

  config.py    environment, keys, limits
  binance.py   signed REST client + user-data stream (testnet by default)
  control.py   modes, pre-trade guards, audit log (data/live.db), kill switch
  swing.py     live ledgers for the main wallet and the autotrader (Binance is the source of truth)
  scalper.py   live order book for the scalper (fills come from Binance, not the queue model)
  selfcheck.py one-off plumbing test against the testnet — run it before switching anything on
"""
