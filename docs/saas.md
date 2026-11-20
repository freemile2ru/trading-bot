# Crypto Oracle as a subscription product (2026-11-20)

The user: "Assuming I want to monetize this software, refactor it to support multi-user features and multiple
subscription tiers according to offered services. Create a tab listing each tier and its features along with price."
Chosen up front: full multi-tenant (each user has their own books and wallets), tiers Free / Pro $49 / Elite $199 a
month, Google sign-in only, Stripe Checkout + webhooks.

## Architecture

Market signals are the same for everyone: the scanner, the oracle, the lanes, the copy board and the labs read one
market. What differs per user is capital, settings, exchange keys and what their tier lets them see and run. So:

* **Shared signal engines.** The scanner, autotrader lanes, books and labs keep running once, as the *owner* tenant
  (the instance's own data, unchanged — nothing is migrated or orphaned).
* **Per-tenant execution.** Every entry an engine book opens is published as an event; the tenant runner
  (`tenancy/runner.py`) mirrors it into each subscribed tenant's own paper wallet for that book, sized by risk parity
  (the same share of equity at the stop as the source leg, times the tenant's risk multiplier), and closes the mirror
  when the source closes. Stops, targets and trails on the tenant's legs are managed by `trading/monitor.run` on the
  tenant's wallet every minute. Tenant wallets live in `data/tenants/<tenant>/swing_trading.db` (a schema of their own
  on Postgres).
* **Per-tenant state.** `storage.state.key()` prefixes every key with the tenant while a request runs for one, so the
  book switches, capital plan and settings a tenant edits are theirs alone. Engine loops run with no tenant set and
  therefore read and write the owner's state exactly as before.
* **Tiers gate features**, not code paths: `tenancy/tiers.py` is the one catalogue (price, features, limits), the API
  gates with `tenancy.gate.require(feature)`, the frontend hides or locks tabs by the same feature keys.
* **Billing** is Stripe Checkout and the customer portal, hosted by Stripe; the webhook sets the tenant's tier. Without
  Stripe keys the Pricing tab still lists the tiers and an admin assigns tiers by hand.

Not in this round (named so they are not mistaken for done): Elite mirroring to the tenant's own exchange keys places
no real orders yet — keys are stored (encrypted at rest) and the gate exists, but `live/` is built around one account
and becomes per-tenant in the next round; Hyperliquid copy legs and the carry books (not PaperWallet-based) are not
mirrored; rate-limited API keys for the "API access" feature issue tokens but have no per-key quota yet.

## Tiers

| | Free $0 | Pro $49 / mo | Elite $199 / mo |
|---|---|---|---|
| Signals | 15 minutes delayed | live, with the stream | live |
| News, calendar, market state | yes | yes | yes |
| Books, Trades, History, Portfolio, Markets | — | yes | yes |
| Own paper books mirroring the engines | — | up to 5 books, $10k paper capital | every book, $50k paper capital |
| Alerts | — | email + WhatsApp | email + WhatsApp + webhook |
| Copy board | — | view | view |
| Mentor, Prop, One book, Control, Lab, Retired | — | — | yes |
| Claude second looks on your legs | — | — | yes |
| Mirroring to your own exchange keys | — | — | yes (keys stored; execution next round) |
| API access | — | — | 60 requests / minute |

Feature keys (what the gates and the tabs use): `signals.delayed`, `signals.live`, `news`, `calendar`, `books`,
`trades`, `markets`, `paper.mirror`, `alerts.email`, `alerts.whatsapp`, `alerts.webhook`, `copy.view`, `mentor`,
`prop`, `one`, `control`, `lab`, `retired`, `claude.second_look`, `mirror.exchange`, `api.access`.

Tab → feature: all/signal → `signals.delayed` (free) / `signals.live` (pro); news → `news`; calendar → `calendar`;
trade, trades, history, portfolio, books → `trades`; markets → `markets`; copy → `copy.view`; mentor → `mentor`;
prop → `prop`; one → `one`; control → `control`; lab → `lab`; retired → `retired`; live → `mirror.exchange`;
pricing → everyone; admin → role admin.

## API contract

All responses JSON. Sessions are an HttpOnly cookie `oracle_session` (set by the auth routes; same origin, so
`fetch` needs nothing). A gated route the tenant's tier does not include returns **402** `{"detail": "...",
"feature": "<key>", "upgrade": "pro"|"elite"}`; no session where one is required returns **401**.

```
GET  /api/auth/config        → {google_client_id: string|null, dev_login: bool, auth_required: bool}
POST /api/auth/google        {credential: <Google ID token>} → Session
POST /api/auth/dev-login     {email: string, name?: string} → Session        (local only: dev_login true)
POST /api/auth/logout        → {ok: true}
GET  /api/auth/me            → Session   (401 when auth is required and there is no session;
                                          the owner session when auth is optional, i.e. the local instance)

Session = {
  user:   {id, email, name, picture: string|null, role: 'admin'|'owner'|'member'},
  tenant: {id, name, tier: 'free'|'pro'|'elite', status: 'active'|'trialing'|'past_due'|'canceled',
           current_period_end: number|null, cancel_at_period_end: bool},
  features: string[],                       // the feature keys the tier includes
  limits: {paper_books: number|null, paper_capital_usd: number, alert_channels: number,
           signal_delay_min: number, api_rpm: number},
}

