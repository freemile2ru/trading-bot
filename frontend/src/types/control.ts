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
  e_ratio?: Record<string, number | null>
}

/** Entry cells (experiments/entry_cells.py): the conditions at each crypto entry, scored forward. */
export interface CellStat { n: number; mean_pct?: number; t?: number | null; pos_pct?: number; sum_pct?: number }
export interface CellVerdict { verdict: string; have?: number; needed?: number; gap_pct?: number; t?: number | null; halves_pct?: number[] }
export interface EntryCellsReport {
  since: number; verdict_n: number; min_gap_pct: number; books: string[]; recorded: number; pending: number
  long: {
    gate: { funding_max_pct: number; btc24_max_pct: number; pre4_max_pct: number; hours_off: number[] }
    all: { kept: CellStat; rejected: CellStat; by_flag: Record<string, CellStat> }
    forward: { kept: CellStat; rejected: CellStat; by_flag: Record<string, CellStat> }
    verdict: CellVerdict
  }
  short_cells: Record<string, { label: string; all: { in: CellStat; out: CellStat }; forward: { in: CellStat; out: CellStat }; verdict: CellVerdict }>
  reversal_long_shadow: {
    hold_h: number; written: number; settled: number; coin: CellStat; hedged: CellStat
    forward: { n: number; hedged: CellStat; halves_pct: (number | null)[] }; verdict: string; needed: number
    recent: { symbol: string; score: number; opened_ts: number; price: number; result: { coin_pct: number; btc_pct: number; hedged_pct: number } | null }[]
  }
  recent: { trade_id: string; book: string; lane: string | null; symbol: string; direction: 'long' | 'short'; opened_ts: number; actual_pct: number
    fund_pct: number | null; btc24_pct: number | null; pre4_pct: number | null; hour: number; flags?: string[]; kept?: boolean; cells?: string[] }[]
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
