# Retired code (archive — not used by the app)

Kept for reference only. Nothing imports these files. Why each experiment was retired, with the
data, is on the app's **Retired** tab (backend/experiments/retired.py).

- `scalper_strategy.py` — the order-flow scalper's entry/exit rules (30s trade-flow z-score,
  order-book imbalance, EMA filter). An early version recovered from git after the module was
  removed on 2026-09-29; the final version had wider stop/target floors and passive exits.
- `straddle_lab.py` — the hedge-straddle backtest (long + short at once, cut the loser). An
  earlier revision of the script that produced the retired result.
- `impulse.py` — the impulse scalper (retired 2026-10-06): extreme-move detection (60 s σ-move on
  heavy volume, or a 150 bps 5-minute move), taker entries, a stop sized from the move and trailed.
  Final version, as it ran.
- `test_impulse.py` — its tests (they import `scalper.impulse`, so they no longer run).
- `scalper_analytics.py` — the scalper dashboards' summary, research and trade-page queries, used by
  the removed /api/impulse routes.

Not recoverable (never committed): the live order-flow scalper (`live/scalper.py`, Binance
user-data stream book) and `live/history_reconcile.py`.
