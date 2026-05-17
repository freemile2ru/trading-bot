export interface TradeRecommendation {
  action: 'BUY' | 'SELL' | 'HOLD'
  direction: 'long' | 'short' | 'none'
  symbol: string
  entry_price: number
  entry_zone: { low: number; high: number }
  stop_loss: number
  take_profit_1: number
  take_profit_2: number
  take_profit_3: number
  risk_reward_ratio: string
  timeframe: string
  hold_duration: string
  confidence: number
  position_size_suggestion: string
}

export interface OracleReasoning {
  primary_thesis: string
  technical_analysis: string
  fundamental_factors: string
  risk_factors: string
  invalidation_level: string
}

export interface MarketContext {
  trend: 'bullish' | 'bearish' | 'ranging'
  volatility: 'low' | 'medium' | 'high'
  market_phase: 'accumulation' | 'markup' | 'distribution' | 'markdown' | 'uncertain'
  btc_dominance_impact: string
  macro_environment: string
}

export interface OracleResult {
  trade_recommendation: TradeRecommendation
  reasoning: OracleReasoning
  market_context: MarketContext
  alerts: string[]
  error?: string
  _meta?: {
    model: string
    input_tokens: number
    output_tokens: number
    cache_read_tokens: number
    cache_creation_tokens: number
  }
}

export interface TimeframeSignal {
  rsi: number
  rsi_signal: string
  rsi_divergence: string
  signal: string
  score: number
  macd_cross: string
  macd_histogram: number
  bb_position: string
  bb_squeeze: boolean
  ema_alignment: string
  adx: number
  adx_strength: string
  stoch_k: number
  volume_ratio: number
  volume_signal: string
  obv_trend: string
  pivot: number
  resistance_1: number
  resistance_2: number
  support_1: number
  support_2: number
  distance_to_r1_pct: number
  distance_to_s1_pct: number
  ichimoku_signal: string
  ichimoku_tk_cross: string
  ichimoku_above_cloud: boolean
  ichimoku_cloud_color: string
  candlestick_patterns: string[]
  pattern_signal: string
  fib_position: number
  fib_nearest_support: number
  fib_nearest_resistance: number
  fib_near_support: boolean
  fib_near_resistance: boolean
  market_regime: string
}

export interface SignalPackage {
  symbol: string
  price: number
  composite_score: number
  direction: 'long' | 'short' | 'neutral'
  confidence_pct: number
  best_timeframe: string
  hold_duration: string
  stop_loss: number
  take_profit_1: number
  take_profit_2: number
  component_scores: {
    technical: number
    sentiment: number
    macro: number
    onchain: number
  }
  ta_summary: {
    consensus_label: string
    timeframe_signals: Record<string, TimeframeSignal>
  }
  market_regime: {
    regime: string
    confidence: number
    trend_direction: string
    adx: number
    bb_width_pct: number
    atr_pct: number
  }
  tf_alignment: {
    multiplier: number
    direction: string
    bullish_tfs: number
    bearish_tfs: number
    total_tfs: number
    aligned: boolean
  }
  fear_greed: { value: number; classification: string }
  news_summary: {
    aggregate_sentiment: string
    total_articles: number
    top_headlines: string[]
  }
  macro_summary: {
    yield_curve_inverted: boolean
    fed_funds_rate: number
    vix: number
  }
  social: { tweets_analyzed: number; sentiment_label: string; bullish_pct: number }
  coingecko: {
    market_cap_rank: number
    price_change_24h_pct: number
    price_change_7d_pct: number
    sentiment_votes_up_pct: number
    total_volume_usd: number
  }
}

export type SourceStatus =
  | { ok: true }
  | { ok: false; error: string }
  | { ok: 'partial'; error: string }

export interface PipelineStatus {
  elapsed_seconds: number
  oracle_ok: boolean
  data_sources: Record<string, SourceStatus>
  ta_timeframes: Record<string, string>
}

export interface OracleResponse {
  symbol: string
  signal_package: SignalPackage
  oracle: OracleResult
  raw_data: {
    ticker: Record<string, number>
    fear_greed: { value: number; classification: string }
  }
  pipeline_status: PipelineStatus
  cached?: boolean
}

export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface PaperTrade {
  id: string
  symbol: string
  direction: 'long' | 'short'
  entry_price: number
  current_price: number
  stop_loss: number
  take_profit_1: number
  take_profit_2: number
  quantity: number
  position_value_usd: number
  size_pct: number
  timeframe: string
  oracle_confidence: number
  oracle_reasoning: string
  opened_at: string
  closed_at: string | null
  status: 'open' | 'closed'
  pnl_usd: number
  pnl_pct: number
  risk_reward: number
  close_reason: string | null
}

export interface EquityPoint {
  ts: string
  equity: number
}

export interface Portfolio {
  balance: number
  initial_balance: number
  total_equity: number
  open_pnl: number
  realized_pnl: number
  total_return_pct: number
  open_trades: PaperTrade[]
  closed_trades: PaperTrade[]
  equity_history: EquityPoint[]
  stats: {
    total_trades: number
    open_trades: number
    win_rate_pct: number
    avg_win_usd: number
    avg_loss_usd: number
    profit_factor: number
    best_trade: number
    worst_trade: number
  }
}

export interface SignalHistoryEntry {
  ts: string
  symbol: string
  price: number
  action: 'BUY' | 'SELL' | 'HOLD'
  direction: string
  confidence: number
  timeframe: string
  entry_price: number
  stop_loss: number
  take_profit_1: number
  composite_score: number
  elapsed_seconds: number
  sources_ok: number
  sources_total: number
  primary_thesis: string
}
