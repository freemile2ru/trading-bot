# History review — every closed trade on both instances, 2026-11-17

Both ledgers dumped at 19:46 UTC (scratchpad `dump_rows.py` → `history_review.py`). Prod: 130 closed trades over 13 days (11-04 →), net −$326, 3 open. Local: 550 closed on the active books over 75 days, net −$4,382 (the retired books' 1,130 trades, −$12,874, are out of every figure here); 40 open. Retired books now leave every total (commit 3c214a0).

## What stands out

**1. We are biggest when we are wrong — on both instances.** Outcome falls with size, monotonically.

| risk per trade | prod n / net / mean R | local n / net / mean R |
|---|---|---|
| under $50 | 14 / +$42 / +0.17 | 305 / +$198 / +0.02 |
| $50–150 | 53 / +$545 / +0.10 | 100 / −$422 / −0.05 |
| $150–400 | 55 / +$312 / +0.02 | 62 / −$2,051 / −0.12 |
| $400–1,000 | 8 / −$1,226 / −0.33 | 82 / −$2,124 / −0.04 |

Prod's losers average $5,851 of notional, its winners $2,629. The eight prod trades risking over $400 are −$1,226 between them; the 67 risking under $150 are +$587. The daily risk budget is the mechanism: it hands the largest legs to the thinnest records (copy_stocks 50% of the day, $2,519 a leg, on 12 trades at +0.12R; locally copy_trading 44% on +0.03R). The books with a record (the reversal lane, 83 and 27 trades) get the smallest legs.

**2. Full stops are the entire loss.** Prod: 13 stop-outs (10% of trades) −$2,263; 52 trailing stops +$1,930; everything else nets to about zero. Local: 90 stop-outs −$8,188 against 68 trailing stops +$4,233. Nine of prod's thirteen stop-outs are copy trades (−$841), two are copy-stocks (−$356), one is the auction book (−$519), one the reversal lane (ZEC, −$546, 11-06). The reversal lane stops out 1% of the time.

**3. The reversal lane's edge is market timing; the coin pick and the exit each give a slice back.** Prod, 83 shorts: +$350, +0.05R a trade, 71% won, no trade over +1R, ex-top-2 +$28 — it never loses big and never wins big. Its entries are ahead of price (E-ratio 1.98 at 24h; held 48h +$1,467 vs +$364 actual), its `hold_check` exits are the drag (31 exits, −$179, 42% won, ~9h in), and the index shadow over 54 episodes says the equal-weight alt index moved +0.75% (t 2.87), the picked coin +0.62%, the leg as traded +0.51%. That index figure is the single most significant number in the dataset; its 40-episode verdict is at 32.

**4. Every lane's long side loses and short side wins — five lanes now, not one.**

| lane (local) | shorts n / net | longs n / net |
|---|---|---|
| auto:weekly | 10 / +$929 (10 of 10 won) | 10 / −$1,496 |
| auto:factor | 8 / +$971 | 16 / −$440 |
| auto:unlocks | 5 / +$575 (5 of 5) | 4 / −$386 (0 of 4) |
| auto:reversal | 22 / +$242 | 5 / −$891 |
| weekly_rs book | 11 / +$192 | 13 / −$330 |

Local longs overall: 351 trades, −$7,283, 43% won; the 47 longs that lasted under an hour lost −$2,682 (23% won) and the 30 held over three days lost −$2,760. Longs at 10× or more: 100 trades, −$2,088. Prod's 13 longs: −$250, 46% stopped out in full. Copy longs are now off on production; the long gate's forward read locally is kept −0.20%/slot vs refused −1.04% (t −2.65), 36 of 40 to its verdict.

**5. The copy book's edge is doubtful on both instances.** Local copy_trading: 116 trades, verdict "no edge"; its shorts 71 trades −$999 at 0.00R with 28% stop-outs. Prod copy shorts: 22 trades +$221, but ex-top-2 −$186 and 18% stop-outs; the last week −$315. Copy-stocks shorts carry both instances' copy results (prod +$276 over 10, 80% won, PLTR and LLY). The budget gives the copy books 19–44% of the day's risk on this.

**6. Time cells.** Shorts opened 16–20 UTC: +0.17R (30) prod, +0.26R (39) local — the late-US hour again. But the pre-registered `late_us` cell is failing forward on both instances (prod 11 in at +0.32% vs 42 out at +0.45%; local 7 in at +1.35% vs 22 out at +1.42%): don't build on it. Monday shorts −0.15R (28) prod; Saturday shorts +0.62R (23) local, +0.16R (17) prod — unregistered, noted only.

**7. The auction book is prod's biggest single item.** NVDA −$924 over two trades (11-04/05) is larger than prod's whole net; without them prod is +$598. The next-open rule starts in the 11-18 session.

**8. Cascade carries no information.** 112 trades, −$4, 52% won, E-ratio 1.0 at every horizon, index shadow says the index beat the coin (+0.41% vs +0.24%). The verdict rule retires it at 120 trades (8 away) as "no edge".

**9. The prop book has the best R of any active book.** 24 shorts, +$418, +0.17R, 62% won, no stop-outs, +$292 in the last week; held 48h +$1,030 vs +$446 actual, so the 72h trail deployed today should take some of that.

**10. Burst books as reviewed yesterday**: burst_up +$173 over 47 with a −0.25R median and 26% stop-outs; burst_dip +$27 over 37. Not promotable. The two short mirrors start recording from today.

**11. The "expected" line of the risk budget is the honest ceiling.** Prod: reversal +$49/day, copy_trading −$32, copy_stocks +$310 (on 12 trades) = $327; local $47/day. The $500/day target is not in the records, and most of prod's figure rests on the thinnest record.

## What changed since the 11-16 review

- The short-only finding now holds across five lanes, not just the reversal lane and the copy books.
- Size-versus-outcome is new and structural: it was visible in the losers' notional on 11-16 but not measured by risk bucket.
- The late-US cell, promising on 11-12, is failing forward on both instances.
- The index-timing result strengthened (t 2.87 over 54 episodes, 32/40 forward).
- The programme's weekly trend: prod −$632 / +$352 / −$46; local −$2,675 / −$1,513 / +$255. Days positive 7 of 14 on prod, 15 of 34 locally.

## Candidates (not built)

1. **Size by record length, not just record level.** Leg risk = budget share × min(1, n/40), or equal shares until a book has 40 trades. Replayable on both ledgers before anything changes.
2. **Express the reversal signal in the index or majors**, or add an index leg, when the index-shadow verdict lands (8 episodes away).
3. **Retire cascade at 120** per its rule; nothing more to learn from it.
4. **Reversal exits**: the hold_check at ~9h is the drag; the ATR trail is live since 11-14 — read its 40-leg record before touching hold_check.
