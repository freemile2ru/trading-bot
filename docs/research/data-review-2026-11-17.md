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

## Addendum, 22:40 UTC — the LIT copy and the per-leg cap

A LIT short in the research copy book (opened 22:11, stopped 22:32) lost −$605 = 2.1% of its $29k book in 21 minutes. The leg was the risk budget's $358 (43.5% of the day on a +0.031R record ÷ 5.93 legs) × 1.25 for the shorts cell × 1.25 for the leader's week = $568 at the 2% stop; the second look said "doubt" (chasing, −4.3% below its 24h VWAP) but halves only in veto mode, and prod's audit still says the doubted copies win (+$265 over 23 vs −$367 over 10 agreed), so that stays.

Replayed on both ledgers (every closed copy, P&L scaled with the notional):

| rule | local 156 copies (net −$4,089) | prod 45 copies (net +$247) |
|---|---|---|
| multipliers ≤ 1 under the budget | +$96, 6 legs | −$66, 2 legs |
| stop ≤ 1% of the book | +$748, 24 legs; legs over $400 24 → 6; worst −$717 → −$621 | −$10, 1 leg |
| stop ≤ 1.5% of the book | +$278, 19 legs; legs over $400 24 → 20 | −$7 |
| both (1%) | +$603 | −$71 |

Built (candidate 1's companion): `COPY_MAX_RISK_PCT` = 1 — a copy's stop never costs more than 1% of the book, after every multiplier; and under the risk budget the record multipliers only shrink the leg (the budget already sized it by the record). Each copy stamps `risk_cap` (pct, bound, wanted_notional, size_x_full). Prod pays about $70 over its 45 copies for the cap (NEAR and PLTR were sized up and won); the research book would have kept $600 and lost its $400–1,000 bucket almost entirely.

## Addendum, 2026-11-18 00:15 UTC — three of the candidates built (the user: "go ahead")

1. **The reversal signal's index leg** (`experiments/reversal_index.py`, book `reversal_index`, $10k, local only): when the reversal lane opens a slot's legs, one ALTIDX leg (an equal-weight index of the shadow's twenty alts, rebased to 100 at the book's first tick; the book marks itself at the level each minute) in the slot's direction at one coin leg's risk, a 4% stop, closed when the slot's last coin leg closes or at the 72h cap. Every index leg is paired with its slot's coin legs in R; verdict at 40 slots: "index better" (≥ +0.1R a slot, t ≥ 2, both halves agree) is the case for moving the lane to the index. A slot first seen more than 30 minutes old (a restart) is not taken.
2. **The side switch** (`trading/sides.py`): `auto:ta`, `auto:weekly`, `auto:factor` and the `weekly_rs` book open no long. Each refused long is written down at its price (`entry_cells.refuse_long`, flag `side_off`) and scored 24h on; longs return to a book once its last 20 refused longs average ≥ +0.3% net with both halves positive. Not switched: the reversal lane (its own long shadow), unlocks and events (short by construction), cascade (retires at 120), the burst longs (their short mirrors are the comparison), hedge legs.
3. **The budget's per-leg cap** (`risk_budget.LEG_CAP_PCT` = 1): no leg risks more than 1% of its own book, after the record share and maturity. Locally it binds on both copy books (copy_trading $358 → $286, copy_stocks → $326); on prod it will bind on copy-stocks ($756 → about $330). The reversal lane's $240 on the $50k ledger is under its $500 cap.

Pre-registration: the index leg's verdict rule above; the side switch's return rule above. Neither has a replay (the first is forward by design, the second is the copy rule's shape on records that start today).

## Addendum, 2026-11-18 — the prop book's AMD trade and the margin cap

The research prop book (local) shorted AMD from the auction source at 17:45 UTC on 11-17 and was stopped at 21:03 for −$283, 1.1% of the $25k account. The leg was sized by the pre-registered rule — 1% of the account at the level stop — and the level stop was 1.46% away, so the rule asked for $17,785 of notional, $8,892 of margin at 2×: 70% of the account on one name, against a median margin of $504 across the book's other 24 legs (the reversal legs: 0.4% at a 10% stop = $1,000 of notional). The only other bound, half the leverage cap's worth of the account ($25k), never touched it. The same AMD trade in the Mentor book lost −$334 on $21k.

Built: `prop_rules.MAX_MARGIN_PCT` = 2 — no leg puts up more than 2% of the account as margin ($500 on the $25K card: $1,000 of notional at 2×, $2,500 on BTC/ETH at 5×). The risk rule stays and the margin cap binds first whenever the stop is tighter than risk × leverage ÷ 2% (every auction trade, and a reversal leg only if its stop were under 5%). Each leg stamps `margin_cap` (pct, bound, wanted_notional, margin_usd); the log and the Prop tab show it.

Replay of the local book's 25 closed legs at the cap: only AMD changes, −$283 → −$16 (its $508 of margin); the book's net +$135 → +$402. With six legs at the cap the book's worst case from open positions is about 1.76% of the account (four reversal stops at $100 and two auction stops at ~$20), under our 2.25% flatten floor — the firm's daily limit can no longer be reached by open legs alone. The ledger is left as the record (the trade was taken under the registered rule).

Production: the prop book has not opened a trade in 9.5 days — `docker-compose.yml`'s `PROMOTED_BOOKS` was never updated when the book was promoted on 11-09 (the code default had it, the compose override did not), so the wallet sat at the $10k seed and the book waited for funding. Added to the compose list; the next deploy funds it at $25k and it starts.
