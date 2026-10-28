export interface Book {
  name: string; label: string; kind: 'manual' | 'automated' | 'experiment' | 'scalper'
  description: string; off_means: string
  enabled: boolean; unavailable: string | null; mode: 'paper' | 'live' | null; switched_at: number | null
  open: number; closed: number; wins: number; win_rate: number | null; net_pnl: number
  trades_24h: number; trades_7d: number; last_opened: number | null; live_trades: number
  wallets: BookWallet[]; has_live: boolean
  verdict: BookVerdict | null
}
export interface BookVerdict {
  rule: 'trades' | 'carry'; status: 'collecting' | 'pass' | 'fail' | 'unclear' | 'no_edge'; note: string; registered: string
  trades?: number; target?: number; max?: number; avg_r?: number | null; ci?: [number, number] | null; level?: number
  days?: number; target_days?: number; funding_bps_day?: number | null; expected_bps_day?: number | null
}
export interface BookWallet {
  system: string; ledger: 'paper' | 'live'; label: string
  balance: number | null; equity: number | null; initial: number | null; return_pct: number | null
}
export interface RetiredBook { name: string; label: string; kind: 'retired'; description: string; closed: number; net_pnl: number }
export interface BooksResponse { books: Book[]; retired: RetiredBook[]; ledger: 'both' | 'paper' | 'live'; updated_at: number }
