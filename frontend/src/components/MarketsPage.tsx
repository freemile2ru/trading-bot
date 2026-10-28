import { useCallback, useEffect, useRef, useState } from 'react'
import { Globe2, RefreshCw } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import type { BooksResponse } from '../types/books'
import type { MarketBook, MarketKey, MarketSummary } from '../types/markets'
import { BookCard } from './BooksPage'
import { SortHeader, useSort } from './ui/Sortable'

const MARKETS: [MarketKey, string, string][] = [
  ['crypto', 'Crypto', 'The crypto books: autotrader, signal experiments, Claude swing, carry, Hold-72'],
  ['stocks', 'Stocks', '20 US stocks & ETFs plus SK Hynix, Samsung and Tencent — Binance stock perpetuals'],
  ['futures', 'Futures', 'Gold, silver, platinum, palladium, copper, WTI, Brent, natural gas — Binance commodity perpetuals'],
]
const CRYPTO_BOOKS = new Set(['main', 'auto', 'auto_rules', 'signals_claude', 'signals_rules', 'claude_swing', 'carry', 'hold72', 'fade24', 'weekly_rs', 'events', 'cascade', 'carry_q'])
const KIND_NOTE: Record<MarketBook['kind'], string> = {
  trend: 'Daily after the US close: long while the 6-month trend is up, flat otherwise; equal volatility, 15% disaster stop.',
  rules: 'Our scanner + autotrader entry rules on the perps’ own candles, no Claude; the live "protect" exit.',
  claude: 'Switched off 2026-10-19: in 10 days and ~220 Claude calls it never traded — Claude’s confidence here topped out at 52–57% against a 65% bar.',
}
const KEY = 'oracle.market'
const memo: Partial<Record<'stocks' | 'futures', MarketSummary>> & { crypto?: BooksResponse } = {}

