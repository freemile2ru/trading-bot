"""Scraped news: RSS/Atom feeds, Google News per coin, Binance announcements, SEC and Fed
press releases — collected on a schedule, classified by Claude, scored per coin.

  sources.py    what is fetched and how each format is parsed (dates normalised to UTC)
  store.py      news.db — items (deduplicated across sources), source health, price reactions
  classify.py   Claude labels each headline: coins, event type, direction, importance
  scoring.py    per-coin news score (importance × direction × recency decay) and news bursts
  collector.py  the polling loop, alerts on held coins, and reaction labelling
"""
