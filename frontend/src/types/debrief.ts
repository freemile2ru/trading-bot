export interface ClaudeDebrief {
  thesis: 'right' | 'wrong' | 'partly right' | 'no thesis'
  what_happened: string
  cause: string
  lesson: string
}

export interface TradeDebrief {
  system: string
  trade_id: string
  symbol: string
  closed_ts: number
  primary: string
  tags: string[]
  metrics: {
    best_r?: number | null
    worst_r?: number | null
    post_best_r?: number | null
    post_worst_r?: number | null
    post_back_through_entry?: boolean
    window_h?: number
    r?: number | null
    net_pnl?: number
  }
  claude: ClaudeDebrief | null
}

export interface DebriefSummary {
  trades: number
  by_tag: { tag: string; trades: number; primary: number; share_pct: number; avg_r: number | null; net: number }[]
  causes: { cause: string; trades: number }[]
  recent: TradeDebrief[]
  calls_today: number
  daily_calls: number
  claude_systems: string[]
}
