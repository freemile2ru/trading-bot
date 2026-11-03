export interface CopyLeader {
  id: string; venue?: 'binance' | 'hyperliquid' | 'lighter'; retired?: boolean; turnover?: number | null; nickname: string; nickname_en: string | null; roi: number; mdd: number; win_rate: number; copiers: number; aum: number
  copier_pnl: number; score: number; days_running: number; leverage_tier: string | null; badge: string | null
  profile: { n: number; median_hold_h: number | null; win_move_pct: number | null; loss_move_pct: number | null; win_rate: number | null; trades_per_day?: number }
  stats: { sharpe: number | null; mdd_pct: number | null; weeks_positive_pct: number | null; last_30d_pct?: number; days?: number }
  curve?: [number, number][]
  /** Which copy rules follow this leader (base, wide, fast, late). */
  variants?: string[]
  /** How many of the leader's positions (history + open) fall in each of our markets. */
  markets?: Record<string, number>
  /** What the leader holds right now (last poll) and what we did about each position. */
  positions?: { symbol: string; market?: string; side: string; entry: number; leverage: number | null; opened_ts: number | null; remaining: number
                mark: number | null; moved_pct: number | null; state: string }[]
}
export interface CopyBook {
  shared_wallet?: boolean
  market: string; variant: string; book: string; label: string; note: string; enabled: boolean; equity: number; initial: number; open: number; closed: number; win_rate: number | null; net_pnl: number
  per_day_7d?: number; trades_7d?: number
  recent: { symbol: string; direction: string; leader: string | null; venue?: string | null; pnl_usd: number | null; roe_pct: number | null; close_reason: string | null
            opened_at: string | null; closed_at: string | null; moved_pct_at_entry: number | null }[] }
export interface CopyStatus {
  leaders: CopyLeader[]
  /** Per market, per variant (base, wide, fast, late): that rule's own book. */
  variants?: Record<string, Record<string, CopyBook>>
  variant_info?: Record<string, { label: string; note: string }>
  pool?: number
  leaders_refreshed_at: number | null
  positions_at?: number | null
  why_not?: { total: number; copied: number; pre_tracking: number; too_late_age: number; too_late_price: number; already_failing: number; no_feed: number; book_full: number; other: number; text: string }
  consensus: { symbol: string; side: string; leaders: number }[]
  log?: { observations: number; positions: number; since: number | null; through: number | null; selections: number; db: string }
  /** Per-market copy books (the base rule): crypto, stocks, futures — same leaders, separate wallets. */
  books?: Record<string, CopyBook>
  book: { equity: number; initial: number; open: number; closed: number; win_rate: number | null; net_pnl: number
          recent: { symbol: string; direction: string; leader: string | null; pnl_usd: number | null; roe_pct: number | null; close_reason: string | null
                    opened_at: string | null; closed_at: string | null; moved_pct_at_entry: number | null }[] }
  open: { symbol: string; direction: string; market?: string; variant?: string; size_x?: number | null; leader: string | null; venue?: string | null; consensus?: number | null; entry: number; leader_entry: number | null; moved_pct_at_entry: number | null
          age_h_at_entry: number | null; pnl_usd: number | null; opened_at: string | null }[]
  skipped: { ts: number; leader: string; venue?: string | null; symbol: string; market?: string; variant?: string; side: string; reason: string; mark: number | null; moved_pct?: number; age_h?: number; outcome_pct?: number; leader_roi?: number }[]
  skip_rule: { judged: number; avg_outcome_pct: number | null; would_have_won_pct: number | null }
  config: { leaders_n: number; wide_n?: number; fast_hold_h?: number; late_size_x?: number; venues?: string[]; consensus_x?: number; leg_pct: number; max_leverage: number; max_open: number; disaster_pct: number; late_age_frac: number; late_move_frac: number
            adverse_frac: number; max_hold_h?: number; stop_x?: number; stop_pct_min?: number; max_leg_pct?: number; fee_gate_x?: number; round_trip_pct?: number; reduce_frac?: number; risk_pct?: number; bar: { min_copiers: number; min_aum: number; min_days: number; max_mdd: number; min_win: number; min_weeks_pos: number } }
}