const ago = (ts: number | null | undefined) => {
  if (!ts) return 'not yet'
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60))
  return m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ${m % 60}m ago`
}
const tone = (v: number | null | undefined) => (v == null || v === 0 ? 'text-gray-300' : v > 0 ? 'text-emerald-400' : 'text-red-400')
const base = (s: string) => s.replace('USDT', '')

function readMarket(): MarketKey {
  const fromHash = window.location.hash.split('/')[1] as MarketKey
  if (MARKETS.some(m => m[0] === fromHash)) return fromHash
  try {
    const v = localStorage.getItem(KEY) as MarketKey
    return MARKETS.some(m => m[0] === v) ? v : 'crypto'
  } catch { return 'crypto' }
}

function MarketBookCard({ b }: { b: MarketBook }) {
  return (
    <section className={clsx('card space-y-3', !b.enabled && 'opacity-70')}>
      <div className="flex items-start gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-white">{b.label}</h3>
          <p className="text-[11px] text-gray-500">{KIND_NOTE[b.kind]}</p>
        </div>
        <span className={clsx('ml-auto text-[10px] font-semibold px-1.5 py-0.5 rounded', b.enabled ? 'bg-emerald-900/50 text-emerald-300' : 'bg-gray-800 text-gray-400')}
          title="Switch it on the Books tab">{b.enabled ? 'ON' : 'OFF'}</span>
      </div>
      <div className="grid grid-cols-3 gap-2 text-xs">
        {([
          ['Equity', <>{fmtUsd(b.equity)} <span className={clsx('text-[10px]', tone(b.return_pct))}>{b.return_pct == null ? '' : `${b.return_pct > 0 ? '+' : ''}${b.return_pct}%`}</span></>],
          ['Closed · won', `${b.closed} · ${b.wins}`],
          ['Avg R · net', <><span>{b.avg_r == null ? '—' : `${b.avg_r > 0 ? '+' : ''}${b.avg_r}R`}</span> · <span className={tone(b.net_pnl)}>{fmtUsdSigned(b.net_pnl, 0)}</span></>],
        ] as const).map(([k, v]) => (
          <div key={k} className="bg-gray-800/40 rounded px-2 py-1.5">
            <div className="text-[10px] text-gray-500">{k}</div>
            <div className="tabular-nums text-gray-100">{v}</div>
          </div>
        ))}
      </div>
      <div className="space-y-1">
        <div className="text-[11px] text-gray-500">Open ({b.open.length})</div>
        {!b.open.length ? <p className="text-[11px] text-gray-600">None.</p> : b.open.map(t => (
          <div key={t.id} className="flex flex-wrap items-center gap-x-3 text-xs">
            <span className="font-semibold text-gray-100 w-20">{base(t.symbol)}</span>
            <span className={t.direction === 'long' ? 'text-emerald-400' : 'text-red-400'}>{t.direction}</span>
            <span className="text-gray-400 tabular-nums">@ {t.entry_price.toPrecision(6)}</span>
            <span className={clsx('ml-auto tabular-nums', tone(t.pnl_usd))}>{fmtUsdSigned(t.pnl_usd)}</span>
          </div>
        ))}
      </div>
      {!!b.recent_closed.length && (
        <div className="space-y-1">
          <div className="text-[11px] text-gray-500">Recently closed</div>
          {b.recent_closed.slice(0, 5).map(t => (
            <div key={t.id} className="flex flex-wrap items-center gap-x-3 text-[11px] text-gray-400">
              <span className="font-semibold text-gray-200 w-20">{base(t.symbol)}</span>
              <span>{t.direction}</span><span>{(t.close_reason ?? '').replace(/_/g, ' ')}</span>
              <span className={clsx('ml-auto tabular-nums', tone(t.pnl_usd))}>{fmtUsdSigned(t.pnl_usd)}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function PicksCard({ s }: { s: MarketSummary }) {
  const p = s.picks
  return (
    <section className="card space-y-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-sm font-semibold text-white">Claude's daily picks</h3>
        <span className="text-[11px] text-gray-500">
          From {s.pick_hour_utc}:00 UTC Claude reviews the {s.picks_per_day} strongest-conviction instruments, whatever the rules say, and
          the Claude book enters any BUY or SELL it's confident in — a test of Claude's own judgement.
        </span>
        {p && <span className="ml-auto text-[11px] text-gray-500">last run {ago(p.at)}</span>}
      </div>
      {!p?.picks.length ? <p className="text-[11px] text-gray-600">No picks yet.</p> : p.picks.map(x => (
        <div key={x.symbol} className="flex flex-wrap items-baseline gap-x-3 text-xs">
          <span className="font-semibold text-gray-100 w-20">{x.base}</span>
          <span className={clsx('w-28', x.action === 'BUY' ? 'text-emerald-400' : x.action === 'SELL' ? 'text-red-400' : 'text-gray-400')}>
            {x.action} {x.confidence}%</span>
          <span className={x.entered ? 'text-emerald-300 font-semibold' : 'text-gray-500'}>{x.entered ? 'entered' : 'not entered'}</span>
          <span className="text-[11px] text-gray-500 basis-full sm:basis-auto sm:flex-1 truncate" title={x.thesis}>{x.thesis}</span>
        </div>
      ))}
    </section>
  )
}

function ScanTable({ s }: { s: MarketSummary }) {
  const t = useSort(s.scans, {
    instrument: r => r.base, price: r => r.price, conviction: r => r.conviction,
    rules: r => (r.rules?.enter ? `0 enter ${r.rules.direction}` : r.rules?.direction ? `1 ${r.rules.direction}` : '2 none'),
  })
  if (!s.scans.length) return <p className="text-xs text-gray-500">The first scan runs about a minute after start-up, then every {Math.round(s.sweep_s / 60)} minutes.</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead><tr className="text-[10px] uppercase tracking-wider text-gray-500 text-left">
          <th className="py-1.5 pr-3 font-normal"><SortHeader k="instrument" sort={t.sort} onSort={t.toggle}>Instrument</SortHeader></th>
          <th className="py-1.5 pr-3 font-normal text-right"><SortHeader k="price" sort={t.sort} onSort={t.toggle} align="right">Price</SortHeader></th>
          <th className="py-1.5 pr-3 font-normal text-right"><SortHeader k="conviction" sort={t.sort} onSort={t.toggle} align="right">Conviction</SortHeader></th>
          <th className="py-1.5 pr-3 font-normal"><SortHeader k="rules" sort={t.sort} onSort={t.toggle}>Rules say</SortHeader></th>
          <th className="py-1.5 font-normal">Why / Claude</th>
        </tr></thead>
        <tbody>
          {t.rows.map(r => (
            <tr key={r.symbol} className="border-t border-gray-800">
              <td className="py-1.5 pr-3 font-semibold text-gray-100">{r.base}</td>
              <td className="py-1.5 pr-3 text-right tabular-nums text-gray-300">{r.price.toPrecision(6)}</td>
              <td className={clsx('py-1.5 pr-3 text-right tabular-nums', tone(r.conviction))}>{r.conviction > 0 ? '+' : ''}{r.conviction.toFixed(2)}</td>
              <td className="py-1.5 pr-3">
                {!r.rules?.direction ? <span className="text-gray-500">no bias</span>
                  : r.rules.enter ? <span className="text-emerald-400 font-semibold">enter {r.rules.direction}</span>
                  : <span className="text-gray-300">{r.rules.direction}, waiting{r.rules.score != null ? ` (timing ${r.rules.score})` : ''}</span>}
              </td>
              <td className="py-1.5 text-gray-500 max-w-md truncate" title={[...(r.rules?.reasons ?? []), ...(r.rules?.timing ?? []), r.rules?.claude ?? ''].join(' · ')}>
                {r.rules?.claude ? <span className="text-gray-300">Claude: {r.rules.claude}</span> : (r.rules?.timing ?? r.rules?.reasons ?? []).join(' · ')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function MarketsPage() {
  const [market, setMarketState] = useState<MarketKey>(readMarket)
  const [data, setData] = useState<MarketSummary | null>(market !== 'crypto' ? memo[market] ?? null : null)
  const [crypto, setCrypto] = useState<BooksResponse | null>(memo.crypto ?? null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const req = useRef(0)

  const setMarket = (m: MarketKey) => {
    setMarketState(m)
    try { localStorage.setItem(KEY, m) } catch { /* private mode */ }
    window.history.replaceState(null, '', `#markets/${m}`)
  }
  const load = useCallback(async () => {
    const token = ++req.current
    try {
      if (market === 'crypto') {
        const b = await api.books.list()
        if (token === req.current) { memo.crypto = b; setCrypto(b); setError(null) }
      } else {
        const d = await api.markets.get(market)
        if (token === req.current) { memo[market] = d; setData(d); setError(null) }
      }
    } catch (e) {
      if (token === req.current) setError(e instanceof Error ? e.message : 'Could not load the market')
    }
  }, [market])
  useEffect(() => {
    if (market !== 'crypto') setData(memo[market] ?? null)
    load()
    const id = setInterval(load, 30_000)
    return () => clearInterval(id)
  }, [market, load])

  async function scanNow() {
    if (market === 'crypto') return
    setBusy(true)
    try { const d = await api.markets.sweep(market); memo[market] = d; setData(d) }
    catch (e) { setError(e instanceof Error ? e.message : 'Scan failed') }
    finally { setBusy(false) }
  }

  const info = MARKETS.find(m => m[0] === market)!
  return (
    <div className="space-y-4">
      <section className="card flex flex-wrap items-center gap-3">
        <Globe2 className="w-5 h-5 text-blue-400" />
        <label className="flex items-center gap-2 text-sm font-semibold text-white">
          Market
          <select value={market} onChange={e => setMarket(e.target.value as MarketKey)}
            className="bg-gray-800 text-gray-100 rounded-md px-2 py-1 text-sm outline-none focus:ring-1 focus:ring-blue-500">
            {MARKETS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
          </select>
        </label>
        <span className="text-[11px] text-gray-500">{info[2]}</span>
        {market !== 'crypto' && data && (
          <>
            <span className="text-xs text-gray-400 ml-auto">Last scan {ago(data.last_sweep)} · Claude {data.claude_calls_today}/{data.claude_daily} today
              {data.last_trend ? ` · trend rebalanced ${data.last_trend}` : ''}</span>
            <button onClick={scanNow} disabled={busy}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 disabled:opacity-50">
              <RefreshCw className={clsx('w-3.5 h-3.5', busy && 'animate-spin')} /> {busy ? 'Scanning…' : 'Scan now'}
            </button>
          </>
        )}
      </section>
      {error && <p className="text-xs text-red-400">{error}</p>}

      {market === 'crypto' ? (
        !crypto ? <div className="card h-40 animate-pulse" /> : (
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            {crypto.books.filter(b => CRYPTO_BOOKS.has(b.name)).map(b => (
              <BookCard key={b.name} b={b} busy={busy} onToggle={async on => {
                setBusy(true)
                try { await api.books.set(b.name, on); await load() }
                catch (e) { setError(e instanceof Error ? e.message : 'Switch failed') }
                finally { setBusy(false) }
              }} />
            ))}
          </div>
        )
      ) : !data ? <div className="card h-40 animate-pulse" /> : (
        <>
          <p className="text-[11px] text-gray-500">
            Paper experiments on Binance's {data.label.toLowerCase()} perpetuals. Research going in: textbook strategies and our own scanner showed
            no edge on stocks after costs; trend following made money on these markets over 2020–26 but no more than holding them.
            These books test it forward. Switch them on and off on the Books tab.
          </p>
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 items-start">
            {data.books.filter(b => b.kind === 'trend' || b.enabled || b.open.length > 0).map(b => <MarketBookCard key={b.name} b={b} />)}
          </div>
          <PicksCard s={data} />
          <section className="card space-y-2">
            <h3 className="text-sm font-semibold text-white">Latest scan</h3>
            <ScanTable s={data} />
          </section>
        </>
      )}
    </div>
  )
}
