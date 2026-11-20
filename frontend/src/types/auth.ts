/** Multi-tenant auth, tiers and billing — the shapes in docs/saas.md ("API contract"). */

export type TierId = 'free' | 'pro' | 'elite'
export type Role = 'admin' | 'owner' | 'member'
export type TenantStatus = 'active' | 'trialing' | 'past_due' | 'canceled'
export type ExchangeVenue = 'binance' | 'hyperliquid'

export interface SessionUser {
  id: string
  email: string
  name: string
  picture: string | null
  role: Role
}

export interface SessionTenant {
  id: string
  name: string
  tier: TierId
  status: TenantStatus
  current_period_end: number | null
  cancel_at_period_end: boolean
}

export interface Limits {
  paper_books: number | null
  paper_capital_usd: number
  alert_channels: number
  signal_delay_min: number
  api_rpm: number
}

export interface Session {
  user: SessionUser
  tenant: SessionTenant
  /** The feature keys the tier includes (docs/saas.md "Feature keys"). */
  features: string[]
  limits: Limits
}

export interface AuthConfig {
  google_client_id: string | null
  dev_login: boolean
  auth_required: boolean
  /** The tenant-engines flag: Elite tenants may run every engine on their own account (docs/saas.md). */
  tenant_engines?: boolean
}

export type EngineMode = 'mirror' | 'own'

export interface PricingTier {
  id: TierId
  name: string
  price_usd_month: number
  tagline: string
  features: string[]
  limits: Limits
  highlight: boolean
}

export type FeatureGroup = 'Signals' | 'Trading' | 'Books' | 'Alerts' | 'Pro tools' | 'Access'

export interface FeatureRow {
  key: string
  label: string
  group: FeatureGroup
  note?: string
}

export interface Pricing {
  tiers: PricingTier[]
  features: FeatureRow[]
  current_tier: string | null
  /** Stripe is configured — checkout and the portal work. */
  billing: boolean
}

export interface BillingStatus {
  configured: boolean
  tier: TierId
  status: TenantStatus
  current_period_end: number | null
  cancel_at_period_end: boolean
  stripe_customer: boolean
}

export interface Settings {
  risk_x: number
  paper_capital_usd: number
  books: Record<string, boolean>
  allowed_books: string[]
  exchange_keys: Record<string, { configured: boolean; label: string }>
  /** "mirror": the engines' entries copied into your wallets; "own": the engines run on your account (flag + Elite). */
  engine_mode?: EngineMode
  engine_modes_allowed?: EngineMode[]
}

export interface SettingsPatch {
  risk_x?: number
  paper_capital_usd?: number
  books?: Record<string, boolean>
  engine_mode?: EngineMode
}

export interface AdminTenant {
  id: string
  name: string
  tier: TierId
  status: TenantStatus
  owner_email: string
  users: number
  created_ts: number
  stripe_customer: boolean
  equity_usd: number | null
}

export interface RunnerStatus {
  tenants_active: number
  mirrored_24h: number
  last_tick: number | null
  errors: unknown[]
}
