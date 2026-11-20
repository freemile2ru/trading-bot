"""Multi-user accounts, subscription tiers and per-tenant execution (2026-11-20). See docs/saas.md.

  context    who is asking: the tenant and user of the current request (contextvars); engines run as the owner
  tiers      the catalogue — Free / Pro / Elite, prices, features, limits — the one source for gates and the Plans tab
  accounts   users, tenants, settings, encrypted exchange keys, billing events (accounts.db)
  sessions   the signed session cookie
  google     Google sign-in: verifying an ID token
  gate       feature gates and the route policy for subscriber accounts
  middleware resolves the session on every request and applies the policy
  billing    Stripe Checkout, the customer portal and the webhook
  wallets    a tenant's own paper wallets, one per book
  runner     mirrors the engines' entries into subscribed tenants' wallets and manages their legs
"""
