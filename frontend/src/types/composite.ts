/** The one book: the programme's short slices with a record as a single $50k system (experiments/composite.py). */
export type CompositePhase = 'funding' | 'running' | 'stopped'

export interface CompositeAssess {
  action: 'ok' | 'halt' | 'flatten'
  why: string | null
  daily_loss_pct: number
  dd_pct: number
  daily_used_pct: number
  dd_used_pct: number
  room_today_usd: number
  room_total_usd: number
  floors: { halt_daily: number; flatten_daily: number; halt_dd: number; flatten_dd: number }
}

export interface CompositeOpen {
  id: string; symbol: string; direction: 'long' | 'short'; source: string | null
  notional: number | null; risk_usd: number | null; entry: number; stop: number | null; pnl_usd: number | null; opened_at: string | null
  bound: string | null
}

export interface CompositeDaily {
  days: { day: string; pnl: number }[]
  trading_days: number; cal_days: number
  net: number; per_cal_day: number; per_trading_day: number; median_day: number
  pos_days_pct: number; worst_day: number; best_day: number; max_dd: number
  t: number | null
}

export interface CompositeStatus {
  label: string
  size: number
  phase: CompositePhase
  started_at: number
  days: number
  balance: number; equity: number; open_pnl: number; sod_balance: number; today: number
  min_equity: number | null; max_equity: number | null
  stopped: { ts: number; why: string; equity: number } | null
  assess: CompositeAssess
  halted_day: string | null; halt_why: string | null; halts: number; flattens: number
  target: { low: number; high: number; per_cal_day: number; progress_pct: number }
  daily: CompositeDaily
  verdict: { status: 'collecting' | 'promote' | 'retire' | 'stopped'; note: string; legs: number; days: number }
  rules: {
    risk_pct: number; notional_cap_pct: number; gross_cap_pct: number; max_legs: number; leverage: number; hold_h: number; trail_atr: number
    halt_daily_pct: number; flatten_daily_pct: number; halt_dd_pct: number; flatten_dd_pct: number; sides: string[]; sources: Record<string, string>
    verdict: { legs: number; days: number; promote_usd_day: number; retire_legs: number }
    risk_usd: number; notional_cap_usd: number
  }
  open: CompositeOpen[]
  closed_n: number; closed_pnl: number; wins: number
  by_source: Record<string, { n: number; pnl: number; wins: number; mean_r: number | null; open: number; book: string }>
  recent: { symbol: string; direction: string; source: string | null; pnl_usd: number | null; risk_usd: number | null; close_reason: string | null; closed_at: string | null }[]
  log: Record<string, unknown>[]
}
