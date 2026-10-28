"""Non-crypto markets on Binance — stocks and futures (commodities) as USDⓈ-M perpetuals, on paper.

Three books per market, so the same question is asked three ways (the Markets tab):
  trend    6-month trend following, daily, equal volatility — the one approach with research behind
           it outside crypto (research/macro_trend_lab.py: made money 2020–26, but no more than holding)
  rules    our scanner + the autotrader's entry rules, no Claude (on stocks the replay lost −0.28R/trade)
  claude   the same rules, Claude approves each entry (its own daily budget)
"""
