export interface GuardAdjustments {
  risk_mult: number; conviction_add: number; max_open_risk_pct: number
  paused: Record<string, number>; until: number; reason: string; source: string; set_at: number
}
export interface GuardsStatus {
  enabled: boolean; gate_n: number; max_open_risk_pct: number
  directions: Record<'long' | 'short', {
    n: number; net_r: number; open: boolean; next_needs_r: number | null; shadows: number
    trades: { book: string; symbol: string; r: number; closed_ts: number; gated: boolean }[]
  }>
  families: Record<string, { books: string[]; adjustments: Partial<GuardAdjustments>; active?: boolean; review_due: string | null }>
  log: (GuardAdjustments & { family: string })[]
  skips: { ts: number; book: string; symbol: string; direction: string; why: string }[]
  triggers: { losses_in_a_row: number; last10_net_r: number; adjust_hours: number }
  thesis_exit: {
    enabled: boolean; fade_below: number; min_age_h: number; closed: number; twins_running: number; pairs: number
    avg_r: number | null; twin_avg_r: number | null; saved_r: number | null
  } | null
}
