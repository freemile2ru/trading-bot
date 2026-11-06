import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Download, RefreshCw } from 'lucide-react'
import clsx from 'clsx'
import { api, historyQuery } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import { DataTable, type Column } from './ui/DataTable'
import { SYSTEM_LABEL, type SystemKey, type TradeAnalytics, type TradeFilters, type TradesResponse, type UnifiedOrder, type UnifiedTrade } from '../types/overview'
import { OpenPositions } from './trades/OpenPositions'
import { TradeDrawer } from './TradeDrawer'
import { RetiredSwitch } from './ui/RetiredSwitch'
import { useIncludeRetired } from '../hooks/useIncludeRetired'
import { TradeCharts, TradeResearch } from './TradeAnalytics'

const SYSTEM_TONE: Record<SystemKey, string> = {
  auction: 'bg-amber-900/30 text-amber-200 border-amber-800/60',
  copy_crypto_wide: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dotted', copy_crypto_fast: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dotted', copy_crypto_late: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dotted', copy_stocks_wide: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dotted', copy_stocks_fast: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dotted', copy_stocks_late: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dotted', copy_futures_wide: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dotted', copy_futures_fast: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dotted', copy_futures_late: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dotted',
  main: 'bg-blue-900/40 text-blue-300 border-blue-800/60',
  auto: 'bg-violet-900/40 text-violet-300 border-violet-800/60',
  scalper: 'bg-amber-900/30 text-amber-300 border-amber-800/60',
  impulse: 'bg-teal-900/30 text-teal-300 border-teal-800/60',
  // Live systems: real money on Binance — loud on purpose.
  main_live: 'bg-rose-600/90 text-white border-rose-400',
  auto_live: 'bg-rose-600/90 text-white border-rose-400',
  scalper_live: 'bg-rose-600/90 text-white border-rose-400',
  // Side experiments (Lab tab): neutral, dashed — they're not real trading books.
  auto_rules: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  signals_claude: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  signals_rules: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  claude_swing: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  hold72: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  fade24: 'bg-violet-950/60 text-violet-300 border-violet-800 border-dashed',
  events: 'bg-orange-950/60 text-orange-300 border-orange-800 border-dashed',
  weekly_rs: 'bg-teal-950/60 text-teal-300 border-teal-800 border-dashed',
  cascade: 'bg-pink-950/60 text-pink-300 border-pink-800 border-dashed',
  carry_q: 'bg-yellow-950/60 text-yellow-300 border-yellow-800 border-dashed',
  unlocks: 'bg-red-950/60 text-red-300 border-red-800 border-dashed',
  vol_premium: 'bg-indigo-950/60 text-indigo-300 border-indigo-800 border-dashed',
  copy_trading: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dashed',
  multi_factor: 'bg-lime-950/60 text-lime-300 border-lime-800 border-dashed',
  copy_stocks: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dashed',
  copy_futures: 'bg-cyan-950/60 text-cyan-300 border-cyan-800 border-dashed',
  carry: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  stocks_trend: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  stocks_rules: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  stocks_claude: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  futures_trend: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  futures_rules: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
  futures_claude: 'bg-gray-800 text-gray-300 border-gray-600 border-dashed',
}
const MODES = [['', 'Paper & live'], ['paper', 'Paper'], ['live', 'Live']] as const
const PERIODS = [['', 'All time'], ['1', '24h'], ['7', '7 days'], ['30', '30 days']] as const
const VIEWS = [['table', 'Trades'], ['charts', 'Charts'], ['research', 'Research']] as const
type View = typeof VIEWS[number][0]

export function fmtHold(min: number | null): string {
  if (min == null) return '—'
  if (min < 1) return `${Math.round(min * 60)}s`
  if (min < 60) return `${min.toFixed(min < 10 ? 1 : 0)}m`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h}h ${Math.round(min % 60)}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

function fmtPrice(p: number | null): string {
  if (p == null) return '—'
  return p.toLocaleString(undefined, { maximumFractionDigits: p >= 100 ? 2 : p >= 1 ? 4 : 6 })
}

function fmtWhen(ts: number | null): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}

const LANE_LABEL: Record<string, string> = { ta: 'scanner', events: 'event short', unlocks: 'unlock short', weekly: 'weekly basket', cascade: 'cascade fade', factor: 'multi-factor', reversal: 'reversal score' }

