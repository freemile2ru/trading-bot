import type { PaperTrade } from './oracle'

export type HistoryWallet = 'all' | 'main' | 'auto'

export interface HistoryFilters {
  wallet: HistoryWallet
  symbol?: string
  direction?: 'long' | 'short'
  outcome?: string
  result?: 'win' | 'loss'
  entry_type?: 'market' | 'limit'
  date_from?: number
  date_to?: number
  q?: string
  fa?: 'with' | 'neutral' | 'against' | 'unknown'
  sort: string
  order: 'asc' | 'desc'
  page: number
  page_size: number
}

export interface HistoryTrade extends PaperTrade {
  wallet: 'main' | 'auto'
  outcome: string
  entry_type: 'market' | 'limit'
  opened_ts: number | null
  closed_ts: number | null
  duration_minutes: number | null
  r_multiple: number | null
  fa_score?: number | null
  fa_bucket?: 'with' | 'neutral' | 'against' | 'unknown'
  fa_at_entry?: { score: number; label: string; rs_vs_btc_24h?: number; components?: Record<string, number> } | null
  exit_price: number
  risk_usd: number
  hold_duration?: string
  // Autotrader-only detail — absent on main-wallet trades.
  claude_confidence?: number | null
  claude_thesis?: string | null
  claude_entry?: number | null
  confirmed_by?: string | null
  entry_reasons?: string[]
  entry_score?: number
  conviction_at_entry?: number
  setups_at_entry?: string[]
  limit_price?: number
  exit_note?: string | null
  original_stop_loss?: number
  adjustments?: import('./signals').TradeAdjustment[]
  realized_pnl_usd?: number
  [key: string]: unknown
}

export interface BacktestPoint {
  ts: number | null
  pnl: number
  r: number
  drawdown: number
  drawdown_r: number
  trade: string | null
  trade_pnl?: number
  trade_r?: number | null
}

export interface BacktestStats {
  trades: number
  capital: number
  net_pnl: number
  return_pct: number
  net_r: number
  win_rate: number
  profit_factor: number | null
  expectancy_usd: number
  expectancy_r: number
  avg_win: number
  avg_loss: number
  best: number
  worst: number
  max_drawdown: number
  max_drawdown_pct: number
  max_drawdown_r: number
  avg_hold_minutes: number | null
  longest_win_streak: number
  longest_loss_streak: number
}

export interface HistoryResponse {
  rows: HistoryTrade[]
  total: number
  page: number
  pages: number
  page_size: number
  facets: { symbols: string[]; outcomes: string[]; entry_types: string[] }
  backtest: {
    curve: BacktestPoint[]
    stats: BacktestStats
    by_symbol: { symbol: string; trades: number; wins: number; pnl: number; r: number }[]
    outcomes: Record<string, number>
    by_fa: { bucket: string; trades: number; win_rate: number; avg_r: number | null; pnl: number }[]
  }
}

export interface ExitPolicyRow {
  policy: string
  live: boolean
  trades: number
  avg_r?: number
  total_r?: number
  win_rate?: number
  avg_hold_h?: number
  exits?: Record<string, number>
}

export interface CalibrationStats {
  trades: number
  avg_r?: number
  win_rate?: number
  total_r?: number
}

export interface ExitComparison {
  policies: ExitPolicyRow[]
  calibration: {
    policy: string
    by_verdict: { approved: CalibrationStats; declined: CalibrationStats }
    by_confidence: (CalibrationStats & { band: string })[]
  }
  interventions: { exit: string; trades: number; avg_delta_r: number; total_delta_r: number }[]
  open_shadows: number
  trades_compared: number
  descriptions: Record<string, string>
}

// ── Insights (GET /api/history/insights) ──────────────────────────────────────

export interface InsightGroup {
  key: string
  trades: number
  win_rate: number
  avg_r: number | null
  pnl: number
}

export interface TradeStory {
  id: string
  wallet: string
  symbol: string
  direction: 'long' | 'short'
  entry_price: number
  exit_price: number | null
  pnl_usd: number
  r_multiple: number | null
  close_reason: string | null
  opened_ts: number | null
  closed_ts: number | null
  duration_minutes: number | null
  claude_thesis: string | null
  oracle_reasoning: string | null
  exit_note: string | null
  setups_at_entry: string[] | null
  confirmed_by: string | null
  claude_confidence: number | null
  entry_type: string
  outcome: string
}

export interface HistoryInsights {
  trades: number
  by_setup: InsightGroup[]
  by_conviction: InsightGroup[]
  by_source: InsightGroup[]
  by_fa: InsightGroup[]
  by_entry_type: InsightGroup[]
  by_armed: InsightGroup[]
  by_timeframe: InsightGroup[]
  by_regime: InsightGroup[]
  best: TradeStory[]
  worst: TradeStory[]
}
