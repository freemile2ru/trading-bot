/** All three trading systems on one footing — backend trading/overview.py. */

export type PaperSystem = 'main' | 'auto' | 'scalper' | 'impulse'
export type MarketSystem = `${'stocks' | 'futures'}_${'trend' | 'rules' | 'claude'}`
export type ExperimentSystem = 'auto_rules' | 'signals_claude' | 'signals_rules' | 'claude_swing' | 'hold72' | 'fade24' | 'events' | 'weekly_rs' | 'cascade' | 'carry' | 'carry_q' | 'unlocks' | 'vol_premium' | 'copy_trading' | 'copy_stocks' | 'copy_futures' | 'multi_factor' | 'auction' | CopyVariantSystem | MarketSystem
export type CopyVariantSystem = `copy_${'crypto' | 'stocks' | 'futures'}_${'wide' | 'fast' | 'late'}`
export type SystemKey = PaperSystem | ExperimentSystem | 'main_live' | 'auto_live' | 'scalper_live'

export interface UnifiedTrade {
  id: string
  system: SystemKey
  /** Autotrader only: which lane placed it (ta, events, unlocks, weekly). */
  lane?: string | null
  symbol: string
  direction: 'long' | 'short'
  status: 'open' | 'closed'
  opened_ts: number | null
  closed_ts: number | null
  hold_min: number | null
  entry_price: number | null
  exit_price: number | null
  mark_price: number | null
  stop_loss: number | null
  take_profit: number | null
  notional: number | null
  leverage: number | null
  margin: number | null
  risk_usd: number | null
  gross_pnl: number | null
  fees: number | null
  funding: number | null
  net_pnl: number | null
  r: number | null
  exit_reason: string | null
  outcome: string | null
  timeframe: string | null
  note: string
  quantity?: number | null
  liquidation?: number | null
  tp_set?: boolean
  mfe_r?: number | null
}

/** A resting limit order in any swing book (GET /api/positions/orders). */
export interface UnifiedOrder {
  system: SystemKey
  id: string
  symbol: string
  direction: 'long' | 'short'
  limit_price: number
  notional_usd: number | null
  leverage: number | null
  stop_loss: number
  take_profit: number | null
  placed_at: number | null
  expires_at: number | null
  current_price: number | null
}

/** Everything recorded about one trade (GET /api/positions/{system}/{id}). */
export type TradeRecord = Record<string, unknown> & { id: string; system: SystemKey; kind: 'swing' | 'scalp'; symbol: string }

/** Which actions the Trades page offers for a system's positions. */
export function positionActions(system: SystemKey): { close: boolean; levels: boolean; cancel: boolean; note?: string } {
  if (['main', 'main_live', 'auto', 'auto_live'].includes(system)) return { close: true, levels: true, cancel: true }
  if (['scalper', 'scalper_live', 'impulse'].includes(system)) return { close: false, levels: false, cancel: false, note: 'retired — history only' }
  return { close: false, levels: false, cancel: false, note: 'experiment — hands off' }
}

export interface TradeFilters {
  system?: SystemKey | ''
  mode?: 'paper' | 'live' | ''
  status?: 'open' | 'closed' | ''
  symbol?: string
  direction?: 'long' | 'short' | ''
  result?: 'win' | 'loss' | ''
  exit_reason?: string
  date_from?: number
  date_to?: number
  sort?: string
  order?: 'asc' | 'desc'
  page?: number
  page_size?: number
  include_retired?: boolean
}

export interface TradesResponse {
  rows: UnifiedTrade[]
  total: number
  page: number
  pages: number
  page_size: number
  facets: { symbols: string[]; exit_reasons: string[] }
  totals: { trades: number; net_pnl: number; gross_pnl: number; fees: number; funding: number; wins: number }
  open_count: Partial<Record<SystemKey, number>>
  retired_hidden?: number
  /** Retired systems (hidden unless include_retired) and why. */
  retired_systems?: Record<string, string>
}

