# Data review, 2026-11-19 — "can you analyze our data? what can you see?"

Both ledgers as dumped at 22:55 UTC on 11-19 (scratchpad `analyze3.py` / `analyze3b.py` over `overview.all_rows()`):
production 167 closed trades over 15 days, local 1,751 over 77 days (358 in the promoted books). R = net P&L ÷ the
risk at the stop; t = the mean's t-statistic against zero.

## What the record says

1. **Production is flat and one book is the loss.** Promoted books: +0.02R, −$615 net, drawdown −$1,022, half the
   days positive. Without the auction book the other four are up ≈ $1,270.

   | prod book | n | avg R | t | net |
   |---|---|---|---|---|
   | auto (reversal lane) | 97 | +0.054 | 2.3 | +$636 |
   | copy_stocks | 17 | +0.356 | 2.2 | +$643 |
   | copy_trading | 37 | −0.006 | 0.0 | +$8 (after the 11-19 restatement) |
   | prop | 6 | −0.30 | −0.7 | −$16 |
   | auction | 10 | −0.56 | −2.0 | −$1,886 |

2. **The auction book is failing.** Ten trades, three winners, t −2.0 at a quarter of its verdict sample. On 11-19
   it fired seven stock-perp trades between 15:25 and 18:10 UTC and lost five (TSLA shorted three times in three
   hours, stopped each time). Its stops average 0.9%; every loss came inside 30 minutes. The control replay says the
   same positions held 24 hours would have recovered.
3. **Every loss is a tight stop hit within the hour.** Sub-hour trades: prod −0.21R over 39, local −0.21R over 66,
   all stop-outs — auction at 0.9%, prop at 1.0%, crypto copies at 2.8%. Stop-outs are −$3,596 of prod's ledger
   against +$2,713 from trailing stops. Trades held 1–12h are positive on both instances.
4. **Shorts earn, longs lose, everywhere.** Prod longs −0.43R (15), local longs −0.13R (157). Local shorts are
   positive in all six auto lanes (factor, unlocks, weekly, ta, reversal, cascade). Copy longs' record −0.29R (13).
5. **Copy-stocks shorts are the strongest and thinnest signal.** Prod +0.45R over 15 (t 3.0), local +0.17R over 28 —
   the same sign on both ledgers. One Hyperliquid leader (0x2396…) carries the crypto copy book at +0.37R over 11;
   three leaders at 4–5 copies each sit at −0.27R to −0.55R. Stock copies had been refused by the BTC 24h-VWAP tape
   gate (NVDA, PLTR, CRWV), by the 10× leader-leverage cap (MSFT, TSLA at 20×) and by the 2-per-leader cap (NVDA).
6. **The reversal lane is a small, real, slow edge.** 97 shorts, 70% win, payoff 0.87 (avg win +0.15R, avg loss
   −0.17R) ≈ $6.50 a trade, ≈ $40 a day at 6.4 legs. Exits: hold_check 44 at −0.01R (reverted 31 at +0.06R, flipped
   10 at −0.12R, horizon 3 at −0.44R), trailing stop 32 at +0.10R, manual 13 at +0.26R. Peak R averaged +0.195 against
   +0.054 realised. Control: $334 taken vs $661 held 24h vs $1,486 held 48h (E-ratio 1.74 at 24h). The index-vs-coin
   shadow shows no difference at 43 episodes.
7. **Sizing is contained; give-back is not a problem.** Since 11-17 the median risk on prod is $245, the maximum
   $333. Of trades that reached +0.5R only 10% ended ≤ 0 on prod (22% locally).
8. **Time and weekday cells still disagree between instances** (prod loses 16–20 UTC and Mondays; local 12–16 UTC
   and Sundays). Nothing to gate on.
9. **An artefact:** the capital tracker reads the day's P&L from equity, so the 11-19 restatement (+$365) showed as
   copy_trading earning $699 that day.

## What was built on 11-20 (the user: "do all?")

1. **Auction paused on production** — out of `PROMOTED_BOOKS` in docker-compose.yml; its wallet and record stay, no
   new entries on prod. The local book keeps running as an experiment.
2. **The stop floor** — `trading/stop_floor.py`: a stop closer than 1.5% (stocks, futures) or 2% (crypto) is widened
   to the floor and the leg sized at the widened stop (same risk, room for the noise). Live in the auction entry (so
   the prop mirror too); the copy books already sat at the 2% floor and now read it from the module.
3. **The reversal lane holds a day** — `hold_check.GRACE_H` 4 → 24: inside the first 24h only the stop and the profit
   lock act; after it the horizon rule decides (hold while in profit with follow-through), 72h ends anything.
   Registered 11-20; the Books tab judges each rule's closes separately.
4. **Stock copies free of the BTC tape gate** — `against_tape` applies to crypto copies only.
5. **Restatements leave the day's P&L** — `capital.note_adjustment` writes a no-money ledger entry that
   `target.daily_pnl` treats like a deposit; `copy_trading.restate_oversized` notes its shift; a route
   (`POST /capital/restatement-note`) exists for the 11-19 restatement already applied on production.

Not done: raising the 10× leader-leverage cap for stock copies (the 15× and 20× leaders were prod's two big losers on
11-06 — that decision stands until a record says otherwise).
