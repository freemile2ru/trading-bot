import type { PaperTrade } from './oracle'

export interface AuctionLevel { name: string; label: string; price: number; bps: number }
export interface AuctionGrade { score: number; stand_down: boolean; reasons: string[] }
export interface AuctionContext {
  trend15: number; trend30: number; indecision30: boolean; master: number; bias: number; bias_kind: string; healthy: boolean; lost9: boolean
  divergence: boolean; pullback: boolean; caution: boolean; reaccel: boolean; vol_pressure: number; atr_low: boolean
}
export interface AuctionPending { direction: 'long' | 'short'; level: number; level_name: string; stop: number; target: number; target_name: string; accept_ts: number }
export interface AuctionInstrument {
  symbol: string; error?: string
  price: number; bar_ts: number; et: string; in_session: boolean; orb_valid: boolean; ib_valid: boolean
  vwap: number; ema20: number; ema50: number; ema200: number; atr_pct: number | null
  levels: AuctionLevel[]; next_above: { name: string; price: number }; next_below: { name: string; price: number }
  context: AuctionContext; grade_long: AuctionGrade; grade_short: AuctionGrade
  state: { pending?: AuctionPending | null; entering?: (AuctionPending & { grade: number; target_bps: number; waited?: number }) | null }
  gate: string | null
}
export interface AuctionLog { ts: number; sym: string; event: string; why?: string; level?: string; grade?: number; direction?: string; target?: string; target_bps?: number; notional?: number }
export interface AuctionSnapshot {
  at: number
  daily: { goal: [number, number]; today: number; avg7: number; days: Record<string, number> }
  book: { equity: number; initial: number; balance: number; return_pct: number | null; open: (PaperTrade & Record<string, unknown>)[]; closed: number; wins: number
          net_pnl: number; avg_r: number | null; enabled: boolean; equity_history: { ts: number; equity: number }[] }
  verdict: { status: 'collecting' | 'edge' | 'no_edge' | 'inconclusive'; trades: number; needed: number; expectancy_usd: number | null; ci: [number | null, number | null] }
  config: { symbols: string[]; variant: Record<string, string | number>; risk_pct: number; max_gross: number; max_open: number; max_leverage: number; session: string
            buffer_bps: number; stop_offset_bps: number; retest_bars: number }
  instruments: AuctionInstrument[]
  log: AuctionLog[]
  replay: { verdict: string; variant_verdict: string; variants: Record<string, { trades: number; binance: number | null; futures: number | null; halves_futures: [number | null, number | null] }> } | null
  level_labels: Record<string, string>
}

export interface AuctionChartData {
  symbol: string; error?: string; at: number; bar_s: number
  bars: { t: number; o: number; h: number; l: number; c: number; v: number; session: boolean }[]
  lines: Record<string, (number | null)[]>
  levels: Record<string, (number | null)[]>
  current_levels: { name: string; label: string; price: number }[]
  study_events: { t: number; kind: 'accept' | 'confirm' | 'entry' | 'exit'; direction: 'long' | 'short'; level?: string; price?: number; stop?: number; target?: number
                  target_name?: string; grade?: number; taken?: boolean; reason?: string; pnl_pct?: number | null; r?: number | null }[]
  trades: { id: string; direction: 'long' | 'short'; entry_ts: number | null; exit_ts: number | null; entry: number; exit: number | null; stop: number | null; target: number | null
            level?: string | null; target_name?: string | null; grade?: number | null; reason?: string | null; pnl_usd?: number | null; status: string }[]
  events_error?: string
}