export interface SideStats { trades: number; net_pnl: number | null; win_rate: number | null }

export interface SystemMetrics {
  label: string
  initial_balance: number | null
  equity: number | null
  total_return_pct: number | null
  realized_pnl: number | null
  unrealized_pnl: number | null
  pnl_24h: number | null
  pnl_7d: number | null
  pnl_30d: number | null
  trades: number
  open_positions: number
  win_rate: number | null
  profit_factor: number | null
  payoff_ratio: number | null
  breakeven_win_rate: number | null
  expectancy_usd: number | null
  avg_r: number | null
  total_r: number | null
  avg_win: number | null
  avg_loss: number | null
  best_trade: number | null
  worst_trade: number | null
  gross_pnl: number | null
  fees: number | null
  funding: number | null
  costs_pct_of_gross_wins: number | null
  max_drawdown_usd: number | null
  max_drawdown_pct: number | null
  current_drawdown_pct: number | null
  longest_drawdown_h: number | null
  recovery_factor: number | null
  sharpe: number | null
  sortino: number | null
  days: number
  current_streak: number
  longest_win_streak: number
  longest_loss_streak: number
  avg_hold_win_min: number | null
  avg_hold_loss_min: number | null
  long: SideStats
  short: SideStats
  best_symbol: { symbol: string; net_pnl: number } | null
  worst_symbol: { symbol: string; net_pnl: number } | null
  exposure: {
    long_notional: number | null
    short_notional: number | null
    net_notional: number | null
    gross_leverage: number | null
    margin_used: number | null
    open_risk_usd: number | null
    open_risk_pct: number | null
  }
}

export type OverviewKey = SystemKey | 'all' | 'everything'

export interface PortfolioOverview {
  systems: Partial<Record<OverviewKey, SystemMetrics>> & { all: SystemMetrics; everything?: SystemMetrics }
  curves: Partial<Record<OverviewKey, { ts: number; equity: number }[]>>
  updated_at: number
}

export const SYSTEM_LABEL: Record<SystemKey, string> = {
  main: 'Main wallet', auto: 'Autotrader', scalper: 'Scalper', impulse: 'Impulse scalper',
  main_live: 'Main · LIVE', auto_live: 'Autotrader · LIVE', scalper_live: 'Scalper · LIVE',
  auto_rules: 'Autotrader · no Claude', signals_claude: 'Signals · Claude', signals_rules: 'Signals · rules only',
  claude_swing: 'Claude swing', hold72: 'Hold vs BTC (24h)', fade24: 'Fade the scanner (24h vs BTC)', events: 'Event shorts (listings & delistings)', weekly_rs: 'Weekly near-high (long/short)', cascade: 'Cascade fade (+1h, 4h)', carry_q: 'Carry Q · quarterly basis', carry: 'Carry · BTC, ETH & gold', unlocks: 'Unlock shorts (cliff unlocks vs BTC)', vol_premium: 'Vol premium (weekly BTC/ETH/SOL condors)', copy_trading: 'Copy trading (Binance & Hyperliquid leaders)', multi_factor: 'Multi-factor basket (momentum + trend + funding)', auction: 'Auction levels (mentor’s NQ system)', copy_stocks: 'Copy trading · stock perps', copy_futures: 'Copy trading · commodity perps', copy_crypto_wide: 'Copy · crypto · more leaders', copy_crypto_fast: 'Copy · crypto · faster traders', copy_crypto_late: 'Copy · crypto · late entries, half size', copy_stocks_wide: 'Copy · stock perps · more leaders', copy_stocks_fast: 'Copy · stock perps · faster traders', copy_stocks_late: 'Copy · stock perps · late entries, half size', copy_futures_wide: 'Copy · commodity perps · more leaders', copy_futures_fast: 'Copy · commodity perps · faster traders', copy_futures_late: 'Copy · commodity perps · late entries, half size',
  stocks_trend: 'Stocks · Trend', stocks_rules: 'Stocks · Scanner rules', stocks_claude: 'Stocks · Claude-confirmed',
  futures_trend: 'Futures · Trend', futures_rules: 'Futures · Scanner rules', futures_claude: 'Futures · Claude-confirmed',
}

