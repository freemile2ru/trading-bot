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

/** Index shadow (experiments/index_shadow.py): a signal scored on the picked coin, the alt index and BTC+ETH, by episode. */
export interface ShadowStat { n: number; mean_pct?: number; t?: number | null; pos_pct?: number }
export interface ShadowBlock { episodes: number; actual_pct: ShadowStat; coin_pct: ShadowStat; index_pct: ShadowStat; majors_pct: ShadowStat }
export interface ShadowVerdict {
  verdict: 'measuring' | 'index better' | 'coin better' | 'no difference'
  episodes: number
  needed?: number
  index_minus_coin?: ShadowStat & { halves_pct: number[] }
  timing_vs_baseline?: ShadowStat & { halves_pct: number[]; real: boolean }
}
export interface ShadowSide {
  trades: number
  all: ShadowBlock
  forward: ShadowBlock
  baseline: { mean_pct: number; n_starts: number; hold_h: number } | null
  verdict: ShadowVerdict
}
export interface IndexShadowReport {
  signal: string; book: string; since: number; verdict_episodes: number; min_gap_pct: number; fee_rt_pct: number
  index: string[]; sides: Partial<Record<'long' | 'short', ShadowSide>>; pending: number
}
