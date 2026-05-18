import type { OracleResponse, Portfolio, PaperTrade, Candle, SignalHistoryEntry, OracleResult, SignalPackage } from '../types/oracle'

const BASE = '/api'

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(detail?.detail ?? `${res.status} ${res.statusText}`)
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
    throw new Error(detail?.detail ?? `${res.status} ${res.statusText}`)
  }
  return res.json()
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export const api = {
  oracle: {
    analyze: (symbol: string) =>
      get<OracleResponse>(`/oracle/${symbol}`),
    history: (symbol: string, limit = 20) =>
      get<{ symbol: string; history: SignalHistoryEntry[] }>(`/oracle/${symbol}/history?limit=${limit}`),
    executeAndTrade: (symbol: string, sizePct = 2) =>
      post<{ message: string; trade?: PaperTrade; oracle: OracleResponse['oracle'] }>(
        `/oracle/${symbol}/execute?auto_size_pct=${sizePct}`,
      ),
  },
  market: {
    ohlcv: (symbol: string, interval = '1h', limit = 120) =>
      get<{ symbol: string; interval: string; candles: Candle[] }>(
        `/ohlcv/${symbol}?interval=${interval}&limit=${limit}`,
      ),
    symbols: () => get<{ symbols: string[] }>('/symbols'),
  },
  paper: {
    portfolio: () => get<Portfolio>('/paper/portfolio'),
    updatePrices: () =>
      post<{ prices_updated: Record<string, number>; auto_closed: PaperTrade[] }>('/paper/update-prices'),
    closeTrade: (id: string, price: number) =>
      del<PaperTrade>(`/paper/trade/${id}?current_price=${price}`),
    reset: (balance = 10000) =>
      post<{ message: string; balance: number }>(`/paper/reset?starting_balance=${balance}`),
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