function SystemBadge({ system, lane }: { system: SystemKey; lane?: string | null }) {
  return (
    <span className={clsx('px-1.5 py-0.5 rounded border text-[10px] font-semibold whitespace-nowrap', SYSTEM_TONE[system])}
      title={lane ? `Lane: ${LANE_LABEL[lane] ?? lane}` : undefined}>
      {SYSTEM_LABEL[system]}{lane && <span className="font-normal opacity-80"> · {LANE_LABEL[lane] ?? lane}</span>}
    </span>
  )
}

function Side({ d }: { d: string }) {
  return <span className={clsx('font-semibold capitalize', d === 'long' ? 'text-emerald-400' : 'text-red-400')}>{d}</span>
}

function Pnl({ v, strong }: { v: number | null; strong?: boolean }) {
  return (
    <span className={clsx(v == null ? 'text-gray-600' : v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : 'text-gray-400',
      strong && 'font-semibold')}>
      {fmtUsdSigned(v)}
    </span>
  )
}

/** Won / Won before costs / Lost — the middle one is where fees turned a good call into a loss. */
function Result({ t }: { t: UnifiedTrade }) {
  const net = t.net_pnl ?? 0
  const gross = t.gross_pnl ?? 0
  const [label, tone] = net > 0 ? ['Won', 'bg-emerald-900/40 text-emerald-400']
    : gross > 0 ? ['Won before costs', 'bg-yellow-900/40 text-yellow-400'] : ['Lost', 'bg-red-900/40 text-red-400']
  return <span className={clsx('px-1.5 py-0.5 rounded text-[10px] font-semibold whitespace-nowrap', tone)}>{label}</span>
}

function Reason({ r }: { r: string | null }) {
  if (!r) return <span className="text-gray-600">—</span>
  const tone = r.startsWith('take_profit') || r === 'trailing_stop' ? 'text-emerald-400'
    : r.startsWith('stop') || r === 'tightened_stop' || r === 'liquidated' ? 'text-red-400' : 'text-gray-400'
  return <span className={clsx('capitalize whitespace-nowrap', tone)}>{r.replace(/_/g, ' ')}</span>
}

