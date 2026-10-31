import type { EquityPoint, OracleResult, PaperTrade, Portfolio } from './oracle'

export interface Setup {
  name: string
  label: string
  direction: 'long' | 'short'
  strength: number
  reasons: string[]
  timeframe: string
}

export interface FaComponent {
  score: number
  note: string
}

export interface Fundamentals {
  score: number
  label: 'bullish' | 'bearish' | 'neutral'
  components: Record<string, FaComponent>
  market_cap_rank: number | null
  rs_vs_btc_24h: number
}

export interface ScanTriggers {
  bb_squeeze: boolean
  divergence: string
  volume_ratio: number
  near_level: boolean
  distance_to_level_pct: number
  atr_pct: number
  tf_labels: Record<string, string | null>
}

export interface ScanRow {
  symbol: string
  base: string
  name: string | null
  rank: number | null
  price: number
  change_1h: number | null
  change_24h: number | null
  change_7d: number | null
  conviction: number
  direction: 'long' | 'short' | 'neutral'
  confidence: number
  composite_score: number
  ta_score: number
  fa: Fundamentals
  setups: Setup[]
  conviction_detail: {
    conviction: number
    setup_multiplier: number
    aligned_setups: string[]
    opposing_setups: string[]
  }
  regime: string
  /** Scraped-news reading (news/scoring.py); absent on rows from before it existed. */
  news?: { score: number | null; burst: number | null; stories_6h: number | null }
  best_timeframe: string | null
  tf_alignment: { multiplier: number; bullish_tfs: number; bearish_tfs: number; total_tfs: number }
  hold_duration: string | null
  levels: { stop_loss: number; take_profit_1: number; take_profit_2: number }
  triggers: ScanTriggers
  scanned_at: number
  tier: 'full' | 'light'
}

export interface FiredSignal {
  symbol: string
  base: string
  action: 'BUY' | 'SELL'
  confidence: number
  conviction: number
  entry_price: number
  stop_loss: number | null
  take_profit_1: number | null
  take_profit_2: number | null
  hold_duration: string | null
  timeframe: string | null
  setups: Setup[]
  fa_label: string
  regime: string
  thesis: string
  risk_factors: string | string[]
  alerts: string[]
  fired_at: number
  oracle: OracleResult
}

export interface WatchlistEntry {
  symbol: string
  added_at: number
  last_seen: number
  conviction: number
  reasons: string[]
  checks_below: number
  pinned: boolean
}

export interface ScannerStatus {
  running: string[]
  last_general: number
  last_watchlist: number
  general_interval: number
  watchlist_interval: number
  subscribers: number
  /** The firing bar: |conviction| and confidence a scan needs before Claude is asked. */
  min_conviction?: number
  min_confidence?: number
}

export interface SelectionEntry {
  symbol: string
  base: string
  tradeability: number
  rank: number
  selected: boolean
  range_pct: number
  quote_volume: number
}

export interface ScanSnapshot {
  rows: ScanRow[]
  fired: FiredSignal[]
  watchlist: WatchlistEntry[]
  updated_at: number | null
  universe_size: number
  pool_size?: number
  selection?: SelectionEntry[]
  next_sweep_at?: number
  llm_budget_used?: number
  llm_budget_total?: number
  scanner?: ScannerStatus
}

export interface WatchlistChange {
  entries: WatchlistEntry[]
  added: { symbol: string; reasons?: string[] }[]
  removed: { symbol: string; reason: string }[]
}

// ── Autotrader (autonomous experiment, separate wallet) ─────────────────────────

export interface AutoTrade extends PaperTrade {
  /** Which lane placed it: ta (scanner + Claude), events, unlocks, weekly — see autotrader/lanes.py. */
  lane?: string
  lane_mult?: number
  lane_why?: string
  expires_at?: number
  entry_score?: number
  entry_reasons?: string[]
  conviction_at_entry?: number
  armed_minutes?: number
  stop_moved?: boolean
  original_stop_loss?: number
  exit_note?: string
  confirmed_by?: 'claude' | 'scanner_signal'
  claude_confidence?: number
  claude_thesis?: string
  entry_type?: 'market' | 'limit'
  claude_entry?: number | null
  limit_price?: number
  adjustments?: TradeAdjustment[]
  last_review?: { ts: number; action: 'add' | 'reduce' | 'hold'; reason: string }
  realized_pnl_usd?: number
  initial_quantity?: number
}

