# Data review — everything we record, 2026-11-16

Every data type on both instances, read on 2026-11-16 ~17:00 UTC: the ledgers (closed trades by book and side), the daily-target tracker, the 40-trade verdicts, the control replays (what holding would have made), the entry cells and the live long gate, the index shadow, the reversal long shadow, the exit-policy and lock shadows, the copy records and second look, the stock-flow scores, the risk budget, the prop book, the burst/weekly/cascade experiment books, and the operational state. Prod = the promoted books on the Tokyo box; local = the experiments.

## 1. What stands out

**1. The short side is the whole business.** Every book that shorts earns; every book that longs loses, on both instances, in every regime cell we have.

| | n | net | win |
|---|---|---|---|
| prod reversal shorts | 80 | +$383 (+0.61%/trade) | 72% |
| prod copy shorts (crypto + stocks) | 27 | +$374 | 67% |
| prod copy longs | 11 | −$150 | 36% |
| local auto shorts / longs | 45 / 67 | +$2,669 / −$3,849 | 78% / 34% |
| local weekly book shorts / longs | 11 / 13 | +$192 / −$330 | 73% / 15% |
| copy record, last 20: shorts / longs (R) | | +0.18…+0.33R / −0.29…−0.35R | |

The reversal long shadow — the longs the lane does not take, scored 24h on — is at 34–36 settled slots: **−3.0% a slot alone, −0.8% hedged with BTC**. The long leg of our best signal loses, forward, on both instances. Verdict at 40 in days.

**2. Exits leave most of the money on the table, on every book whose entries are good.** "Held 24h with its stop" against what we actually made:

| book | n | actual | held 24h | E-ratio 1h/4h/24h |
|---|---|---|---|---|
| prod reversal lane | 66 | +$75 | **+$858** | 1.12 / 1.56 / 2.22 |
| prop book (replay, 20 legs) | 20 | +$465 | +$1,091 (48h), **+$1,116** (1-ATR trail) | 0.63 / 0.84 / 1.81 |
| burst_up | 38 | +$216 | **+$932** | 1.40 / 1.79 / 2.43 |
| auto:weekly mirror | 16 | −$798 | +$644 (4h) / +$336 | 1.85 / 1.64 / 1.49 |

Entries ahead of price (E > 1) cut by a bell (24h expiry, 4h expiry) or a 65%-of-peak lock. The ATR trail went live on the reversal lane on 11-14; the three lock shadows beside it are already behind it (old 65% lock −0.06R, ½-ATR −0.21R, tight −0.05R a leg on 10–14 legs). The books with E < 1 (burst_dip 0.88 / 1.16 / 1.08, cascade 0.88 / 0.96 / 1.00) gain nothing from holding — their entries are the problem.

**3. The market beats our coin pick.** Index shadow, prod reversal shorts, 29 of 40 forward episodes: the coin we shorted +0.73%, the equal-weight alt index +0.82%, BTC+ETH +0.67%. Cascade longs (local, 99): coin +0.24% vs index +0.41%. The signal's information is in *when*, not *which coin*.

**4. The regime cells repeat across ledgers.** Shorts opened on a BTC +2% day: +2.51% (19, local) and +2.66% (8, prod) vs +0.4–0.5% for other shorts. Shorts opened 16–20 UTC: +1.51% (22, prod). Longs failing the gate: −1.8% (local, 519 recorded) vs −0.1% kept. The live gate has refused 36 longs locally and 3 on prod since 11-15; the first would-have scores settle this week.

**5. Leader quality is measurable and already priced in.** Leaders above 10× leverage: −0.25R (local, 13) and −0.58R (prod, 2); Lighter leaders beat Hyperliquid on both instances (+0.19…+0.31R vs +0.09…+0.30R).

**6. The second look's Claude verdict carries no information.** Prod: copies Claude *agreed* with −$551 over 8 (3 wins); copies it *doubted* +$327 over 20 (13 wins). Local: agreed −$77 over 28, doubted −$688 over 30. The tape read vetoes nothing (0 "disagree" on either instance). The doubt-halving is the only effect, and it halves the wrong trades on prod. It costs a Claude call per copy.

**7. The stock side has nothing that works.** Stock flow (17 marks scored): the crypto-signed score +0.09%/slot (t 0.25), the stock-signed score −0.36%/slot (t −1.0) — the signs chosen on the lab's first half are failing forward. Stock copies: longs −$703 (14), shorts +$10 (21). The auction book has taken nothing in ten days. stocks_trend: no edge.

**8. Operational.** The local programme breaker has been paused since 11-03 (7-day was −$212 then; it has no auto-resume) — local books run on fixed sizing. vol_premium is the local programme's worst line (−$115/day over 7 days; 5 condors, −$508). On prod the risk budget hands copy_stocks 62% of the day ($3,142 a copy) on an 11-trade record of +0.11R — a legitimate but concentrated bet.

