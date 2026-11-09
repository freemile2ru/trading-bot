import type { SystemKey } from './overview'

export interface BookStats {
  trades: number
  open: number
  net_pnl: number
  win_rate: number | null
  avg_pnl: number | null
  avg_r: number | null
  unrealized: number
}

export interface Verdict {
  status: 'collecting' | 'edge' | 'no_edge' | 'inconclusive' | 'extend' | 'adds' | 'costs' | 'no_difference'
  label: string
  expectancy_usd?: number
  expectancy_ci?: [number, number]
  diff_per_trade?: number
  diff_ci?: [number, number]
  claude_better_share?: number
  with_trades?: number
  without_trades?: number
}

export interface Experiment extends BookStats {
  key: SystemKey
  pending_orders: number
  label: string
  description: string
  started_at: number
  running_days: number
  verdict: Verdict
  curve: { ts: number; pnl: number }[]
  /** Hold-72 only: the year-long replay it tests, its settings, and results per side. */
  replay?: HoldReplay | null
  config?: { min_conviction: number; hold_hours: number; leg_pct: number; max_pairs: number; disaster_pct: number }
  by_side?: Record<'long' | 'short', BookStats>
  /** Carry only: positions, funding and the holdout it's testing. */
  carry?: { coins: string[]; notional_x: number; open: number; equity: number; initial?: number; funding_earned: number; return_pct: number | null
            annualised_pct: number | null
            positions: { symbol: string; notional: number; funding: number; basis_bps_now: number | null; pnl: number; days: number }[] }
  research?: Record<string, any> | null
  /** Carry Q only: the quarterly basis locked per position. */
  carry_q?: { coins: string[]; notional_x: number; open: number; equity: number; initial?: number; return_pct: number | null
              positions: { symbol: string; contract: string; notional: number; basis_locked_pct: number; days_to_delivery: number; pnl: number; days: number }[] }
  /** Event shorts only: announcements queued and positions open. */
  events?: { pending: { symbol: string; kind: string; title: string | null }[]
             open: { symbol: string; kind: string; mark_pct: number | null; deadline_ts: number }[] }
  /** Unlock shorts only: positions, what's queued, and the qualifying unlocks on the calendar. */
  unlocks?: { open: { symbol: string; pct: number; mark_pct: number | null; unlock_ts: number }[]
              pending: { symbol: string; pct: number; unlock_ts: number }[]
              watch: { symbol: string; pct: number; usd: number; insider_share: number; ts: number }[]
              closed: number; equity: number; calendar_coins: number; calendar_refreshed_at: number | null }
  /** Copy trading only: the leaders being mirrored, open copies and the skipped entries. */
  copy_trading?: {
    leaders: { id: string; venue?: 'binance' | 'hyperliquid'; nickname: string; nickname_en: string | null; roi: number; mdd: number; win_rate: number; copiers: number; aum: number
               copier_pnl: number; score: number; days_running: number | null; leverage_tier: string | null; badge: string | null
               profile: { n: number; median_hold_h: number | null; win_move_pct: number | null; loss_move_pct: number | null; win_rate: number | null; trades_per_day?: number }
               stats: { sharpe: number | null; mdd_pct: number | null; weeks_positive_pct: number | null; last_30d_pct?: number } }[]
    leaders_refreshed_at: number | null
    open: { symbol: string; direction: string; leader: string | null; entry: number; leader_entry: number | null; moved_pct_at_entry: number | null
            age_h_at_entry: number | null; pnl_usd: number | null; opened_at: string | null }[]
    skipped: { ts: number; leader: string; symbol: string; side: string; reason: string; mark: number | null; moved_pct?: number; age_h?: number; outcome_pct?: number }[]
    skip_rule: { judged: number; avg_outcome_pct: number | null; would_have_won_pct: number | null }
  }
  /** Vol premium only: the open condors. */
  vol_premium?: { open: { coin: string; qty: number; iv: number; credit: number; max_loss: number; mark: number; days_to_expiry: number; strikes: number[] }[]
                  closed: number; equity: number }
}

