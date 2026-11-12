# Entries and the long side — research note, 2026-11-12

The user's ask: entries keep coming up as the root cause of losses and longs lose while shorts make money; find out, from our data and from everything published, how to fix it.

## 1. What our own tape says (542 closed crypto trades, 125 stock trades, local + prod, all active systems)

Method: every closed trade scored on Binance 1h bars — the move into the entry (1h/4h/24h, signed in the trade's direction), the best and worst excursion after it (MFE/MAE at 1h/4h/24h), the hour, BTC's 24h/7d move at entry, the funding rate at entry — and the Turtle **E-ratio** (mean MFE ÷ mean MAE in ATR units; random entries score 1.0). Scripts: `scratchpad/entry_quality.py`, `entry_report.py`.

| | n | net | %/trade | t | E-ratio 1h / 4h / 24h | reach +1% in 4h |
|---|---|---|---|---|---|---|
| crypto long | 331 | −$7,096 | −1.02 | −2.7 | **1.07 / 0.98 / 0.92** | 62% |
| crypto short | 211 | +$1,067 | +0.28 | 1.1 | **1.31 / 1.09 / 1.47** | 58% |
| stock long | 81 | −$1,722 | −2.74 | −3.7 | — | 19% |
| stock short | 44 | −$1,361 | −0.01 | 0.0 | — | 36% |

**The long entries are random.** An E-ratio of 1.0 at every horizon means that after a long entry price is as likely to go against us as for us, by the same distance. The short entries have a real edge (1.3–1.5). This is not an exit problem (the 11-10 exit review already showed the stops are right): a random entry loses the fees and the drift whatever the exit.

Where the long losses sit (crypto):

- **Chasing.** Longs entered after the coin had already risen 0–2% in the last 4h: −2.08%/trade (t −4.3, n 90). Longs entered after a >15% 24h rise: −2.87% (t −2.0). Longs entered after a >2% 4h drop: −0.44% (n.s., n 91), with +1% reached within 4h 78% of the time. The autotrader's TA lane is the clearest case: its longs come after a +7.8% 24h move on average, −1.17% a trade (t −3.7, n 84).
- **BTC already up.** Longs when BTC's last 24h was +0–2%: −1.88% (t −4.0, n 130); BTC >+2%: −1.84% (t −2.5). Longs with BTC down on the day: −0.03 to −0.8%, not significant. Mirror image on the short side: alt shorts when BTC is >+2% on the day: **+2.37% (t 3.0, n 24)**.
- **Funding.** Longs paying ≥0.01%/8h: −1.12% (t −3.2, n 119); ≥0.03%: −6.36% (t −3.2, n 12). Longs at ≤0.01%: −0.5%, not significant. Shorts collecting 0.01–0.03%: +0.78% (t 2.4).
- **US open.** Longs opened 12–16 UTC: −1.14% (t −3.4, n 64) and only 36% reach +1% in 4h, the worst cell in the table. Shorts 16–20 UTC: +0.96% (t 2.3).
- **Weekdays.** Longs Mon–Fri −1.28% (t −4.3); weekend longs −0.3% (n.s.).
- **By lane.** reversal longs −3.59% (n 5), factor longs −2.87% (t −3.1), TA longs (both ledgers) −1.2 to −1.4%, copy longs −0.42% (n.s.), weekly longs −1.2 to −6%, cascade longs 0.0%. No long lane is positive. Reversal shorts +0.64% (t 1.8, n 56), factor shorts +3.7%, weekly shorts +3.3% (t 4.0, n 6), prop shorts +3.1%.

What would the obvious gates have done to the 291 non-burst longs (in-sample, so a ceiling, not a forecast):

| gate | kept | %/trade | t |
|---|---|---|---|
| none | 291 | −1.03 | −5.0 |
| BTC 24h < 0 | 178 | −0.29 | −0.5 |
| funding < 0.01%/8h | 200 | −0.64 | −1.1 |
| coin not up in the last 4h | 189 | −0.71 | −2.4 |
| not 12–16 UTC | 267 | −0.99 | −2.1 |
| all four | 68 | **+0.06** | 0.2 |
| rejected by any | 223 | −1.36 | −5.6 |

The gate removes the bleed; it does not create an edge (first half of the sample kept: −0.89%, second half: +0.30%). Four cuts chosen on the same data is also a textbook overfit, which is why it goes forward as a pre-registered shadow and not as a rule.

**Limit entries below the signal do not help — adverse selection.** Simulating a resting bid 0.25/0.5/0.75 ATR under the signal price: the orders that fill are the trades that were going to lose anyway (filled at market −1.7%, unfilled +0.2%), and the discount only recovers part of it (0.5 ATR: −1.77% → −1.06% on the 50% that fill). A better price on a random entry is still a random entry.

## 2. What the literature says

**Liquid-coin returns live in the short leg; the long-side reversal only exists in coins we can't trade.** Fičura (2023, FFA WP 5:003, 2017–2022, weekly quintiles): for coins with >$50M cap and >$5M/week volume, one-week returns show *momentum* (Q5−Q1 +1.36%/wk, t 2.3), and "the short-term momentum effect is driven mostly by the short leg … and the low returns in the Q1 quintile" (Q1 −0.20%/wk vs Q5 +1.15%). Distance from the 1-week high is a stronger sort (t 4.9): coins far below their 1-week high earn −0.70%/wk, those at it +1.98%. The famous weekly *reversal* (t −7.3) is confined to small/illiquid coins — "driven mostly by the long leg … buying the coins that underperformed" — and "driven mostly by coins with very low trading volumes (under 100 thousand USD in the last week)". Our longs buy dips in liquid coins: that is the losing quintile of the liquid-coin table, not the winning quintile of the illiquid one.

**Short-horizon mean reversion exists but is smaller than our fees.** Kitron & Wengrowicz (arXiv 2608.21888, 183 Binance pairs, 15-min bars, 2025–26, six-month holdout): reversal after flow-driven bars is real (AUC 0.54 vs 0.52) and strongest 10–14 and 21 UTC, but the gross edge "peaks near 1.3 bp per trade against a 5 bp round-trip cost"; not one of 183 pairs clears maker fees; the effect is gone by 4h (AUC 0.496). Fayez (SSRN 6701738): OHLCV + funding signals on ten large-cap perps have no cross-sectional alpha at an 8h horizon. Schmalz (SSRN 7227998): order-flow imbalance's sign flipped across samples. This is the same answer our cascade lane gave (12 bps gross vs 10 bps fees) and the quarter-hour lab gave on 11-07.

**Two-week momentum is the one robust cross-sectional signal; retail chases trends.** Borri, Liu, Tsyvinski & Wu (arXiv 2510.14435, Oct 2025): the crypto momentum factor is a *two-week* lookback (weekly Q5−Q1 +2.6%, t 3.9; post-2020 +2.1%, t 3.7); 12- and 24-month momentum are insignificant; retail investors are "contrarian in stocks and gold, but trend-chasing in cryptocurrencies". The perp carry trade's Sharpe fell from 6.45 (full sample) to 4.06 in 2024 and negative in 2025 — funding premia are "neither guaranteed nor permanent".

**The alt long side carries a negative drift.** Blockworks (via CryptoRank): of 1,972 tokens that crossed $50M cap 2020–2025, 4.1% outperformed Bitcoin; Swan: average top-300 alt drawdown 76% over five years. Industry sources, not peer-reviewed, and not beta-adjusted — but consistent with our 11-11 index read (alts −12% in ten days while our longs were on) and with the paper above: $1 → ~$160 in Bitcoin vs ~$80 in the whole market.

**Time of day.** Padysak & Vojtko / Quantpedia: Bitcoin's return is concentrated in a short window around US hours; the Springer "tea time" study: returns lowest in early-morning UTC, highest early afternoon/evening, with volatility and illiquidity cycling the same way. Nothing rigorous on alts; our 12–16 UTC long cell is a local finding until it survives forward.

**Liquidation cascades.** Bank of Canada / DeFi liquidation studies find prices usually recover within hours; the one tradable write-up (+2.33%/trade, 72 trades) attributes 54% of it to BTC movement with no significant alpha; arXiv 2608.03616: no metric warned before all seven BTC cascades. Matches our control data: the post-cascade rebound is there, the coin pick and BTC beta eat it.

**Entry quality measurement.** Faith's E-ratio (Way of the Turtle) is the standard way to judge an entry stripped of exits: MFE/MAE over a fixed window in ATR units; random ≈ 1.0. StrategyQuant and Build Alpha implement it the same way. Our control replay already stores best/worst by window — the ratio is one line away.

**Trend filters.** The 200-day filter on BTC is "drawdown insurance" (−2.6 points of drawdown for −0.17 Sharpe); 25 of 36 upward crosses failed within 30 days. It is a regime switch for *exposure*, not an entry signal.

## 3. What this adds up to

1. The long side of our programme has no entry edge, on any lane, in any regime cell, and the entries that lose most are the ones that chase (coin up, BTC up, funding paid, US open). The published evidence says this is structural: in liquid coins the money is in shorting the losers and in holding the two-week winners near their highs, and the dip-buying reversal only pays in coins too thin to trade. Our reversal lane is already a short lane in practice (56 shorts +0.64% vs 5 longs −3.6%).
2. Better execution (limit entries, pullback fills) cannot rescue a random entry: the fills are adversely selected. The lever is *which* longs are taken, not *how*.
3. Sub-hour mean reversion is real and below our fee line; nothing published or in our labs clears 10 bps round trip at that horizon. Stop looking there.
4. Shorts fading a strong BTC day (+2.4%, t 3) and shorts opened 16–20 UTC (+1.0%, t 2.3) are the two strongest cells on the tape; both are in-sample reads of ~25–40 trades.

## 4. Candidates, in order (none built; the user decides)

- **A. Long gate shadow (cheapest, pre-registered).** Every long the autotrader's own lanes and the copy book would open is also scored against four conditions — funding < 0.01%/8h, BTC 24h < 0, coin not up over the last 4h, not 12–16 UTC — and recorded as kept/rejected without changing the trade. Verdict at 40 rejected longs: the rule is adopted if the rejected set is ≥0.5%/trade worse than the kept set with t ≥ 2 and both halves agreeing. Costs nothing, settles in a few weeks.
- **B. Long leg re-pointed at the right quintile (a lab first).** Replace dip-buying longs with Fičura's distance-from-1-week-high sort on the liquid universe (long the coins at their 1-week high, two-week lookback as in Borri et al.), hedged with a BTC short so the alt drift is out; replay on our 1h history (we have 60 days of scanner readings and years of klines) at 10 bps a side before any forward book.
- **C. Make the reversal lane short-only** and let the long budget go to the index shadow's winner (BTC/ETH vs the picked coin) — the shadow's verdict is already due at 40 episodes.
- **D. E-ratio in the control report**, per book and lane, from the best/worst columns already stored, so entry quality is watched forward instead of re-derived each review.
- **E. Short-side cells to watch, not act on:** alt shorts on a BTC +2% day, shorts 16–20 UTC — flagged in the lanes report, verdict at 40 each.

Not recommended: limit/pullback entries (adverse selection shown above); any sub-hour reversal lane (below fees in every source); a 200-day BTC filter as an entry signal (it is a drawdown cap, and our losses are not in the BTC<0 cells).

## Sources

- Fičura, *Impact of size and volume on cryptocurrency momentum and reversal*, FFA WP 5:003 (2023) — https://quantitative.cz/wp-content/uploads/2023/09/impact_of_size_and_volume_on_cryptocurrency_momentum_and_reversal.pdf
- Borri, Liu, Tsyvinski, Wu, *Cryptocurrency as an Investable Asset Class: Coming of Age* (2025) — https://arxiv.org/html/2510.14435v2
- Kitron & Wengrowicz, *Short-horizon mean reversion in cryptocurrency markets* (2026) — https://arxiv.org/pdf/2608.21888
- Fayez, *Failure of Cross-Sectional Alpha Screening on Cryptocurrency Perpetual Futures* — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6701738
- Schmalz, *Order Flow Imbalance and Short-Horizon BTC/USDT Returns* — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7227998
- Fieberg et al., *A Trend Factor for the Cross-Section of Cryptocurrency Returns* — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4601972 (abstract only; SSRN blocked the full text)
- Fan, Jiao, Lu, Tong, *The Risk and Return of Cryptocurrency Carry Trade* — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4666425
- Dobrynskaya, *Cryptocurrency Momentum and Reversal* — https://www.semanticscholar.org/paper/951c9d236c9cc32afd1d5bdadc2ec29952587048
- Liu, Tsyvinski, Wu, *Common Risk Factors in Cryptocurrency*, NBER w25882 — https://www.nber.org/system/files/working_papers/w25882/w25882.pdf
- Quantpedia, *Are There Seasonal Intraday or Overnight Anomalies in Bitcoin?* — https://quantpedia.com/are-there-seasonal-intraday-or-overnight-anomalies-in-bitcoin/
- Springer, *The crypto world trades at tea time* — https://link.springer.com/article/10.1007/s11156-024-01304-1
- Bank of Canada, *Liquidation Mechanisms and Price Impacts in DeFi* — https://www.bankofcanada.ca/wp-content/uploads/2025/03/swp2025-12.pdf
- arXiv 2608.03616, *Measuring the engine of a liquidation cascade* — https://arxiv.org/abs/2608.03616
- Build Alpha, *E-Ratio (Edge Ratio)* — https://www.buildalpha.com/eratio/ ; StrategyQuant — https://strategyquant.com/blog/edge-ratio-in-strategyquant-x/
- The Long Trend, *I tried to debunk the 200-day moving average* — https://thelongtrend.substack.com/p/i-tried-to-debunk-the-200-day-moving ; CoinPaprika backtest — https://coinpaprika.com/education/bitcoin-200-day-moving-average-backtest
- Blockworks token study via BeInCrypto — https://beincrypto.com/bitcoin-vs-altcoins-which-is-better/ ; Block Scholes altcoin rotation — https://www.blockscholes.com/research/bybit-x-block-scholes-the-altcoin-rotation-why-and-when-altcoins-outperform-bitcoin

## 5. Built the same day ("experiment all") — what exists now

- **A + E — `experiments/entry_cells.py`, Control tab "Entry cells".** Every closed crypto trade of auto, main, auto_rules, copy_trading, copy_crypto_wide, cascade, weekly_rs and claude_swing gets the conditions at its entry (funding last settled, BTC's 24h, the coin's 4h in the trade's direction, the UTC hour) and is sorted into the long gate (kept / rejected, with the failing flags) or the two short cells. Pre-registered verdicts at 40 forward trades (rejected longs; shorts in the cell): a gap ≥ 0.5%/trade, Welch t ≥ 2, both halves agreeing. Backfill on the local ledgers, 448 trades: kept longs +0.15% (n 69) vs rejected −1.39% (n 219, t −5.6); by flag: funding paid −1.39% (115), BTC up −1.66% (134), chasing 4h −1.55% (120), 12–16 UTC −1.31% (60). Shorts on a BTC +2% day +2.55% (n 17, t 2.7) vs −0.28% outside; 16–20 UTC +0.22% (n 29) vs −0.02%. Forward count from 2026-11-12: 4 longs, 1 short. `GET /api/entry-cells`, `POST /api/entry-cells/run`.
- **C — the reversal lane has been short-only since 2026-11-02** (`AUTOTRADER_REVERSAL_SIDES` defaults to `short`; prod sets nothing else), so the five losing longs predate it. What was missing is the control: `entry_cells.shadow_reversal_longs` now writes down the top-K coins of every 4h slot — the longs the lane does not take — and scores them 24h later alone and BTC-hedged, fees paid; verdict "long leg real" at 40 settled slots (hedged ≥ 0.2%/slot, t ≥ 2, both halves). Hooked in `engine._reversal_lane` only while longs are off. Two slots written at build time.
- **D — the E-ratio is in every control report** (`experiments/control.py`, `all.e_ratio` and `early.e_ratio` at 1h/4h/24h, on 5-minute bars so wicks count) and on the Control tab under the replay table. First read: reversal lane 0.88 / 0.77 / 1.20 (n 24), cascade 0.86 / 0.93 / 1.45 (77), burst_dip 0.58 / 0.68 / 0.68 (16), prop 1.07 / 1.54 / 1.49 (6).
- **B — `research/high_momentum_lab.py`, REJECTED, on the Lab shelf.** 40 most-traded perps (chosen by today's volume — a survivorship bias that flatters every long leg), daily bars from 2023-01-01, 199 Monday-to-Monday weeks, 5 bps a side. Long the top quintile: near the 1-week high +0.85%/wk (t 1.2, halves +1.88 / −0.18), BTC-hedged −0.09%/wk; two-week momentum +1.95%/wk (t 2.1, both halves positive) but the universe held equal-weight made +1.28% and BTC +0.84%, so hedged it is +1.01% at t 1.35; the dip-buy we do +1.59% unhedged, hedged +0.66% with the halves split (−0.77 / +2.07). Nothing hedged clears the pre-registered bar: the long side of liquid alts is market beta plus noise at our fees. No forward book. Results: data/research_cache/high_momentum_results.json.

Nothing trades differently after this build. The reads above are the backfill; the verdicts are forward-only.
