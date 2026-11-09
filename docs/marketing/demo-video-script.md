# Crypto Oracle — demo video brief and script

This document is written for an AI video generator (or a human editor working with one). It contains everything needed to produce a 2-minute product demo of Crypto Oracle: positioning, visual style, a scene-by-scene script with voice-over, on-screen text and the screenshot to use for each scene, plus a 30-second cut and strict rules about what the video may and may not claim.

The screenshots this script refers to are committed next to it in the repository, in `docs/marketing/screenshots/` (github.com/freemile2ru/trading-bot, branch `main`). If you received only this file, ask for that folder or for the `crypto-oracle-demo-video-brief.zip` archive, which is the same folder zipped. They are real captures of the app on 9 November 2026, dark theme, 1600 px wide. `*_full.png` files are full-page scrolls of the same tab for slow pan-down shots.

---

## 1. Product in one paragraph

Crypto Oracle is a self-hosted AI trading desk for crypto perpetuals (plus stock and index perps). It scans the market every few minutes, has Claude read the tape, the news and the macro calendar, and turns that into precise signals with entry, stop, targets and a written thesis. Around that core sit a dozen rule-based trading "books" — an autotrader, leader copy-trading across Binance, Hyperliquid and Lighter, an auction-levels system for stock perps, a prop-firm evaluation simulator — every one of them paper-traded first, judged by pre-registered rules, and reviewed by Claude after every trade. Nothing goes live until it has earned it.

**Tagline options** (pick one):
- "Every rule written down. Every trade judged."
- "An AI trading desk that has to prove itself first."
- "See the market the way a disciplined desk does."

**Audience:** self-directed crypto traders and quant-curious builders who are tired of black-box signal services and want to see the reasoning, the rules and the record.

**Tone:** calm, precise, confident, no hype. Think Bloomberg terminal meets a well-run research lab, not a "100x gains" ad.

---

## 2. Visual style guide

- **Format:** 16:9, 1920×1080 (4K optional). Length 110–120 s for the main cut, 30 s for the short cut.
- **Palette:** the app's own. Near-black navy background (#0b1120), white and grey text, electric-blue accents, green for longs/wins, red for shorts/losses, amber for warnings. Do not recolour the screenshots.
- **Motion:** slow push-ins (3–5 % over a scene), gentle pans down the `_full` captures, subtle parallax between a blurred background copy and the sharp foreground. Cuts on the beat, no flashy wipes. Occasional soft spotlight or rounded highlight box to draw the eye to one card.
- **Typography for titles:** a clean geometric sans (Inter, Söhne or similar), white, left-aligned, lower-third style. Keep on-screen text to one line where possible.
- **Music:** minimal electronic, 90–100 BPM, low pads with a light pulse. No drops, no vocals.
- **Voice:** one narrator, measured, mid-tempo, slightly warm. British or neutral American both fine. Roughly 2.5 words per second.
- **Logo:** the lightning bolt in a blue rounded square shown top-left of every screenshot. Use it for the opening and closing cards.

---

## 3. Rules the video must follow

1. **No performance claims.** Do not say or imply that the software makes money, beats the market, or has any win rate. The screenshots contain real paper-trading P&L figures, some negative. Either keep them small and unremarked, or blur them. Never show a cherry-picked green number as a result.
2. **Say "paper" where it is paper.** Every book shown is paper-traded. The Live tab connects to Binance *testnet* (test funds). Say so.
3. **Do not invent features.** Everything you need to describe is in section 4. If it is not there, it does not exist.
4. **Do not show or read out the email address, account id or API-key hints** on the Live tab (`live.png`). Blur the "account 96e56d75cb" strings and the API-key lines.
5. **Mandatory disclaimer card** at the end (section 4, scene 16).
6. The `trade.png` screenshot has an empty chart area because the chart widget does not render in a headless browser. Either crop to the order form and order book, or record that scene live in the app.

---

## 4. Main cut — scene by scene (≈115 s)

Timecodes are targets, not hard cuts. "VO" is voice-over. "Text" is the lower-third caption.

### Scene 1 · Cold open (0:00–0:06)
- **Visual:** black. The logo fades in. Then the headline bar from `all.png` (the clock, "Market: Normal · BTC 3.43% / 2.35% usual", "CPI release in 21h 42m", the red LIVE pill) slides in as a thin strip.
- **Text:** *Crypto Oracle*
- **VO:** "Most trading bots ask you to trust them. This one asks you to check."

### Scene 2 · The scanner (0:06–0:16)
- **Visual:** `all.png`. Start on the left column: the live scanner line ("best 10 tradeable of top 52 · watching 6 · next sweep 162s"), push in to the BUY ARB signal card with entry, stop, two take-profits and the three setup tags.
- **Text:** *Live scanner · 52 coins · re-checked every minute*
- **VO:** "Every few minutes Crypto Oracle sweeps the top perpetual markets, ranks them by conviction, and escalates the strongest to Claude. When a setup qualifies, you get a signal: entry, stop, two targets, and the setups that fired."