GET  /api/pricing            → {tiers: Tier[], features: FeatureRow[], current_tier: string|null, billing: bool}
Tier = {id, name, price_usd_month: number, tagline: string, features: string[], limits: Limits, highlight: bool}
FeatureRow = {key, label, group: 'Signals'|'Trading'|'Books'|'Alerts'|'Pro tools'|'Access', note?: string}

POST /api/billing/checkout   {tier: 'pro'|'elite'} → {url}       (503 when Stripe is not configured)
POST /api/billing/portal     → {url}                              (503 when not configured / no customer)
GET  /api/billing/status     → {configured: bool, tier, status, current_period_end, cancel_at_period_end,
                                 stripe_customer: bool}
POST /api/billing/webhook    (Stripe; signature header Stripe-Signature)

GET  /api/me/settings        → Settings
POST /api/me/settings        {risk_x?: number, paper_capital_usd?: number, books?: {[name]: bool}} → Settings
Settings = {risk_x: number, paper_capital_usd: number, books: {[name]: bool}, allowed_books: string[],
            exchange_keys: {[venue]: {configured: bool, label: string}}}
POST /api/me/exchange-keys   {venue: 'binance'|'hyperliquid', api_key, api_secret} → {ok: true}   (elite)
DELETE /api/me/exchange-keys/{venue} → {ok: true}

GET  /api/admin/tenants      → {tenants: [{id, name, tier, status, owner_email, users: number, created_ts,
                                            stripe_customer: bool, equity_usd: number|null}]}
POST /api/admin/tenants/{id}/tier   {tier} → tenant
GET  /api/admin/runner       → {tenants_active: number, mirrored_24h: number, last_tick: number|null, errors: [...]}
```

Tenant-scoped data for a signed-in tenant comes from the **existing** routes: `/api/trades/all`, `/api/portfolio/*`,
`/api/books`, `/api/paper/*` and the position routes all read the tenant's wallets and state when a tenant session is
present (the owner session, or none locally, reads the instance's own data as before).

## Environment

```
SESSION_SECRET          signs the session cookie (required in production)
GOOGLE_CLIENT_ID        the Google OAuth web client; unset locally → dev-login enabled (never in production)
OWNER_EMAILS            comma-separated: these Google accounts are admins on the owner tenant
AUTH_REQUIRED           default true only in production with GOOGLE_CLIENT_ID set, false otherwise (no session → the owner's data)
STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_PRO, STRIPE_PRICE_ELITE
APP_URL                 where Stripe returns to (https://freemileoracle.com)
TENANT_RUNNER           default true: mirror engine entries into subscribed tenants' wallets
```

## Choices made while building (2026-11-20)

* The local instance without a sign-in is the owner and an admin (`require_admin` lets it through; production requires
  a session, so this is reachable only locally). The first local dev-login becomes the admin when OWNER_EMAILS is empty.
* A subscriber's "auto" book switch is a plain switch under their prefix; only the owner's toggles the autotrader's pause.
* Frontend: `has()` returns every feature for admins and for the owner view without a session; a paid tenant changing
  to the other paid tier is sent to the Stripe portal, not a second Checkout; the signed-out Pricing view is reachable
  from the sign-in page at `#pricing`.
* `/api/trades/all` for a subscriber returns their own wallets' rows only (`overview.all_rows` returns early for a
  tenant after their swing and book wallets); the owner's scalper, pair, markets and live ledgers are never theirs.

## Tenant engines, behind a flag (2026-11-20, the user: "build a multitenant feature for all our systems but put behind a feature flag")

`TENANT_ENGINES=true` turns on the tenant engine host (`tenancy/engines.py`). An Elite tenant may then set
`engine_mode: own` (Settings → "How your books run"; feature key `engines.own`), and every book they switch on runs
FOR THEM instead of being mirrored: the same engine code, called once per opted-in tenant inside that tenant's context.

What makes that possible without rewriting the engines:

* **The kv store scopes keys at access time** (`storage/state.py`: `_scope`). Inside a tenant's context every
  `state:` key — including the engines' import-time constants such as the autotrader's pause and per-symbol states —
  reads and writes `state:t:<tenant>:…`. Market reads and the one LLM budget stay shared (`SHARED_PREFIXES`, env
  `TENANT_SHARED_STATE`).
* **The wallet globals are proxies** (`tenancy/proxies.py`): `autotrader.engine.auto_wallet`, `experiments.books.WALLETS`
  and `markets.engine.WALLETS` resolve to the owner's objects in the owner context and to the tenant's own wallets
  (`data/tenants/<id>/swing_trading.db`) inside a tenant's.
* **The host** runs beside the owner's loops: every minute it manages each opted-in tenant's open legs and calls the
  ticks of the books they have on (copy, auction, prop, composite, burst, cascade, weekly_rs, reversal_index, unlocks,
  events, the autotrader's monitor), and after every scanner sweep it calls the autotrader's entry decision once per
  tenant that runs it. One tenant's failure is logged and contained. The mirror runner skips opted-in tenants.
* Status: `GET /api/admin/runner` → `engines` (flag, who runs, per-tenant last tick, errors).

Known limits (stated, not hidden): module-level in-memory caches inside the engines are shared across tenants (kv and
wallets are not), so two tenants' runs of the same engine in the same minute may read each other's transient cache;
Claude-reviewing engines draw on the one shared LLM budget, so cost scales with opted-in tenants; the carry books (own
SQLite ledger) and the stocks/futures books are not run per tenant; per-tenant *parameters* beyond switches, watchlist
and capital (e.g. a different risk % or stop rule) still read the owner's env defaults. The flag is off by default and
off in production until these are judged acceptable.
