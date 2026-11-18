# The single system — research and build, 2026-11-18

The user: "research on and work on a single auto trader system that earns $300 to $500 daily with minimal losses. factor
in all possible parameters." Answers to the up-front questions: a $50k paper book beside the others; any source, ours or
from the research; "minimal losses" = a 1% day stop and a 5% drawdown floor.

## 1. What the target means

$300–$500 a day on $50k is 0.6–1.0% a day, 219–365% a year. At the 0.1–0.2R a trade our best slices earn, six trades a
day need $500–$800 of risk a trade; three stop-outs in a day is −$1,500 to −$2,400, past the 1% stop. The target and the
loss limit can both hold only if the edge is real and the losers are few — the system below is built to find out, not
to assert it.

## 2. What the literature says (two research passes, every figure cited in the agents' reports)

| strategy family | best documented | realistic $/day on $50k inside 1%/day, 5% DD |
|---|---|---|
| vol-targeted trend ensemble, BTC + top-20 rotation (Zarattini/Pagani/Barbon 2025) | 18–30% CAGR, Sharpe 1.5, DD 11–19% | $8–12 |
| funding carry, spot vs perp (BIS WP 1087; Borri et al. 2025) | Sharpe 6.45 on ~8%/yr 2020–25, **negative in 2025** | $10–15 unlevered; $25–40 at 3× in the right regime |
| multi-asset perp trend with ATR trail (arXiv 2602.11708, 2026, unaudited) | 40%/yr, Sharpe 2.4 | $14–20 |
| cross-exchange funding spread | 15% gross H1 2025 (BitMEX blog); academic version negative | $0–20 |
| market-neutral fund composite (VisionTrack) | +21.8% 2024, +11.7% 2025, +4.6% 2026 YTD | $12–16 |
| cross-sectional alt momentum (Liu/Tsyvinski/Wu) | 2.7–4.1%/week gross, pre-cost; quant index −10% 2025 | $0–10 |
| short-term alt reversal | 2.2%/day gross equal-weight; **0.06%/day value-weighted** | ~$0 |
| session effects (21–23 UTC, overnight) | 35–40%/yr gross, costs not modelled | $0–5 after fees |
| liquidation / OI cascades | no backtest with a Sharpe; event studies mixed | $0 |
| grid bots / retail market making | expected return ≈ 0; L2 tests −0.18% | $0–8 |
| stat-arb pairs on perps | OOS Sharpe −0.87 | $0 |

Live market (2026-11-18 16:14 UTC, pulled from the exchanges): BTC funding +6.9%/yr Binance, −3.0% Bybit, 30d means
6.6–7.1%; ETH and SOL negative on every venue this week; Binance Dec/Mar quarterlies 5.3% (BTC) / 4.2% (ETH); sUSDe
5.35% APY; HLP trailing 18.8% with a −0.11% display two weeks ago; BTC 30d realised vol 44%; Fear & Greed 12; BTC −26%
from the October high. The fat funding is in stock perps (SK Hynix −232%/yr, NVDA +36% on Bybit).

The agents' frank statement, which this note adopts: under a 5% drawdown cap the evidence supports **8–20% a year, $11–27
a day**; $300–500 a day on $50k needs a Sharpe of roughly 20–50 at the vol that keeps the drawdown under 5%, and no
published category is within an order of magnitude. Crypto quant funds: median since-inception Sharpe 1.5, median max
drawdown −20.7%, 2025 average +0.4%. Prop-firm data from below: ~7% of challenge buyers are ever paid, 60–70% of
failures are drawdown breaches.

## 3. What our own ledgers say (replay, scratchpad `composite_replay.py` on the 11-17 dumps)

Every closed trade with an R, scaled to one risk per trade, taken in time order under a 1%/day stop and a 5% static
drawdown on $50k. Slices chosen from the 11-17 review (in-sample on this data — the warning is in §5):

| set | local 75 days | prod 13 days |
|---|---|---|
| all trades | 550, −0.017R: **no expectancy** at any size | 130, +0.045R |
| all shorts | 199, +0.073R, t 1.46 | 117, +0.083R, t 1.92 |
| **the short slices with a record** (reversal, weekly, factor, unlocks, events lanes; weekly_rs; copy-stocks; prop) | 103, +0.119R, t 2.43, 1.4/day | 93, +0.096R, t 2.74, 7.1/day |
| shorts opened 16–20 UTC | 39, +0.259R, t 2.13 | 30, +0.167R, t 1.88 |

