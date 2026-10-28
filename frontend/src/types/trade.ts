/** Trade tab — backend api/trade_routes.py. */

export interface Venue {
  mode: 'paper' | 'live'
  env: string
  ws_book: string
  ws_market: string
  live_allowed: boolean
  has_keys: boolean
  confirm: string
  max_leverage: number
  maker_fee: number
  taker_fee: number
}

export interface SymbolInfo { symbol: string; volume: number; change: number; tick: number; step: number; min_notional: number }

export interface Candle { time: number; open: number; high: number; low: number; close: number; volume: number }

export interface Ticker {
  last: number; change_pct: number; high: number; low: number; volume_usd: number
  mark: number; index: number; funding_rate: number; next_funding: number
}

export interface Position {
  id: string; symbol: string; direction: 'long' | 'short'; mode: 'paper' | 'live'
  entry_price: number; current_price: number; quantity: number; notional_usd: number; leverage: number
  margin_usd: number; liquidation_price: number; stop_loss: number; take_profit_2: number; tp_set?: boolean
  pnl_usd: number; roe_pct: number; fees_usd: number; opened_at: string; strategy?: string; exit_policy?: string
}

export interface PendingOrder {
  id: string; symbol: string; direction: 'long' | 'short'; mode: 'paper' | 'live'; limit_price: number
  notional_usd: number; leverage: number; stop_loss: number; take_profit_2: number; placed_at: number
  expires_at: number; current_price: number | null
}

export interface ClosedTrade {
  id: string; symbol: string; direction: 'long' | 'short'; mode: 'paper' | 'live'; entry_price: number
  current_price: number; pnl_usd: number; risk_usd?: number; close_reason: string; closed_at: string
  opened_at: string; quantity: number; leverage: number; strategy?: string
}

export interface ExchangePosition {
  symbol: string; owner: 'main' | 'auto' | 'unmanaged'; side: 'long' | 'short'; qty: number
  entry: number; mark: number; notional: number; leverage: number; unrealized: number; liquidation: number
  margin: number; stop: number | null; target: number | null
}

export interface TradeAccount {
  exchange: ExchangePosition[]      // everything open on the Binance account (live only)
  mode: 'paper' | 'live'
  balance: { equity: number; available: number; wallet: number; unrealized: number; margin_used: number; error?: string }
  positions: Position[]
  orders: PendingOrder[]
  history: ClosedTrade[]
}

export interface OrderRequest {
  symbol: string; side: 'long' | 'short'; type: 'market' | 'limit'; notional_usd: number; leverage: number
  stop_loss: number; take_profit?: number | null; price?: number | null
}
