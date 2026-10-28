/** Live trading control — backend live/control.py and api/live_routes.py. */

export type LiveSystem = 'main' | 'auto'

export interface LiveSystemStatus {
  mode: 'paper' | 'live'
  live: boolean
  has_keys: boolean
  account: string | null
  confirm: string
  halted: string | null
  flat: boolean | null
  engine_mode?: 'paper' | 'live'
  account_balance?: { wallet: number; unrealized: number; equity: number; available: number }
  account_error?: string
}

export interface LiveStatus {
  allowed: boolean
  env: 'testnet' | 'mainnet'
  rest: string
  daily_loss_pct: number
  max_leverage: number
  shared_account: boolean
  systems: Record<LiveSystem, LiveSystemStatus>
}

export interface LiveActivity {
  events: { ts: number; system: string; kind: string; data: Record<string, unknown> }[]
  orders: { cid: string; system: string; symbol: string; purpose: string; side: string; type: string; qty: number;
            price: number | null; status: string; updated: number }[]
  audit: { ts: number; system: string; method: string; path: string; status: number | null; ms: number; response: string }[]
}