The chosen set at $500 of risk a trade (1% of the book), 10 legs, a 20% notional cap:

| | local | prod |
|---|---|---|
| $ a calendar day | +$219 | +$143 (+$345 uncapped: the copy-stocks shorts have 2% stops) |
| median trading day | +$243 | +$110 |
| days positive | 78% | 62% (85% uncapped) |
| worst day | −$557 | −$456 |
| max drawdown | −$827 | −$696 |
| day-stop days | 1 | 0 |
| daily Sharpe | 9.3 | 6.8 |

Sensitivities: the notional cap costs prod most (0% → $345, 10% → $33, 20% → $143, 30% → $200 a day) because its
copy-stocks edge sits on 10 trades with tight stops; local is flat across caps. The day stop between 0.5% and 1.5%
changes nothing material. Concurrency under 8 legs loses half the record. Time-of-day and weekday cells disagree
between the two ledgers (local 12–16 UTC −$584, prod +$249; local Monday +$3,134, prod −$765): no time gate.
What $400/day needs at the record's edge: $2,449 a trade locally (4.9% of the book), $582 on prod (1.2%) — the
difference is prod's reversal lane firing 6.4 legs a day against local's 1.3.

## 4. The system built — `experiments/composite.py`, book `composite`, $50k, local only

Not a new signal: the programme's record, filtered to what has worked, sized and floored as one account.

- **Sources, shorts only**: the reversal lane's picks (straight from the scores, as the prop book takes them, 10% disaster
  stop); the auto ledger's weekly, factor, unlock and event legs as they open; the weekly_rs book's shorts; the
  copy-stocks book's shorts. Each leg stamps its source and the source's trade id.
- **Size**: 1% of equity at the source's stop ($500); a leg's notional ≤ 25% of equity ($12,500 — a tight stop may not buy
  the book, the prop book's AMD lesson); gross ≤ 150%; 10 legs; one per symbol; 5× leverage.
- **Exit**: the source's stop, the ATR trail once 1% up (1 daily ATR from the best price), a 72h cap — the prop book's exit,
  +0.17R on the same picks against the lane's +0.05R with hold_check.
- **Floors**: today's loss (realised and floating, against the 00:00 UTC balance) halts new legs at 0.75% and flattens at
  1%; a static drawdown from $50k halts at 4% and flattens and **stops the book** at 5% (reset by hand).
- **Verdict, pre-registered**: after 40 legs and 20 calendar days, promote to production when the mean $/calendar day ≥
  $150 with the daily P&L's t ≥ 2 and the drawdown never past 5%; retire at 60 legs with ≤ $0. The One book tab shows
  the daily bars against the $300–500 band, the floors, the legs, the record by source and the log.
- Rejected for the book: longs (no slice has an edge); the auction book (NVDA −$924, prod −1R over 2); cascade (no
  information); burst longs; copy-crypto (no edge over 116 trades); any time-of-day gate (the late_us cell is failing
  forward on both instances); carry as a sleeve (a spot leg does not fit a perp wallet, and at $50k × 1× it is $7–13 a
  day — run it beside, not inside).

## 5. What to expect, honestly

1. The replay's $219–$345 a day is **in-sample**: the slices were chosen on the same 13–75 days they are scored on, and
   the local record of the proven lanes is 40 days old. The literature's out-of-sample figure for a good system under a
   5% drawdown is $11–27 a day. The forward record decides which number is real; the verdict rule above is the only
   thing that promotes it.
2. If the edge is what the ledgers say, the book makes $150–$350 a day with a −$500 to −$800 worst day and a 1–2%
   drawdown — the bottom of the target, not the top. If it is what the literature says, it makes $10–30 a day and the
   verdict retires it at 60 legs.
3. The one input that would move it toward the target is **frequency**: prod's reversal lane fires 5× more often than
   local's. The book takes every short the sources open, so it inherits that difference; nothing in the book itself
   manufactures trades.
4. Next lab if the forward record disappoints: the vol-targeted trend ensemble (Zarattini et al. 2025: breakout
   lookbacks 5–360 days, trailing stop at the channel midpoint, 25% vol target, 0.10% costs; Sharpe 1.5, DD 11–19%) —
   the best-documented directional system in the literature, and one we have not tested.