### Scene 3 · The reasoning (0:16–0:26)
- **Visual:** stay on `all.png`, pan down the signal card to the thesis paragraph and the amber risk list. Highlight box around the risks.
- **Text:** *The thesis — and what could break it*
- **VO:** "Every signal comes with a written thesis — and a list of what could go wrong. No arrows on a chart. Reasons you can argue with."

### Scene 4 · The Signal tab (0:26–0:38)
- **Visual:** `signal.png`. Left panel: SELL BTC card with stop, three take-profits, R:R, size, phase, volatility. Then slide right to the Signal Breakdown: technical / sentiment / news / macro bars, the four-timeframe table, key levels, volume, Fear & Greed, VIX.
- **Text:** *One coin, every angle: technical, sentiment, news, macro*
- **VO:** "Dig into any coin and you see the whole stack: four timeframes, pivot and Fibonacci levels, volume, sentiment, VIX and the macro tape, scored and blended into one call — valid for the next four hours, with a plain-English note on when not to chase it."

### Scene 5 · News desk (0:38–0:48)
- **Visual:** `news.png`. Pan down the "App headlines" column (edition #784). Then cut to the right panel "How are we feeling?" gauge at −0.34, "Leaning bearish", and Claude's verdict paragraph.
- **Text:** *18 sources · Claude writes the briefing around your positions*
- **VO:** "Eighteen news feeds, polled on their own schedules. Claude writes a short briefing every edition, tagged to the positions you actually hold, and keeps a running mood gauge with a verdict you can read."

### Scene 6 · Calendar and blackouts (0:48–0:55)
- **Visual:** `calendar.png`. Month view with CPI, FOMC and earnings markers; right panel "Next release 21h 40m" and the "What the books do" rules.
- **Text:** *FOMC, CPI, earnings — entries pause automatically*
- **VO:** "The calendar is not decoration. Around CPI and FOMC the books stop opening short-term trades. Around earnings the stock books stand down."

### Scene 7 · Trade terminal (0:55–1:02)
- **Visual:** `trade.png` cropped to the order book and the order form (or a live screen recording). Show the Paper / Live-testnet toggle, the leverage slider, the stop-loss field marked "req."
- **Text:** *Paper or Binance testnet · a stop is required on every order*
- **VO:** "Trade it yourself from the terminal — paper first, Binance testnet when you're ready. The stop-loss field isn't optional; it goes to the exchange with the order."

### Scene 8 · The autotrader and its lanes (1:02–1:12)
- **Visual:** `all.png` right column: the Autotrader panel (equity, win rate, closed/open, maker-entry stats) and the "Lanes" list below it with each lane's multiplier. Then `books.png` "Guards" card: longs/shorts gates, risk caps, the Claude review box.
- **Text:** *Autotrader lanes · direction gates · risk caps · Claude reviews after a losing streak*
- **VO:** "The autotrader runs several independent lanes, each sized by its own recent record. Guards sit above all of them: a direction is only traded while its last ten trades are net positive, open risk is capped, and after a losing streak Claude reviews the strategy and its changes expire on their own."

### Scene 9 · Copy trading (1:12–1:22)
- **Visual:** `copy.png`. Top row of stat cards (book equity, copies per day vs goal, leaders who trade crypto, second-look verdicts, too-late rule). Pan down to the leader table: venue pill, 90-day ROI curve, Sharpe, "their style" column, and the SHORT/LONG position chips with "NOT COPIED — leader on probation" labels.
- **Text:** *Mirror vetted leaders on Binance, Hyperliquid and Lighter*
- **VO:** "Copy trading with a selection bar: fifty-plus copiers, real AUM, ninety days of history, drawdown under thirty percent. Every leader position gets a second look from our own tape read and from Claude before it's mirrored — and a leader on a losing run goes on probation."

### Scene 10 · Mentor — auction levels (1:22–1:29)
- **Visual:** `mentor.png`. The QQQ and NVDA level ladders with "next target up / down", the condition chips (30M UP, MASTER UP, RE-ACCEL), and the "A LONG RIGHT NOW · grade 7/8 (A)" box.
- **Text:** *Stock & index perps · auction levels · graded entries*
- **VO:** "For stock and index perps there is a separate playbook: auction levels plotted live, every condition graded, and an entry only when the grade is high enough."

### Scene 11 · Prop-firm simulator (1:29–1:36)
- **Visual:** `prop.png`. The evaluation header, the daily-loss and drawdown bars with "halt" and "flatten" markers, open legs, the log.
- **Text:** *Run a prop-firm challenge on paper before you pay for one*
- **VO:** "Thinking about a funded account? The prop book runs the firm's exact rulebook — target, daily loss, drawdown, position limits — on paper, with its own tighter floors, so you know before you pay the fee."

### Scene 12 · Control data (1:36–1:44)
- **Visual:** `control.png`. The summary table: Actual vs Held 1h…48h, "after exit" columns, the "Early" row. Spotlight the dotted figures.
- **Text:** *Every closed trade replayed: what if we had held?*
- **VO:** "Every closed trade is replayed on its own price bars: what it would have made held one, four, twenty-four, forty-eight hours, and where the price went after we left. Exit rules get judged by evidence, not by feel."

### Scene 13 · History — what the trades teach (1:44–1:52)
- **Visual:** `history.png`. "How trades end" bars on the left, Claude's per-trade lessons on the right ("thesis wrong −0.02R", "clean win +0.31R").
- **Text:** *Claude debriefs trades and tags why they won or lost*
- **VO:** "After the fact, every trade is tagged — clean win, stopped then reversed, never worked — and Claude writes the lesson. The lessons feed back into the rules."

### Scene 14 · Lab and verdicts (1:52–1:59)
- **Visual:** `lab.png` header line: "Paper-only side experiments. Verdicts are fixed rules decided in advance: judged only after 40 closed trades…" then `markets.png` grid of book cards with ON/OFF switches.
- **Text:** *Pre-registered experiments · verdict after 40 trades · nothing promoted early*
- **VO:** "New ideas start in the Lab as paper books with a verdict rule written down in advance. Forty trades, then the numbers decide. Ideas that fail retire. Ideas that pass get capital."

### Scene 15 · Portfolio and Live (1:59–2:08)
- **Visual:** `portfolio.png` equity curve (keep the P&L tiles small or blurred), then `live.png` with the kill-switch button and the "Orders sent to Binance" log (blur account ids).
- **Text:** *One portfolio view · a kill switch that always works*
- **VO:** "Everything rolls up into one portfolio view. And when you do go live, there is a kill switch that closes every position — it works even when live trading is switched off."

### Scene 16 · Close + disclaimer (2:08–2:18)
- **Visual:** logo on black, tagline, then the disclaimer card.
- **Text (tagline):** *Crypto Oracle — every rule written down, every trade judged.*
- **Text (disclaimer, hold 4 s):** *Crypto Oracle is research software. All books shown are paper-traded; live mode connects to an exchange test environment. Nothing here is financial advice. Trading perpetual futures carries a risk of total loss.*
- **VO:** "Crypto Oracle. Every rule written down. Every trade judged."

---

## 5. 30-second cut

| Time | Screenshot | Text | VO |
|---|---|---|---|
| 0–3 | logo | Crypto Oracle | "An AI trading desk that has to prove itself first." |
| 3–9 | `all.png` signal card | Live scanner · entry, stop, targets, thesis | "It scans the market, writes the thesis, and lists what could go wrong." |
| 9–14 | `signal.png` breakdown | Technical · sentiment · news · macro | "Four timeframes, news, macro and sentiment, scored into one call." |
| 14–19 | `copy.png` leader table | Vetted leaders, second-looked | "Mirror vetted leaders — after a second look from Claude." |
| 19–24 | `control.png` | What if we had held? | "Every exit judged against what holding would have done." |
| 24–27 | `live.png` kill switch (blurred ids) | Paper first · testnet · kill switch | "Paper first. Testnet next. A kill switch always." |
| 27–30 | disclaimer | see scene 16 | — |

---

## 6. Asset manifest

| File | Tab | Best use |
|---|---|---|
| `all.png`, `all_full.png` | All Signals | Hero shot; signal card; autotrader panel and lanes on the right |
| `signal.png` | Signal | Deep single-coin analysis; breakdown bars; levels; sentiment |
| `news.png` | News | Claude-written headlines; mood gauge and verdict |
| `calendar.png` | Calendar | Macro/earnings calendar; blackout rules |
| `trade.png` | Trade | Order form and order book only (chart area is blank, see rule 6) |
| `trades.png` | Trades | Positions across every system; closed-trade filters |
| `history.png` | History | Exit tagging; Claude's per-trade lessons |
| `markets.png` | Markets | Strategy book cards with ON/OFF |
| `copy.png`, `copy_full.png` | Copy | Leader table, verdicts, second look |
| `mentor.png` | Mentor | Auction levels on QQQ/NVDA/TSLA/AMD, graded entries |
| `prop.png` | Prop | Prop-firm evaluation simulator |
| `control.png`, `control_full.png` | Control | Counterfactual replay table |
| `lab.png` | Lab | Pre-registered experiments; "Claude on its own" |
| `books.png` | Books | Guards: direction gates, risk caps, Claude review |
| `portfolio.png`, `portfolio_full.png` | Portfolio | Equity curve, edge and risk tiles (blur P&L if shown large) |
| `live.png` | Live | Testnet banner, kill switch, order log (blur account ids) |

---

## 7. Optional live-recorded moments

If the generator supports screen recordings or the editor can capture the app, these three interactions add motion the stills cannot:

1. **Scan now** on All Signals: click, the "next sweep" counter resets, the watchlist re-ranks.
2. **Refresh** on the Signal tab with BTC selected: the "Valid for" bar resets and the thesis re-renders (takes 30–60 s; speed up ×8).
3. **Switching a book off** on Books: the toggle flips, the card dims, the "16 of 16 on" counter drops to 15.

Record at 1440×1000 or larger, dark theme, cursor visible.