export interface HoldReplay {
  threshold: number; hold_h: number; variant: string; trades: number; days: number
  mean_bps_day: number; ci_low: number; ci_high: number; train_bps_day: number; test_bps_day: number
  quarters_positive: number; quarters: number; by_side_bps: Record<string, number>; passed: boolean
}

export interface Comparison {
  key: string
  title: string
  since: number
  with: BookStats & { systems: string[]; curve: { ts: number; pnl: number }[] }
  without: BookStats & { systems: string[]; curve: { ts: number; pnl: number }[] }
  verdict: Verdict
}

export interface SwingTrade {
  symbol: string
  direction: 'long' | 'short'
  entry_type: 'market' | 'limit'
  entry: number
  stop: number
  target: number
  hold_days: number
  confidence: number
  thesis: string
  invalidation: string
  why?: string
  kind?: 'trade' | 'order'
}

export interface SwingReview {
  ts: number
  market_view?: string
  coins?: string[]
  opened?: SwingTrade[]
  rejected?: SwingTrade[]
  managed?: { symbol: string; action: string; reason?: string; new_stop?: number }[]
  calls_today?: number
  usage?: { input_tokens: number; output_tokens: number }
  error?: string
}

export interface ResearchItem {
  key: string; title: string; script: string; file: string | null; question: string; finding: string; decision: string
  book: string | null; ran_at: number | null; status: 'promoted' | 'rejected' | 'mixed' | 'pending'
}

export interface LabSummary {
  ledger?: 'both' | 'paper' | 'live'
  experiments: Experiment[]
  research_shelf?: ResearchItem[]
  comparisons: Comparison[]
  min_trades: number
  enabled: boolean
  claude_swing: {
    interval_h: number
    daily_calls: number
    max_open: number
    risk_pct: number
    model: string
    last_review: number | null
    next_review: number | null
    calls_today: number
    reviews: SwingReview[]
  }
  updated_at: number
}

// ── Strategy lab (research/strategy_lab.py results) ───────────────────────────

export interface BacktestStats {
  days: number
  total_return_pct?: number
  annual_return_pct?: number
  sharpe?: number | null
  max_drawdown_pct?: number
  t_stat?: number | null
  win_days_pct?: number
  mean_daily_bps?: number
  mean_daily_ci_bps?: [number, number]
}

export interface StrategyFamily {
  key: string
  title: string
  chosen: string
  train: BacktestStats
  test: BacktestStats
  status: 'promising' | 'no_edge' | 'fails' | 'insufficient'
  verdict: string
  grid_size: number
  grid_test_positive_pct: number | null
  grid_test_median_sharpe: number | null
  grid: { setting: string; train_sharpe: number | null; test_sharpe: number | null; test_return_pct: number | null }[]
  curve: { day: string; equity: number }[]
}

export interface StrategyLabResults {
  generated_at: number
  dataset: string
  period: [string, string]
  split: string
  coins: number
  cost_per_side_pct: number
  benchmark_btc_test: BacktestStats
  benchmark_btc_curve: { day: string; equity: number }[]
  families: StrategyFamily[]
}

// ── Signal scoring (research/signal_scores.py) ────────────────────────────────

export interface SignalHorizon {
  n: number
  days: number
  buckets: { bucket: string; n: number; avg_bps: number; hit_pct: number; after_costs_bps: number }[]
  inputs: { input: string; ic?: number; t?: number | null; days: number; ic_vs_btc?: number | null; t_vs_btc?: number | null }[]
  by_regime: { regime: string; n: number; avg_bps: number; hit_pct: number }[]
  claude: { fired_n: number; fired_avg_bps: number; other_n: number; other_avg_bps: number } | null
}

export interface SignalScores {
  readings: number
  days?: number
  min_days: number
  round_trip_bps: number
  period?: [number, number]
  horizons: Partial<Record<'4h' | '24h' | '72h', SignalHorizon>>
}
