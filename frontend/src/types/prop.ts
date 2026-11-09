/** The prop book: the reversal lane and the auction book under Propr's Classic 1-Step $25K rules (experiments/prop.py). */
export type PropPhase = 'funding' | 'evaluation' | 'funded' | 'stopped'

export interface PropAssess {
  breach: 'max_daily_loss' | 'max_drawdown' | null
  action: 'ok' | 'halt' | 'flatten'
  why: string | null
  daily_loss_pct: number
  dd_pct: number
  daily_used_pct: number
  dd_used_pct: number
  room_today_usd: number
  room_total_usd: number
  floors: { daily: number; max: number; halt_daily: number; flatten_daily: number; halt_dd: number; flatten_dd: number }
}

export interface PropOpen {
  id: string; symbol: string; coin: string | null; direction: 'long' | 'short'; source: 'reversal' | 'auction' | null
  notional: number | null; leverage: number | null; entry: number; stop: number | null; pnl_usd: number | null; opened_at: string | null
  exec: { opened?: number; closed?: number; orders?: string[]; mode?: string; error?: string; tries?: number } | null
}

export interface PropStatus {
  plan: { firm: string; name: string; size: number; fee_usd: number; target_pct: number; daily_loss_pct: number; max_dd_pct: number
          dd_mode: string; max_open: number; profit_split: number; reset_utc: string; source: string }
  attempt: number
  phase: PropPhase
  started_at: number
  days: number
  passed_at: number | null
  breach: { ts: number; rule: string; equity: number } | null
  stopped: { ts: number; why: string; equity: number } | null
  balance: number; equity: number; open_pnl: number; realised: number
  sod_balance: number; min_equity: number | null; max_equity: number | null
  target: { usd: number; progress_pct: number; to_go_usd: number }
  assess: PropAssess
  halted_day: string | null; halt_why: string | null; halts: number; flattens: number
  ours: { halt_daily_pct: number; flatten_daily_pct: number; halt_dd_pct: number; flatten_dd_pct: number
          risk_pct: Record<string, number>; max_open_by_source: Record<string, number> }
  leverage: { default: number; overrides: Record<string, number> }
  open: PropOpen[]
  closed_n: number; closed_pnl: number; wins: number
  by_source: Record<string, { n: number; pnl: number; wins: number; open: number; cap: number; risk_pct: number }>
  recent: { symbol: string; direction: string; source: string | null; pnl_usd: number | null; close_reason: string | null; closed_at: string | null }[]
  log: Record<string, any>[]
  attempts: { attempt: number; started_at: number; ended_at: number; phase: string; balance: number; breach: unknown; note: string }[]
  exec: { mode: string; enabled: boolean; url: string | null; key_set: boolean }
}