export interface TradeAdjustment {
  ts: number
  action: 'add' | 'reduce'
  kind: 'pyramid' | 'average_down' | 'de_risk' | 'bank' | 'trim' | null
  price: number
  quantity: number
  reason: string
  new_stop: number | null
  quantity_after: number
  avg_entry_after: number
}

export interface AutoOrder {
  id: string
  symbol: string
  direction: 'long' | 'short'
  limit_price: number
  stop_loss: number
  take_profit_1: number
  take_profit_2: number
  size_pct: number
  placed_at: number
  expires_at: number
  status: 'pending' | 'filled' | 'cancelled'
  close_reason?: string
  resolved_at?: number
  current_price: number | null
  claude_entry?: number | null
  claude_confidence?: number
  market_price_at_decision?: number
  fill_price?: number
  plan?: { note: string; gap_pct: number; margin_pct: number }
}

export type AutoStatus =
  | 'in_trade' | 'order_pending' | 'busy_elsewhere' | 'confirming' | 'armed' | 'vetoed' | 'blocked' | 'cooldown' | 'watching'

export interface AutoSymbolState {
  symbol: string
  base: string
  price: number
  conviction: number
  status: AutoStatus
  direction?: 'long' | 'short' | null
  score?: number
  reasons: string[]
  armed_at?: number | null
  updated_at: number
}

export interface LaneCell { n: number; avg_r: number | null; net_r: number }

export interface AutoSummary extends Omit<Portfolio, 'open_trades' | 'closed_trades'> {
  open_trades: AutoTrade[]
  closed_trades: AutoTrade[]
  outcomes: {
    win: number; loss: number; expired: number; breakeven: number; flip: number; left_watchlist: number
    stale?: number; trailing_stop?: number
  }
  orders: AutoOrder[]
  order_history: AutoOrder[]
  order_stats: { filled: number; cancelled: number }
  states: AutoSymbolState[]
  enabled: boolean
  paused: boolean
  benchmark: { initial_balance: number; equity_history: EquityPoint[] }
  config: {
    entry_score: number
    min_conviction: number
    risk_pct: number
    tp1_r: number
    tp2_r: number
    cooldown_seconds: number
    min_claude_confidence: number
    veto_seconds: number
    llm_budget_used: number
    llm_budget_total: number
    llm_budget_warn: number
    max_open_risk_pct: number
    open_risk_pct: number
    maker_entries?: boolean
  }
  /** Maker entries vs the market entries they replaced (their shadow twins). */
  maker?: {
    enabled: boolean; placed: number; filled: number; missed: number; fill_rate_pct: number | null
    avg_price_improvement_bps: number | null
    filled_vs_twin: { trades: number; maker_r: number | null; market_twin_r: number | null }
    missed_twin_r: { trades: number; avg_r: number | null }
    net_r_vs_market: number | null
    pullback_r?: number | null; books?: string[]
  }
  /** The same report for pullback entries only (since 2026-10-19). */
  pullback?: AutoSummary['maker']
  /** Event shorts mirrored from the event book (coin short + BTC hedge, as one result). */
  events?: { enabled: boolean; leg_pct: number; open: number; closed: number; wins: number; net_usd: number }
  /** Forced daily entries (at least one trade a day), measured on their own. */
  daily_min?: { enabled: boolean; hours: number; risk_x: number; closed: number; wins: number; avg_r: number | null; net_usd: number; open: number }
  /** Dry-market widening: after `hours` idle the scanner covers `universe` coins and every one is a candidate. */
  dry?: { active: boolean; idle_h: number; hours: number; universe: number | null }
  /** Lanes: the scanner path beside mirrors of the evidence books, each sized by its record in the current regime. */
  lanes?: {
    regime: string; regime_label: string | null; rule: string; cell_n: number; min_n: number; measure_x: number
    lanes: { lane: string; label: string; mult: number; why: string; open: number; enabled?: boolean
             all: LaneCell; regime_cell: LaneCell; regimes: Record<string, LaneCell> }[]
  }
  updated_at: number
}

/** GET /api/signals/coins — per coin: signal tally and every open position across systems. */
export interface CoinTrade {
  system: 'main' | 'auto'
  mode: 'paper' | 'live'
  direction: 'long' | 'short'
  entry: number
  pnl_usd?: number | null
  pending?: boolean
  opened_at?: string
}
export interface CoinStatus {
  signals: { d1: number; d7: number; total: number; buys: number; sells: number; last: number } | null
  trades: CoinTrade[]
}
