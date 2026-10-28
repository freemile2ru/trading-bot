import type { OracleResponse, Portfolio, PaperTrade, Candle, OracleResult, SignalPackage } from '../types/oracle'
import type { ExitComparison, HistoryFilters, HistoryInsights, HistoryResponse } from '../types/history'
import type { AutoOrder, AutoSummary, AutoTrade, ScanSnapshot, WatchlistEntry } from '../types/signals'
import type { PortfolioOverview, SystemKey, TargetReport, TradeAnalytics, TradeFilters, TradeRecord, TradesResponse, UnifiedOrder } from '../types/overview'
import type { LiveActivity, LiveStatus, LiveSystem } from '../types/live'
import type { LabSummary, SignalScores, StrategyLabResults, SwingReview } from '../types/lab'
import type { RejectedResearch, RetiredExperiment } from '../types/retired'
import type { DebriefSummary, TradeDebrief } from '../types/debrief'
import type { Book, BooksResponse } from '../types/books'
import type { MarketSummary } from '../types/markets'
import type { GuardsStatus } from '../types/guards'
import type { CalendarPage, CalendarStatus, MarketState, WatchdogStatus } from '../types/ops'
import type { CopyStatus } from '../types/copy'
import type { Edition, EditionSummary, Mood, NewsFilters, NewsItem, NewsResearch, NewsScores, NewsStatus, StoryBrief } from '../types/news'
import type { Candle as TradeCandle, ClosedTrade, OrderRequest, Position, SymbolInfo, Ticker, TradeAccount, Venue } from '../types/trade'

const BASE = '/api'

/** FastAPI errors: `detail` is a string, or a list of validation problems — never show "[object Object]". */
function errorMessage(body: any, res: Response): string {
  const d = body?.detail
  if (typeof d === 'string') return d
  if (Array.isArray(d) && d.length) {
    return d.map(e => `${(e.loc ?? []).filter((x: unknown) => x !== 'query' && x !== 'body').join('.') || 'request'}: ${e.msg}`).join('; ')
  }
  return `${res.status} ${res.statusText}`
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(errorMessage(detail, res))
  }
  return res.json()
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(errorMessage(detail, res))
  }
  return res.json()
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(errorMessage(await res.json().catch(() => null), res))
  return res.json()
}

/** Only set filters go on the wire, so the backend's defaults stay authoritative. */
export function historyQuery(f: HistoryFilters | Record<string, unknown>): string {
  const params = new URLSearchParams()
  Object.entries(f).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') params.set(k, String(v))
  })
  return params.toString()
}