function Segmented<T extends string>({ value, options, onChange }: {
  value: T; options: readonly (readonly [T, string])[]; onChange: (v: T) => void
}) {
  return (
    <div className="inline-flex max-w-full overflow-x-auto rounded-lg bg-gray-800/70 p-0.5">
      {options.map(([v, label]) => (
        <button key={v} onClick={() => onChange(v)}
          className={clsx('px-2.5 py-1 rounded-md text-xs font-semibold transition-colors whitespace-nowrap',
            value === v ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-gray-200')}>
          {label}
        </button>
      ))}
    </div>
  )
}

function Select({ value, onChange, children, label }: {
  value: string; onChange: (v: string) => void; children: React.ReactNode; label: string
}) {
  return (
    <select aria-label={label} value={value} onChange={e => onChange(e.target.value)}
      className="bg-gray-800 border border-gray-700 rounded-lg text-xs text-gray-200 px-2 py-1.5 focus:outline-none focus:border-blue-500">
      {children}
    </select>
  )
}

// ── Page ───────────────────────────────────────────────────────────────────────

export function TradesPage() {
  const [system, setSystem] = useState<SystemKey | ''>('')
  const [mode, setMode] = useState<'' | 'paper' | 'live'>('')
  const [filters, setFilters] = useState<TradeFilters>({ sort: 'closed_ts', order: 'desc', page: 1, page_size: 50 })
  const [period, setPeriod] = useState('')
  const [closed, setClosed] = useState<TradesResponse | null>(null)
  const [open, setOpen] = useState<TradesResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [orders, setOrders] = useState<UnifiedOrder[]>([])
  const [notice, setNotice] = useState<string | null>(null)
  const [drawer, setDrawer] = useState<{ system: SystemKey; id: string } | null>(null)
  const [includeRetired, setIncludeRetired] = useIncludeRetired()
  // The view lives in the URL (#trades/charts) so it survives a reload and can be bookmarked.
  const [view, setViewState] = useState<View>(() => {
    const sub = window.location.hash.split('/')[1] as View
    return VIEWS.some(([k]) => k === sub) ? sub : 'table'
  })
  const setView = (v: View) => { setViewState(v); window.history.replaceState(null, '', v === 'table' ? '#trades' : `#trades/${v}`) }
  const [analytics, setAnalytics] = useState<TradeAnalytics | null>(null)
  // Shown only for loads the user caused (a filter, a view switch) — the 15s background refresh
  // stays silent so the page doesn't flicker.
  const [loading, setLoading] = useState(false)
  const [analyticsLoading, setAnalyticsLoading] = useState(false)
  const latest = useRef(0)

  const query = useMemo<TradeFilters>(() => ({
    ...filters, system, mode, status: 'closed',
    date_from: period ? Math.floor(Date.now() / 1000 - Number(period) * 86400) : undefined,
    include_retired: includeRetired || undefined,
  }), [filters, system, mode, period, includeRetired])

  const load = useCallback(async (visible = false) => {
    // Only the newest request may update the page: a slow reply for the previous filter
    // arriving after the new one used to overwrite it.
    const req = ++latest.current
    if (visible) setLoading(true)
    try {
      const [c, o, ord] = await Promise.all([
        api.trades.all(query),
        api.trades.all({ system, mode, status: 'open', sort: 'opened_ts', order: 'desc', page_size: 200,
                         include_retired: includeRetired || undefined }),
        api.positions.orders(),
      ])
      if (req !== latest.current) return
      setClosed(c)
      setOpen(o)
      setOrders(ord.filter(x => (!system || x.system === system) && (!mode || (x.system.endsWith('_live') ? 'live' : 'paper') === mode)))
      setError(null)
    } catch (e) {
      if (req === latest.current) setError(e instanceof Error ? e.message : 'Could not load trades')
    } finally {
      if (req === latest.current) setLoading(false)
    }
  }, [query, system, mode, includeRetired])

  useEffect(() => {
    load(true)
    const id = setInterval(() => load(false), 15_000)
    return () => clearInterval(id)
  }, [load])

  // Charts and research use the same filters as the table (paging and sorting aside).
  useEffect(() => {
    if (view === 'table') return
    let cancelled = false
    setAnalyticsLoading(true)
    const fetchIt = () => api.trades.analytics(query)
      .then(a => { if (!cancelled) setAnalytics(a) })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load analytics') })
      .finally(() => { if (!cancelled) setAnalyticsLoading(false) })
    fetchIt()
    const id = setInterval(fetchIt, 60_000)
    return () => { cancelled = true; clearInterval(id) }
  }, [view, query])

  const set = (patch: Partial<TradeFilters>) => setFilters(f => ({ ...f, page: 1, ...patch }))
  const sortBy = (key: string) => setFilters(f => ({
    ...f, page: 1, sort: key, order: f.sort === key && f.order === 'desc' ? 'asc' : 'desc',
  }))

  const changed = (msg?: string) => {
    if (msg) { setNotice(msg); setTimeout(() => setNotice(null), 4000) }
    load()
  }

  // Every column sorts; the API does the sorting so paging stays consistent (sortKey = its field).
  const closedCols: Column<UnifiedTrade>[] = [
    { key: 'closed', header: 'Closed', sortKey: 'closed_ts', render: t => <span className="text-gray-400 whitespace-nowrap">{fmtWhen(t.closed_ts)}</span> },
    { key: 'sys', header: 'System', sortKey: 'system', render: t => <SystemBadge system={t.system} lane={t.lane} /> },
    { key: 'sym', header: 'Symbol', sortKey: 'symbol', render: t => <span className="font-mono font-semibold text-white" title={t.note || undefined}>{t.symbol.replace('USDT', '')}</span> },
    { key: 'side', header: 'Side', sortKey: 'direction', render: t => <Side d={t.direction} /> },
    { key: 'hold', header: 'Held', sortKey: 'hold_min', align: 'right', render: t => fmtHold(t.hold_min) },
    { key: 'px', header: 'Entry → exit', sortKey: 'entry_price', align: 'right', render: t => <span className="text-gray-300">{fmtPrice(t.entry_price)} <span className="text-gray-600">→</span> {fmtPrice(t.exit_price)}</span> },
    { key: 'size', header: 'Notional', sortKey: 'notional', align: 'right', render: t => <span>{fmtUsd(t.notional, 0)}{t.leverage && t.leverage > 1 ? <span className="text-gray-500"> · {t.leverage}x</span> : null}</span> },
    { key: 'margin', header: 'Margin', sortKey: 'margin', align: 'right', title: 'Collateral the position held (notional ÷ leverage)', render: t => <span className="text-gray-300">{t.margin == null ? '—' : fmtUsd(t.margin, 0)}</span> },
    { key: 'gross', header: 'Gross', sortKey: 'gross_pnl', align: 'right', title: 'Price move alone, before fees and funding', render: t => <Pnl v={t.gross_pnl} /> },
    { key: 'cost', header: 'Costs', sortKey: 'costs', align: 'right', title: 'Fees + funding paid', render: t => <span className="text-gray-400">{fmtUsd((t.fees ?? 0) + (t.funding ?? 0))}</span> },
    { key: 'net', header: 'Net', sortKey: 'net_pnl', align: 'right', render: t => <Pnl v={t.net_pnl} strong /> },
    { key: 'r', header: 'R', sortKey: 'r', align: 'right', title: 'Net P&L ÷ risk taken at entry', render: t => <span className={clsx((t.r ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400')}>{t.r == null ? '—' : `${t.r > 0 ? '+' : ''}${t.r.toFixed(2)}R`}</span> },
    { key: 'why', header: 'Exit', sortKey: 'exit_reason', render: t => <Reason r={t.exit_reason} /> },
    { key: 'res', header: 'Result', sortKey: 'outcome', render: t => <Result t={t} /> },
  ]

  // Turning retired strategies off while one is selected would leave an empty page — go back to all.
  useEffect(() => {
    if (!includeRetired && system && closed?.retired_systems && system in closed.retired_systems) setSystem('')
  }, [includeRetired, system, closed?.retired_systems])

  const tot = closed?.totals
  const openCount = open?.open_count
  // Live systems appear once they exist (the backend only reports systems that have traded).
  const systemOptions: (readonly [SystemKey | '', string])[] = [
    ['', 'All systems'],
    ...(Object.keys(openCount ?? { main: 0, auto: 0, scalper: 0 }) as SystemKey[]).map(k => [k, SYSTEM_LABEL[k]] as const),
  ]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Segmented value={system} options={systemOptions} onChange={v => { setSystem(v); set({}) }} />
        <Segmented value={mode} options={MODES} onChange={v => { setMode(v); set({}) }} />
        <RetiredSwitch on={includeRetired} onChange={v => { setIncludeRetired(v); set({}) }} hidden={closed?.retired_hidden} />
        {openCount && (
          <span className="text-xs text-gray-500">
            Open: {Object.entries(openCount).map(([k, n]) => `${SYSTEM_LABEL[k as SystemKey]} ${n}`).join(' · ')}
          </span>
        )}
        <button onClick={() => load(true)} disabled={loading} className="ml-auto inline-flex items-center gap-1.5 text-xs text-gray-400 hover:text-white disabled:opacity-60">
          <RefreshCw className={clsx('w-3.5 h-3.5', loading && 'animate-spin')} /> {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {error && <div className="text-sm px-4 py-2 rounded-lg border bg-red-900/30 border-red-800 text-red-400">{error}</div>}

      {notice && <div className="text-sm px-4 py-2 rounded-lg border bg-emerald-900/30 border-emerald-800 text-emerald-300">{notice}</div>}
      <div className={clsx('relative transition-opacity', loading && 'opacity-50 pointer-events-none')} aria-busy={loading}>
        {loading && <LoadingBar />}
        <OpenPositions positions={open?.rows ?? []} orders={orders} onChanged={changed} onOpen={(sys, id) => setDrawer({ system: sys, id })} />
      </div>

      <section className="card p-0 overflow-hidden relative" aria-busy={loading}>
        {loading && <LoadingBar />}
        <div className="px-4 pt-4 pb-3 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-bold text-white">Closed trades</h2>
            <span className="text-xs text-gray-500">{closed?.total ?? 0} matching</span>
            <div className="ml-auto flex items-center gap-2">
              <a href={`/api/trades/all.csv?${historyQuery({ system, mode, status: 'closed', symbol: filters.symbol, direction: filters.direction,
                                                            result: filters.result, exit_reason: filters.exit_reason, date_from: query.date_from,
                                                            include_retired: includeRetired || undefined })}`}
                 className="inline-flex items-center gap-1.5 text-xs text-gray-400 hover:text-white px-2 py-1" title="Download every matching trade as CSV">
                <Download className="w-3.5 h-3.5" /> CSV
              </a>
              <Segmented value={view} options={VIEWS} onChange={setView} />
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Segmented value={period} options={PERIODS} onChange={v => { setPeriod(v); set({}) }} />
            <Select label="Symbol" value={filters.symbol ?? ''} onChange={v => set({ symbol: v || undefined })}>
              <option value="">All symbols</option>
              {closed?.facets.symbols.map(s => <option key={s} value={s}>{s.replace('USDT', '')}</option>)}
            </Select>
            <Select label="Side" value={filters.direction ?? ''} onChange={v => set({ direction: v as TradeFilters['direction'] })}>
              <option value="">Long & short</option><option value="long">Long</option><option value="short">Short</option>
            </Select>
            <Select label="Result" value={filters.result ?? ''} onChange={v => set({ result: v as TradeFilters['result'] })}>
              <option value="">Wins & losses</option><option value="win">Wins</option><option value="loss">Losses</option>
            </Select>
            <Select label="Exit" value={filters.exit_reason ?? ''} onChange={v => set({ exit_reason: v || undefined })}>
              <option value="">Any exit</option>
              {closed?.facets.exit_reasons.map(r => <option key={r} value={r}>{r.replace(/_/g, ' ')}</option>)}
            </Select>
          </div>
          {tot && tot.trades > 0 && (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 text-xs">
              <Total label="Trades" value={tot.trades.toLocaleString()} />
              <Total label="Win rate" value={`${(tot.wins / tot.trades * 100).toFixed(1)}%`} />
              <Total label="Gross" value={<Pnl v={tot.gross_pnl} />} />
              <Total label="Fees" value={fmtUsd(tot.fees)} />
              <Total label="Funding" value={fmtUsd(tot.funding)} />
              <Total label="Net" value={<Pnl v={tot.net_pnl} strong />} />
            </div>
          )}
        </div>
        {view === 'table' && <div className={clsx('transition-opacity', loading && 'opacity-40 pointer-events-none')}><DataTable columns={closedCols} rows={closed?.rows ?? []} rowKey={t => `${t.system}:${t.id}`} dense
          sort={filters.sort ? { key: filters.sort, dir: filters.order === 'asc' ? 'asc' : 'desc' } : null} onSort={sortBy}
          onRowClick={t => setDrawer({ system: t.system, id: t.id })}
          empty={closed ? 'No trades match these filters.' : 'Loading…'}
          footer={closed && closed.pages > 1 && (
            <div className="flex items-center justify-end gap-2 px-4 py-3 text-xs text-gray-400 border-t border-gray-800">
              <button onClick={() => setFilters(f => ({ ...f, page: Math.max(1, (f.page ?? 1) - 1) }))}
                disabled={closed.page <= 1} className="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 disabled:opacity-40">Previous</button>
              <span className="tabular-nums px-1">Page {closed.page} of {closed.pages}</span>
              <button onClick={() => setFilters(f => ({ ...f, page: Math.min(closed.pages, (f.page ?? 1) + 1) }))}
                disabled={closed.page >= closed.pages} className="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 disabled:opacity-40">Next</button>
            </div>
          )} /></div>}
      </section>

      {drawer && <TradeDrawer system={drawer.system} id={drawer.id} onClose={() => setDrawer(null)} />}

      {view !== 'table' && (analytics ? (
        <div className="relative" aria-busy={analyticsLoading}>
          {analyticsLoading && (
            <div className="sticky top-2 z-10 flex justify-center pointer-events-none">
              <span className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-gray-800 border border-gray-700 text-xs text-gray-200 shadow-lg">
                <RefreshCw className="w-3.5 h-3.5 animate-spin" /> Updating for the new filters…
              </span>
            </div>
          )}
          <div className={clsx('transition-opacity', analyticsLoading && 'opacity-40')}>
            {view === 'charts' ? <TradeCharts data={analytics} /> : <TradeResearch data={analytics} />}
          </div>
        </div>
      ) : <SkeletonCards />)}
    </div>
  )
}

function Total({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="bg-gray-800/50 rounded-lg px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-gray-500">{label}</div>
      <div className="font-semibold text-gray-100 tabular-nums">{value}</div>
    </div>
  )
}

/** A thin indeterminate bar along the top edge of whatever is reloading. */
function LoadingBar() {
  return (
    <div className="absolute inset-x-0 top-0 h-0.5 overflow-hidden rounded-t-lg z-10" role="progressbar" aria-label="Loading">
      <div className="h-full w-1/3 bg-blue-500 animate-[loadingbar_1.1s_ease-in-out_infinite]" />
    </div>
  )
}

/** Placeholder shapes while the first charts load. */
function SkeletonCards() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading charts">
      <div className="card h-72 animate-pulse"><div className="h-4 w-40 bg-gray-800 rounded" /><div className="mt-6 h-48 bg-gray-800/60 rounded" /></div>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {[0, 1].map(i => <div key={i} className="card h-56 animate-pulse"><div className="h-4 w-32 bg-gray-800 rounded" /><div className="mt-6 h-32 bg-gray-800/60 rounded" /></div>)}
      </div>
    </div>
  )
}