## 2. What we can leverage

- **The reversal short at full size with the trail.** It is the one lane with an entry edge (E 2.2 at 24h) and now an exit that keeps it. The risk budget gives it $198–266 a leg; the index-shadow verdict (29/40) may say to express it in the index or BTC+ETH rather than the picked coin, which would also remove most single-coin stop-outs.
- **The BTC-up-day cell for shorts** — +2.5% a trade on 27 trades across both ledgers, three to four times the base rate — as a pre-registered size multiplier (×1.5) on reversal and copy shorts, shadowed then live.
- **burst_up's runners.** +0.118R over 45 with the 4h bell; held 24h +$932 vs +$216. A 24h-hold variant as a shadow book.
- **The prop book's trail.** The same evidence, on its own 20 legs (+$1,116 vs +$465). Decision pending.
- **The copy records as they stand**: lean short (the gate does this for longs), ≤10× leaders only (already the rule), Lighter weighted up (already by venue record).

## 3. How to improve — in order of evidence

| # | change | evidence | status |
|---|---|---|---|
| 1 | Prop book: the 1-ATR trail / 72h cap instead of the 24h expiry | 4 independent reads; +$1,116 vs +$465 on its own legs | your go/no-go |
| 2 | Shorts on a BTC +2% day: ×1.5 size, pre-registered, shadow then live | 27 trades, +2.5% vs +0.5% | build |
| 3 | ~~burst_up: 24h-hold shadow book~~ — withdrawn the same day: an independent review found the held-24h gain is two coin-days (AKE 11-13, JCT 11-08); without them held-24h is −$162 and 58% of entries stop out inside a day | see the burst review below | withdrawn |
| 4 | Second look: Claude verdict to record-only (keep the tape read) | no information on 86 copies; a Claude call per copy | your call |
| 5 | Risk budget: cap any book's share at 50% | copy_stocks at 62% on 11 trades | build |
| 6 | Weekly book: the trail on its legs as a shadow | E 1.7 entries, −0.086R outcomes; held 4h +$644 vs −$798 | build |
| 7 | Resume the local breaker, or leave the experiments at fixed size | paused since 11-03 on a −$212 that is now −$144 | your call |
| 8 | vol_premium: let the 40-trade verdict run, but it is −$115/day locally on 5 trades | collecting (5) | watch |
| 9 | Stock side: nothing to promote; the stock-flow score needs its 45 days | stock signs −0.36%/slot forward | wait |

Nothing in the data argues for more longs, more books, or more coins. It argues for fewer, shorter rules on the one signal that works, run at size, held properly.

## Addendum — independent review of the burst books (2026-11-16, evening)

A separate analyst with only the raw data (45 + 37 trades, the control replays, the exit shadows): **no edge after costs in either book.** burst_up mean R +0.118 but median −0.25, t 0.53, block-bootstrap 90% range [−0.23, +0.57]; the best two trades (AKE 11-13, JCT 11-08, both closed on the wallet's default +90% take-profit, not the stated rule) are 238% of the total — without them −$259. burst_dip +0.027R, first half −0.30R, result is one AKE/IDOL afternoon. 23 coins, 59 coin-days for 82 trades; AKE alone 27% of burst_up. The "hold 24h" control sum is those two trades (ex-top-2 −$162; 58% of entries hit the 15% stop within 24h). The universe ("top-40 by 24h volume, refreshed daily") is filled by coins already pumping. Recommendation: burst_up measurement only to 120 trades, burst_dip stop or freeze; nothing to build on. Item 3 above was withdrawn on this.

## Addendum — the copies' profit floor (2026-11-17)

The NEAR short copy of 11-16 (prod, $23.7k at 7×) peaked +$618 (2.61% of notional), the lock that arms at 3% never armed, the leader held, and the copy printed −$944 before settling near −$400 — the same trade as the "sell in bits" question of 11-16. The copies' only own exits were the 4% stop and the 3% lock; a +2% move that retraces through the entry met nothing. Replaying prod's 44 and local's 148 copies on 5m bars under fourteen exit variants: a breakeven floor once the trade has been +2% up (stop to entry + costs) is +$441 on prod and +$565 on local over the live lock, cuts give-backs to a loss 3→1 and 6→2, and on prod changes no trade but NEAR. Every tighter arm or trail (1–2% arms, 1–1.5% breakevens, ½/1-ATR trails) is worse on at least one instance; "bits" ≈ the floor on local and beats it on prod only through NEAR. Built live as `be_at_pct` (copies carry 2%, `COPY_BE_PCT`); running copies take it on the next poll; the bits shadow inherits it.
