export interface Outage { start: number; end: number; minutes: number; reason: string }
export interface WatchdogStatus {
  status: 'ok' | 'stale'; since: number | null; reason: string | null; outages: Outage[]
  enabled: boolean; stale_minutes: number; checked_at: number | null; last_price_ok: number | null
}
export interface MarketState {
  btc_trend?: 'up' | 'down'; btc_vol_30d?: number | null; btc_7d_pct?: number | null; label?: string | null
  activity?: 'quiet' | 'normal' | 'volatile' | null; btc_range_24h_pct?: number; btc_range_median_pct?: number; activity_ratio?: number
}
export interface CalendarEvent { kind: string; ts: number; label: string; symbol?: string; ticker?: string }
export interface CalendarStatus {
  now: number; blackout: string | null; next_macro: CalendarEvent | null; upcoming: CalendarEvent[]
  rules: { macro_before_min: number; macro_after_min: number; earnings_days: number }
}
export interface CalendarPageEvent extends CalendarEvent { rule: string; window?: [number, number] }
export interface CalendarPastEvent extends CalendarEvent { btc: { hour_pct: number; range_2h_pct: number; day_pct: number } | null }
export interface NearEventTrade {
  book: string; symbol: string; direction: string; event: string | null; opened_at: string | null; closed_at: string | null
  pnl_usd: number | null; r: number | null; close_reason: string | null
}
export interface NearEventSplit { trades: number; closed: number; avg_r: number | null; pnl_usd: number; win_pct: number | null }
export interface CalendarPage extends CalendarStatus {
  events: CalendarPageEvent[]; past: CalendarPastEvent[]
  trades: { near: NearEventSplit; clear: NearEventSplit; rows: NearEventTrade[] }
  earnings_refreshed_at: number | null; earnings_tickers: string[]
  settings: { before_min: number; after_min: number; earnings_days: number; near_h: number; near_earnings_days: number; lookahead_days: number }
}
