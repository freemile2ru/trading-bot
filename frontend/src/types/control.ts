/** Control data (experiments/control.py): every closed trade of a book replayed under plain controls. */
export interface ControlAgg {
  n: number
  actual?: number
  wins?: number
  held?: Record<string, number>
  after_exit_avg_pct?: Record<string, number>
  best_4h_avg_pct?: number
  worst_4h_avg_pct?: number
  stopped_share_pct?: number
  avg_hold_h?: number
}

export interface ControlRow {
  trade_id: string; symbol: string; direction: 'long' | 'short'; opened_ts: number; closed_ts: number
  close_reason: string | null; hold_rule: string | null; lane: string | null; notional: number | null
  actual: number; hold_h: number; stopped_at_h: number | null; bars: number
  held: Record<string, number | null>; best_pct: Record<string, number>; worst_pct: Record<string, number>
  after_exit_pct: Record<string, number | null>
}

export interface ControlReport {
  book: string
  horizon_h: number
  control_h: number
  horizons: string[]
  after_exit: string[]
  all: ControlAgg
  early: ControlAgg
  by_reason: Record<string, ControlAgg>
  rows: ControlRow[]
  books: string[]
  pending: number
}