export const isLive = (s: SystemKey) => s.endsWith('_live')

// ── Trades page charts & research (GET /api/trades/analytics) ──────────────────

export interface GroupStat {
  key: string | number
  trades: number
  wins: number
  win_rate: number
  net_pnl: number
  gross_pnl: number
  avg_r: number | null
}

export interface TradeAnalytics {
  trades: number
  systems: SystemKey[]
  initial_balance: number
  cumulative: { total: { ts: number; pnl: number }[]; by_system: Partial<Record<SystemKey, { ts: number; pnl: number }[]>> }
  daily: { day: string; net_pnl: number; trades: number; wins: number }[]
  r_distribution: { label: string; lo: number | null; hi: number | null; count: number }[]
  costs: { gross_pnl: number; fees: number; funding: number; net_pnl: number; won_before_costs: number; gross_wins: number }
  efficiency: {
    points: { mfe_r: number; r: number; system: SystemKey; symbol: string }[]
    trades: number
    avg_best_r: number | null
    avg_kept_r: number | null
    reached_1r: number
    reached_1r_then_lost: number
  }
  rolling: { window: number; points: { n: number; ts: number; avg_r: number | null; win_rate: number; expectancy: number }[] }
  by_symbol: GroupStat[]
  by_exit: GroupStat[]
  by_side: GroupStat[]
  by_system: GroupStat[]
  by_hold: GroupStat[]
  by_hour: GroupStat[]
  by_weekday: GroupStat[]
  heatmap: { weekday: number; hour: number; net_pnl: number; trades: number }[]
  streaks: { current: number; longest_win: number; longest_loss: number }
  fee_scenarios: { label: string; fees: number; net_pnl: number; win_rate: number | null }[]
  confidence: {
    trades: number
    enough: boolean
    expectancy_usd?: number
    expectancy_ci?: [number, number]
    t_stat?: number | null
    p_value?: number | null
    positive_share?: number
    expectancy_r?: number
    sd_r?: number
    sqn?: number | null
    trades_needed?: number | null
    kelly_pct?: number
  }
  monte_carlo: {
    simulations: number
    trades: number
    initial_balance: number
    actual_final: number
    actual_max_drawdown: number
    final: { p5: number; p25: number; p50: number; p75: number; p95: number }
    max_drawdown: { p50: number; p90: number; p99: number }
    loss_probability_pct: number
    drawdown_odds: { pct: number; odds_pct: number }[]
  } | null
}

export interface TargetBook {
  book: string; label: string; equity: number; added: number; planned: number | null; shortfall?: number
  today: number; avg7: number; avg30: number; days: number; since: string | null
  expected: number | null; expected_why: string; open_positions: number
}
export interface TargetReport {
  auto?: { on: boolean; hour_utc: number; last: { at: number; moves: { book: string; from: number; to: number; delta: number; why: string }[]; applied?: [string, number][]; note?: string; verdicts?: Record<string, string> } | null }
  target: number; target_why?: string; goal?: number; gap_goal7?: number; now: number; books: TargetBook[]
  totals: { today: number; avg7: number; avg30: number; expected: number; equity: number; added: number }
  gap7: number; gap_expected: number; paused: { at: number; reason: string } | null; trip_avg7: number
  plan: Record<string, number>; plan_note?: { at: number; note: string } | null
  deposits: { ts: number; book: string; amount: number; note: string }[]
  measuring_since: string | null
}

/** What the allocator's rule would move (GET /api/portfolio/target/suggest) — advisory until applied. */
export interface PlanSuggestion {
  rule: string
  ladder_rule?: string
  current: Record<string, number>
  suggested: Record<string, number>
  moves: { book: string; from: number; to: number; delta: number; why: string }[]
  scores: Record<string, number | null>
  verdicts?: Record<string, string>
  changed: boolean
}
