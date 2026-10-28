import type { PaperTrade } from './oracle'

export type MarketKey = 'crypto' | 'stocks' | 'futures'

export interface MarketBook {
  name: string; kind: 'trend' | 'rules' | 'claude'; label: string; enabled: boolean
  equity: number; initial: number; balance: number; return_pct: number | null
  open: (PaperTrade & { claude_verdict?: string; exit_policy?: string; pnl_usd: number })[]
  closed: number; wins: number; avg_r: number | null; net_pnl: number
  recent_closed: (PaperTrade & { pnl_usd: number; close_reason?: string; closed_at?: string })[]
}

export interface MarketScan {
  symbol: string; base: string; price: number; conviction: number; confidence: number
  best_timeframe: string | null; scanned_at: number
  rules?: { direction: 'long' | 'short' | null; reasons: string[]; score?: number; enter?: boolean; timing?: string[]; claude?: string }
}

export interface MarketSummary {
  market: 'stocks' | 'futures'; label: string; symbols: string[]; books: MarketBook[]; scans: MarketScan[]
  last_sweep: number | null; sweep_s: number; claude_calls_today: number; claude_daily: number
  last_trend: string | null; enabled: boolean
  picks: { at: number; picks: ClaudePick[] } | null; picks_per_day: number; pick_hour_utc: number
}
export interface ClaudePick {
  symbol: string; base: string; conviction: number; action: string; confidence: number; entered: boolean; at: number; thesis: string
}