export const api = {
  oracle: {
    analyze: (symbol: string) =>
      get<OracleResponse>(`/oracle/${symbol}`),
    // Futures: sized by risk (% of equity lost at the stop); leverage is chosen per trade.
    executeAndTrade: (symbol: string, riskPct = 1) =>
      post<{ message: string; trade?: PaperTrade; oracle: OracleResponse['oracle'] }>(
        `/oracle/${symbol}/execute?risk_pct=${riskPct}`,
      ),
  },
  signals: {
    list: () => get<ScanSnapshot>('/signals'),
    scan: () => post<ScanSnapshot>('/signals/scan'),
    watchlist: () =>
      get<{ entries: WatchlistEntry[]; max_size: number; check_interval_seconds: number }>(
        '/signals/watchlist',
      ),
    pin: (symbol: string) => post<WatchlistEntry>(`/signals/watchlist/${symbol}`),
    unpin: (symbol: string) => del<{ message: string }>(`/signals/watchlist/${symbol}`),
  },
  market: {
    ohlcv: (symbol: string, interval = '1h', limit = 120) =>
      get<{ symbol: string; interval: string; candles: Candle[] }>(
        `/ohlcv/${symbol}?interval=${interval}&limit=${limit}`,
      ),
  },
  positions: {
    close: (system: SystemKey, id: string) => post<unknown>(`/positions/${system}/${id}/close`),
    levels: (system: SystemKey, id: string, stop_loss: number | null, take_profit: number | null) =>
      post<unknown>(`/positions/${system}/${id}/levels`, { stop_loss, take_profit }),
    cancelOrder: (system: SystemKey, id: string) => del<unknown>(`/positions/orders/${system}/${id}`),
    orders: () => get<UnifiedOrder[]>('/positions/orders'),
    detail: (system: SystemKey, id: string) => get<TradeRecord>(`/positions/${system}/${id}`),
  },
  debriefs: {
    summary: (system?: string) => get<DebriefSummary>(`/debriefs/summary${system ? `?system=${system}` : ''}`),
    one: (system: string, id: string) => get<TradeDebrief>(`/debriefs/${system}/${id}`),
  },
  history: {
    insights: (wallet: 'all' | 'main' | 'auto', ledger = 'both') => get<HistoryInsights>(`/history/insights?wallet=${wallet}&ledger=${ledger}`),
  },
  lab: {
    summary: (ledger = 'both') => get<LabSummary>(`/experiments?ledger=${ledger}`),
    reviewNow: () => post<SwingReview>('/experiments/claude-swing/review'),
    retired: () => get<{ retired: RetiredExperiment[]; research?: RejectedResearch[] }>('/experiments/retired'),
    strategyLab: () => get<StrategyLabResults>('/experiments/strategy-lab'),
    signalScores: () => get<SignalScores>('/experiments/signal-scores'),
  },
  trades: {
    all: (f: TradeFilters) => get<TradesResponse>(`/trades/all?${historyQuery(f as Record<string, unknown>)}`),
    analytics: (f: TradeFilters) => {
      const { system, mode, symbol, direction, result, exit_reason, date_from, date_to, include_retired } = f
      return get<TradeAnalytics>(`/trades/analytics?${historyQuery({ system, mode, symbol, direction, result, exit_reason, date_from, date_to, include_retired: include_retired || undefined } as Record<string, unknown>)}`)
    },
    history: (f: HistoryFilters) => get<HistoryResponse>(`/trades/history?${historyQuery(f)}`),
    csvUrl: (f: HistoryFilters) => `${BASE}/trades/history.csv?${historyQuery(f)}`,
    exits: (wallet: string, ledger = 'both') => get<ExitComparison>(`/exits/compare?wallet=${wallet}&ledger=${ledger}`),
  },
  auto: {
    summary: () => get<AutoSummary>('/auto'),
    pause: (paused: boolean) => post<AutoSummary>(`/auto/pause?paused=${paused}`),
    close: (id: string) => del<AutoTrade>(`/auto/trade/${id}`),
    cancelOrder: (id: string) => del<AutoOrder>(`/auto/order/${id}`),
  },
  trade: {
    venue: () => get<Venue>('/trade/venue'),
    symbols: () => get<SymbolInfo[]>('/trade/symbols'),
    klines: (symbol: string, interval: string, days = 90) =>
      get<TradeCandle[]>(`/trade/klines?symbol=${symbol}&interval=${interval}&days=${days}`),
    ticker: (symbol: string) => get<Ticker>(`/trade/ticker?symbol=${symbol}`),
    account: () => get<TradeAccount>('/trade/account'),
    order: (req: OrderRequest) => post<{ mode: string; kind: string; id: string; warning?: string }>('/trade/order', req),
    cancel: (id: string) => del<unknown>(`/trade/order/${id}`),
    close: (id: string) => post<ClosedTrade>(`/trade/position/${id}/close`),
    levels: (id: string, stop_loss?: number | null, take_profit?: number | null) =>
      post<Position>(`/trade/position/${id}/levels`, { stop_loss, take_profit }),
  },
  live: {
    status: () => get<LiveStatus>('/live/status'),
    activity: (limit = 50) => get<LiveActivity>(`/live/activity?limit=${limit}`),
    setMode: (system: LiveSystem, mode: 'paper' | 'live', confirm = '') =>
      post<{ system: string; mode: string }>('/live/mode', { system, mode, confirm }),
    kill: () => post<Record<string, string[]>>('/live/kill'),
  },
  copy: {
    status: () => get<CopyStatus>('/copy/leaders'),
    refresh: () => post<CopyStatus>('/copy/refresh'),
  },
  portfolio: {
    overview: (includeRetired = false) => get<PortfolioOverview>(`/portfolio/overview${includeRetired ? '?include_retired=true' : ''}`),
    target: () => get<TargetReport>('/portfolio/target'),
    targetResume: () => post<TargetReport>('/portfolio/target/resume'),
    targetApplyPlan: () => post<{ deposits: unknown[]; report: TargetReport }>('/portfolio/target/apply-plan'),
  },
  paper: {
    portfolio: () => get<Portfolio>('/paper/portfolio'),
    updatePrices: () =>
      post<{ prices_updated: Record<string, number>; auto_closed: PaperTrade[] }>('/paper/update-prices'),
    closeTrade: (id: string, price: number) =>
      del<PaperTrade>(`/paper/trade/${id}?current_price=${price}`),
    reset: (balance = 10000) =>
      post<{ message: string; balance: number }>(`/paper/reset?starting_balance=${balance}`),
    exportCsv: async () => {
      const res = await fetch(`${BASE}/paper/export`)
      if (!res.ok) throw new Error('Export failed')
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `oracle_trades_${new Date().toISOString().slice(0, 10)}.csv`
      a.click()
      URL.revokeObjectURL(url)
    },
  },
  ops: {
    watchdog: () => get<WatchdogStatus>('/ops/watchdog'),
    market: () => get<MarketState>('/ops/market'),
    calendar: () => get<CalendarStatus>('/ops/calendar'),
    calendarPage: () => get<CalendarPage>('/ops/calendar/page'),
  },
  guards: {
    status: () => get<GuardsStatus>('/guards'),
    clear: (family: string) => post<GuardsStatus>(`/guards/${family}/clear`),
  },
  markets: {
    get: (market: 'stocks' | 'futures') => get<MarketSummary>(`/markets/${market}`),
    sweep: (market: 'stocks' | 'futures') => post<MarketSummary>(`/markets/${market}/sweep`),
  },
  books: {
    list: (ledger = 'both') => get<BooksResponse>(`/books?ledger=${ledger}`),
    set: (name: string, enabled: boolean) => post<Book>(`/books/${name}`, { enabled }),
  },
  news: {
    feed: (f: NewsFilters = {}) => {
      const q = new URLSearchParams()
      for (const [k, v] of Object.entries(f)) if (v !== undefined && v !== '' && v !== null) q.set(k, String(v))
      return get<{ items: NewsItem[]; generated_at: number }>(`/news${q.toString() ? `?${q}` : ''}`)
    },
    scores: () => get<NewsScores>('/news/scores'),
    status: () => get<NewsStatus>('/news/status'),
    research: () => get<NewsResearch>('/news/research'),
    refresh: () => post<NewsStatus>('/news/refresh'),
    important: (hours = 24) => get<{ stories: StoryBrief[]; held: string[] }>(`/news/important?hours=${hours}`),
    mood: () => get<Mood>('/news/mood'),
    edition: (id?: number | null) => get<Edition>(`/news/brief${id ? `?id=${id}` : ''}`),
    editions: () => get<{ editions: EditionSummary[]; calls_today: number; daily_calls: number; every_s: number }>('/news/briefs'),
    newEdition: () => post<Edition>('/news/brief'),
  },
  notify: {
    signal: (oracle: OracleResult, signal: SignalPackage) => {
      const rec = oracle.trade_recommendation
      return post<{ email_sent: boolean; sms_sent: boolean }>('/notify/signal', {
        symbol: rec.symbol,
        action: rec.action,
        confidence: rec.confidence,
        entry_price: rec.entry_price,
        stop_loss: rec.stop_loss,
        take_profit_1: rec.take_profit_1,
        composite_score: signal.composite_score,
        hold_duration: rec.hold_duration,
        primary_thesis: oracle.reasoning?.primary_thesis ?? '',
      })
    },
  },
}
