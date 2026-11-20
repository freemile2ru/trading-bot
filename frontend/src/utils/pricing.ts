import type { FeatureGroup, FeatureRow, Pricing, PricingTier, TierId } from '../types/auth'

/** The order the contract lists the tiers and the comparison groups in. */
export const TIER_ORDER: TierId[] = ['free', 'pro', 'elite']
export const GROUP_ORDER: FeatureGroup[] = ['Signals', 'Trading', 'Books', 'Alerts', 'Pro tools', 'Access']

/** Tier badge colours: free gray, pro blue, elite amber. */
export const TIER_CLS: Record<TierId, string> = {
  free: 'bg-gray-800 text-gray-300 border-gray-700',
  pro: 'bg-blue-900/50 text-blue-300 border-blue-800',
  elite: 'bg-amber-900/50 text-amber-300 border-amber-800',
}

export const fmtPrice = (usd: number) => `$${usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}`

const FREE_FEATURES = ['signals.delayed', 'news', 'calendar']
const PRO_FEATURES = ['signals.live', 'news', 'calendar', 'books', 'trades', 'markets', 'paper.mirror', 'alerts.email', 'alerts.whatsapp', 'copy.view']
const ELITE_FEATURES = [...PRO_FEATURES, 'alerts.webhook', 'mentor', 'prop', 'one', 'control', 'lab', 'retired', 'claude.second_look', 'mirror.exchange', 'api.access']

const FALLBACK_TIERS: PricingTier[] = [
  {
    id: 'free', name: 'Free', price_usd_month: 0, highlight: false,
    tagline: 'The signals 15 minutes late, the news and the calendar.',
    features: FREE_FEATURES,
    limits: { paper_books: 0, paper_capital_usd: 0, alert_channels: 0, signal_delay_min: 15, api_rpm: 0 },
  },
  {
    id: 'pro', name: 'Pro', price_usd_month: 49, highlight: true,
    tagline: 'Live signals, your own paper books mirroring the engines, alerts.',
    features: PRO_FEATURES,
    limits: { paper_books: 5, paper_capital_usd: 10_000, alert_channels: 2, signal_delay_min: 0, api_rpm: 0 },
  },
  {
    id: 'elite', name: 'Elite', price_usd_month: 199, highlight: false,
    tagline: 'Every book, the research tools, Claude second looks and your own exchange keys.',
    features: ELITE_FEATURES,
    limits: { paper_books: null, paper_capital_usd: 50_000, alert_channels: 3, signal_delay_min: 0, api_rpm: 60 },
  },
]

const FALLBACK_ROWS: FeatureRow[] = [
  { key: 'signals.delayed', label: 'Signals, 15 minutes delayed', group: 'Signals' },
  { key: 'signals.live', label: 'Live signals with the stream', group: 'Signals' },
  { key: 'news', label: 'News', group: 'Trading' },
  { key: 'calendar', label: 'Calendar and market state', group: 'Trading' },
  { key: 'trades', label: 'Trades, History, Portfolio', group: 'Trading' },
  { key: 'markets', label: 'Markets', group: 'Trading' },
  { key: 'books', label: 'Books', group: 'Books' },
  { key: 'paper.mirror', label: 'Own paper books mirroring the engines', group: 'Books', note: 'Pro: up to 5 books, $10k paper capital · Elite: every book, $50k' },
  { key: 'alerts.email', label: 'Email alerts', group: 'Alerts' },
  { key: 'alerts.whatsapp', label: 'WhatsApp alerts', group: 'Alerts' },
  { key: 'alerts.webhook', label: 'Webhook alerts', group: 'Alerts' },
  { key: 'copy.view', label: 'Copy board (view)', group: 'Pro tools' },
  { key: 'mentor', label: 'Mentor (auction book)', group: 'Pro tools' },
  { key: 'prop', label: 'Prop book', group: 'Pro tools' },
  { key: 'one', label: 'One book (composite)', group: 'Pro tools' },
  { key: 'control', label: 'Control replays', group: 'Pro tools' },
  { key: 'lab', label: 'Lab', group: 'Pro tools' },
  { key: 'retired', label: 'Retired experiments', group: 'Pro tools' },
  { key: 'claude.second_look', label: 'Claude second looks on your legs', group: 'Pro tools' },
  { key: 'mirror.exchange', label: 'Mirroring to your own exchange keys', group: 'Access', note: 'Keys stored; execution next round' },
  { key: 'api.access', label: 'API access', group: 'Access', note: '60 requests / minute' },
]

/** docs/saas.md's numbers — used only when GET /api/pricing fails. */
export const FALLBACK_PRICING: Pricing = { tiers: FALLBACK_TIERS, features: FALLBACK_ROWS, current_tier: null, billing: false }

export const tierRank = (t: string | null | undefined) => (t ? TIER_ORDER.indexOf(t as TierId) : -1)
