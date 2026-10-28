import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { ArrowRight, ChevronDown, ChevronRight, Download, RefreshCw, RotateCcw } from 'lucide-react'
import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import clsx from 'clsx'
import { api } from '../utils/api'
import { RetiredSwitch } from './ui/RetiredSwitch'
import { TargetPanel } from './TargetPanel'
import { useIncludeRetired } from '../hooks/useIncludeRetired'
import { evenTicks } from '../utils/chart'
import { fmtPctSigned, fmtUsd, fmtUsdSigned } from '../utils/format'
import { fmtHold } from './TradesPage'
import type { OverviewKey, PortfolioOverview, SystemMetrics } from '../types/overview'

type Tone = 'good' | 'bad' | 'warn' | 'neutral'
const TONE: Record<Tone, string> = {
  good: 'text-emerald-400', bad: 'text-red-400', warn: 'text-yellow-400', neutral: 'text-gray-100',
}
// Wallets first, then the books; anything not listed follows in label order.
const FIRST: string[] = ['all', 'main', 'auto', 'main_live', 'auto_live', 'scalper', 'impulse', 'scalper_live']
const OPEN_KEY = 'oracle.portfolio.open'

const sign = (v: number | null | undefined, goodAbove = 0): Tone =>
  v == null ? 'neutral' : v > goodAbove ? 'good' : v < goodAbove ? 'bad' : 'neutral'
const num = (v: number | null | undefined, d = 2, suffix = '') => (v == null ? '—' : `${v.toFixed(d)}${suffix}`)

function readOpen(): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(OPEN_KEY) ?? '[]') as string[]) } catch { return new Set() }
}

interface Props {
  onOpenTrades: () => void
}

/** Portfolio: the whole $50k across every paper book, then one collapsible section per system. */
export function Portfolio({ onOpenTrades }: Props) {
  const [data, setData] = useState<PortfolioOverview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [includeRetired, setIncludeRetired] = useIncludeRetired()
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState<Set<string>>(readOpen)

  const load = useCallback(async (visible = false) => {
    if (visible) setLoading(true)
    try {
      setData(await api.portfolio.overview(includeRetired))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load the portfolio')
    } finally {
      setLoading(false)
    }
  }, [includeRetired])

  useEffect(() => {
    load(true)
    const id = setInterval(() => load(false), 30_000)
    return () => clearInterval(id)
  }, [load])

  const toggle = (k: string) => setOpen(s => {
    const n = new Set(s)
    n.has(k) ? n.delete(k) : n.add(k)
    try { localStorage.setItem(OPEN_KEY, JSON.stringify([...n])) } catch { /* private mode */ }
    return n
  })

  async function mainAction(kind: 'prices' | 'export' | 'reset') {
    if (busy) return
    if (kind === 'reset' && !confirm('Reset the main wallet? All its trades will be cleared.')) return
    setBusy(true)
    try {
      if (kind === 'prices') await api.paper.updatePrices()
      if (kind === 'export') await api.paper.exportCsv()
      if (kind === 'reset') await api.paper.reset(10000)
      await load()
    } finally {
      setBusy(false)
    }
  }

  if (error && !data) return <div className="text-sm px-4 py-3 rounded-lg border bg-red-900/30 border-red-800 text-red-400">{error}</div>
  if (!data) return <div className="card animate-pulse h-64" />

  const whole = data.systems.everything ?? data.systems.all
  const keys = (Object.keys(data.systems) as OverviewKey[]).filter(k => k !== 'everything')
  const ordered: OverviewKey[] = [
    ...keys.filter(k => FIRST.includes(k)).sort((a, b) => FIRST.indexOf(a) - FIRST.indexOf(b)),
    ...keys.filter(k => !FIRST.includes(k)).sort((a, b) => (data.systems[a]?.label ?? a).localeCompare(data.systems[b]?.label ?? b)),
  ]
  const allOpen = ordered.every(k => open.has(k))

  return (
    <div className={clsx('space-y-4 transition-opacity', loading && 'opacity-50')} aria-busy={loading}>
      <div className="flex justify-end">
        <RetiredSwitch on={includeRetired} onChange={setIncludeRetired} />
      </div>
      <TargetPanel />

      {/* The whole portfolio */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <h2 className="text-base font-bold text-white">Whole portfolio</h2>
          <span className="text-xs text-gray-500">every paper book together — live ledgers (Binance demo) are listed on their own below</span>
        </div>
        <Summary m={whole} />
        <EquityCard m={whole} curve={data.curves.everything ?? data.curves.all ?? []} />
        <MetricGroups m={whole} />
      </section>

      {/* Each system */}
      <section className="space-y-2">
        <div className="flex flex-wrap items-baseline gap-3">
          <h2 className="text-base font-bold text-white">By system</h2>
          <span className="text-xs text-gray-500">{ordered.length} systems · click a row to open it</span>
          <button onClick={() => { const n = new Set(allOpen ? [] : ordered); setOpen(n); try { localStorage.setItem(OPEN_KEY, JSON.stringify([...n])) } catch { /* private mode */ } }}
            className="ml-auto text-xs text-gray-400 hover:text-gray-200">{allOpen ? 'Collapse all' : 'Expand all'}</button>
        </div>
        {ordered.map(k => {
          const s = data.systems[k]!
          const isOpen = open.has(k)
          return (
            <section key={k} className={clsx('card p-0 overflow-hidden border', isOpen ? 'border-gray-700' : 'border-transparent')}>
              <button onClick={() => toggle(k)} aria-expanded={isOpen}
                className="w-full flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5 text-left hover:bg-gray-800/40 transition-colors">
                {isOpen ? <ChevronDown className="w-4 h-4 text-gray-500 shrink-0" /> : <ChevronRight className="w-4 h-4 text-gray-500 shrink-0" />}
                <span className="font-semibold text-white inline-flex items-center gap-1.5 min-w-[14rem]">
                  {k.endsWith('_live') && <span className="text-[9px] font-black tracking-wider bg-rose-600 text-white px-1.5 py-0.5 rounded">LIVE</span>}
                  {s.label}
                  {s.open_positions > 0 && <span className="text-[10px] bg-blue-600/80 text-white px-1.5 py-0.5 rounded-full">{s.open_positions} open</span>}
                </span>
                <span className="font-black text-white tabular-nums w-24 text-right">{fmtUsd(s.equity, 0)}</span>
                <span className={clsx('tabular-nums w-20 text-right text-sm', TONE[sign(s.total_return_pct)])}>{fmtPctSigned(s.total_return_pct)}</span>
                <span className="text-xs text-gray-500 tabular-nums">24h <span className={TONE[sign(s.pnl_24h)]}>{fmtUsdSigned(s.pnl_24h, 0)}</span></span>
                <span className="text-xs text-gray-500 tabular-nums">7d <span className={TONE[sign(s.pnl_7d)]}>{fmtUsdSigned(s.pnl_7d, 0)}</span></span>
                <span className="text-xs text-gray-500 tabular-nums">{s.trades} closed{s.win_rate != null ? ` · ${s.win_rate.toFixed(0)}% won` : ''}</span>
                <span className="ml-auto text-xs text-gray-600 tabular-nums">started at {fmtUsd(s.initial_balance, 0)}</span>
              </button>
              {isOpen && (
                <div className="px-4 pb-4 space-y-3 border-t border-gray-800/70 pt-3">
                  <EquityCard m={s} curve={data.curves[k] ?? []} />
                  <MetricGroups m={s} />
                  {k === 'main' && (
                    <div className="flex flex-wrap gap-3 text-xs">
                      <button onClick={() => mainAction('prices')} disabled={busy} className="inline-flex items-center gap-1 text-blue-400 hover:text-blue-300 disabled:opacity-50">
                        <RefreshCw className="w-3 h-3" /> Update prices
                      </button>
                      <button onClick={() => mainAction('export')} disabled={busy} className="inline-flex items-center gap-1 text-gray-400 hover:text-gray-200 disabled:opacity-50">
                        <Download className="w-3 h-3" /> Export CSV
                      </button>
                      <button onClick={() => mainAction('reset')} disabled={busy} className="inline-flex items-center gap-1 text-gray-500 hover:text-red-400 disabled:opacity-50">
                        <RotateCcw className="w-3 h-3" /> Reset
                      </button>
                    </div>
                  )}
                </div>
              )}
            </section>
          )
        })}
      </section>

      <button onClick={onOpenTrades} className="inline-flex items-center gap-1.5 text-sm font-semibold text-blue-400 hover:text-blue-300">
        See every trade <ArrowRight className="w-4 h-4" />
      </button>
    </div>
  )
}

/** The headline numbers for a system (or the whole portfolio). */
function Summary({ m }: { m: SystemMetrics }) {
  const cells: [string, ReactNode, Tone][] = [
    ['Equity', fmtUsd(m.equity, 0), 'neutral'],
    ['Since start', `${fmtUsdSigned((m.equity ?? 0) - (m.initial_balance ?? 0), 0)} · ${fmtPctSigned(m.total_return_pct)}`, sign(m.total_return_pct)],
    ['Today / 24h', fmtUsdSigned(m.pnl_24h, 0), sign(m.pnl_24h)],
    ['7 days', fmtUsdSigned(m.pnl_7d, 0), sign(m.pnl_7d)],
    ['30 days', fmtUsdSigned(m.pnl_30d, 0), sign(m.pnl_30d)],
    ['Open', `${m.open_positions} · ${fmtUsdSigned(m.unrealized_pnl, 0)}`, sign(m.unrealized_pnl)],
  ]
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
      {cells.map(([k, v, t]) => (
        <div key={k} className="card py-2.5">
          <div className="text-[10px] uppercase tracking-wider text-gray-500">{k}</div>
          <div className={clsx('text-lg font-black tabular-nums mt-0.5', TONE[t])}>{v}</div>
        </div>
      ))}
    </div>
  )
}

function MetricGroups({ m }: { m: SystemMetrics }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
      <Group title="Returns">
        <Metric label="Equity" value={fmtUsd(m.equity)} hint={`Started at ${fmtUsd(m.initial_balance, 0)}`} />
        <Metric label="Total return" value={fmtPctSigned(m.total_return_pct)} tone={sign(m.total_return_pct)} />
        <Metric label="Realized P&L" value={fmtUsdSigned(m.realized_pnl)} tone={sign(m.realized_pnl)} />
        <Metric label="Unrealized P&L" value={fmtUsdSigned(m.unrealized_pnl)} tone={sign(m.unrealized_pnl)} />
        <Metric label="Last 24h / 7d" value={`${fmtUsdSigned(m.pnl_24h, 0)} / ${fmtUsdSigned(m.pnl_7d, 0)}`} tone={sign(m.pnl_7d)} />
        <Metric label="Last 30 days" value={fmtUsdSigned(m.pnl_30d, 0)} tone={sign(m.pnl_30d)} />
      </Group>

      <Group title="Edge" note={`${m.trades} closed trades`}>
        <Metric label="Win rate" value={num(m.win_rate, 1, '%')}
          tone={m.win_rate == null || m.breakeven_win_rate == null ? 'neutral' : m.win_rate >= m.breakeven_win_rate ? 'good' : 'bad'}
          hint={m.breakeven_win_rate != null ? `Break-even at this payoff: ${m.breakeven_win_rate.toFixed(1)}%` : undefined} />
        <Metric label="Profit factor" value={num(m.profit_factor)} tone={sign(m.profit_factor, 1)}
          hint="Gross wins ÷ gross losses. Above 1 makes money." />
        <Metric label="Payoff ratio" value={num(m.payoff_ratio)} hint="Average win ÷ average loss" />
        <Metric label="Expectancy" value={fmtUsdSigned(m.expectancy_usd)} tone={sign(m.expectancy_usd)} hint="Average net P&L per trade" />
        <Metric label="Average R" value={m.avg_r == null ? '—' : `${m.avg_r > 0 ? '+' : ''}${m.avg_r.toFixed(2)}R`} tone={sign(m.avg_r)}
          hint="Net P&L per trade in units of the risk taken — comparable across position sizes" />
        <Metric label="Avg win / loss" value={`${fmtUsdSigned(m.avg_win, 0)} / ${fmtUsdSigned(m.avg_loss, 0)}`} />
      </Group>

      <Group title="Risk">
        <Metric label="Max drawdown" value={`${num(m.max_drawdown_pct, 1, '%')} · ${fmtUsd(m.max_drawdown_usd, 0)}`}
          tone={(m.max_drawdown_pct ?? 0) > 20 ? 'bad' : (m.max_drawdown_pct ?? 0) > 10 ? 'warn' : 'neutral'}
          hint="Largest fall from a peak in equity" />
        <Metric label="Current drawdown" value={num(m.current_drawdown_pct, 1, '%')}
          tone={(m.current_drawdown_pct ?? 0) > 0 ? 'warn' : 'good'} />
        <Metric label="Longest drawdown" value={m.longest_drawdown_h == null ? '—' : fmtHold(m.longest_drawdown_h * 60)}
          hint="Longest time spent below a previous peak" />
        <Metric label="Recovery factor" value={num(m.recovery_factor)} tone={sign(m.recovery_factor, 1)}
          hint="Net profit ÷ max drawdown" />
        <Metric label="Sharpe / Sortino" value={`${num(m.sharpe)} / ${num(m.sortino)}`}
          hint={m.sharpe == null ? `Needs 5+ days of equity history (have ${m.days})` : 'Annualised from daily returns'} />
        <Metric label="Best / worst trade" value={`${fmtUsdSigned(m.best_trade, 0)} / ${fmtUsdSigned(m.worst_trade, 0)}`} />
      </Group>

      <Group title="Costs">
        <Metric label="Gross P&L" value={fmtUsdSigned(m.gross_pnl)} tone={sign(m.gross_pnl)} hint="Price moves alone, before fees and funding" />
        <Metric label="Fees" value={fmtUsd(m.fees)} />
        <Metric label="Funding" value={fmtUsdSigned(-(m.funding ?? 0))} tone={sign(-(m.funding ?? 0))} hint="Positive = received" />
        <Metric label="Costs vs gross wins" value={num(m.costs_pct_of_gross_wins, 1, '%')}
          tone={(m.costs_pct_of_gross_wins ?? 0) > 50 ? 'bad' : (m.costs_pct_of_gross_wins ?? 0) > 25 ? 'warn' : 'neutral'}
          hint="Share of everything the winners made that went to fees and funding" />
      </Group>

      <Group title="Exposure now" note={`${m.open_positions} open`}>
        <Metric label="Long / short notional" value={`${fmtUsd(m.exposure.long_notional, 0)} / ${fmtUsd(m.exposure.short_notional, 0)}`} />
        <Metric label="Net notional" value={fmtUsdSigned(m.exposure.net_notional, 0)} />
        <Metric label="Gross leverage" value={num(m.exposure.gross_leverage, 2, '×')} hint="Open notional ÷ equity"
          tone={(m.exposure.gross_leverage ?? 0) > 3 ? 'warn' : 'neutral'} />
        <Metric label="Margin used" value={fmtUsd(m.exposure.margin_used, 0)} />
        <Metric label="Open risk" value={`${fmtUsd(m.exposure.open_risk_usd, 0)} · ${num(m.exposure.open_risk_pct, 2, '%')}`}
          hint="Loss if every open stop is hit, as a share of equity"
          tone={(m.exposure.open_risk_pct ?? 0) > 4 ? 'bad' : (m.exposure.open_risk_pct ?? 0) > 2 ? 'warn' : 'neutral'} />
      </Group>

      <Group title="Behaviour">
        <Metric label="Current streak" value={m.current_streak === 0 ? '—' : `${Math.abs(m.current_streak)} ${m.current_streak > 0 ? 'wins' : 'losses'}`}
          tone={m.current_streak > 0 ? 'good' : m.current_streak < 0 ? 'bad' : 'neutral'} />
        <Metric label="Longest win / loss run" value={`${m.longest_win_streak} / ${m.longest_loss_streak}`} />
        <Metric label="Avg hold: wins / losses" value={`${fmtHold(m.avg_hold_win_min)} / ${fmtHold(m.avg_hold_loss_min)}`}
          hint="Cutting winners short and holding losers shows up here" />
        <Metric label="Longs" value={sideText(m.long)} tone={sign(m.long.net_pnl)} />
        <Metric label="Shorts" value={sideText(m.short)} tone={sign(m.short.net_pnl)} />
        <Metric label="Best / worst coin"
          value={m.best_symbol ? `${m.best_symbol.symbol.replace('USDT', '')} ${fmtUsdSigned(m.best_symbol.net_pnl, 0)} / ${m.worst_symbol?.symbol.replace('USDT', '')} ${fmtUsdSigned(m.worst_symbol?.net_pnl, 0)}` : '—'} />
      </Group>
    </div>
  )
}

function sideText(s: { trades: number; net_pnl: number | null; win_rate: number | null }): string {
  if (!s.trades) return 'none'
  return `${s.trades} · ${s.win_rate?.toFixed(0)}% won · ${fmtUsdSigned(s.net_pnl, 0)}`
}

function Group({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <section className="card">
      <div className="flex items-baseline justify-between mb-2">
        <h3 className="text-xs font-bold uppercase tracking-wider text-gray-400">{title}</h3>
        {note && <span className="text-[11px] text-gray-500">{note}</span>}
      </div>
      <dl className="divide-y divide-gray-800/70">{children}</dl>
    </section>
  )
}

function Metric({ label, value, hint, tone = 'neutral' }: { label: string; value: string; hint?: string; tone?: Tone }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <dt className="text-xs text-gray-400">
        {label}
        {hint && <div className="text-[10px] text-gray-600 leading-snug">{hint}</div>}
      </dt>
      <dd className={clsx('text-sm font-semibold tabular-nums text-right whitespace-nowrap', TONE[tone])}>{value}</dd>
    </div>
  )
}

function EquityCard({ m, curve }: { m: SystemMetrics; curve: { ts: number; equity: number }[] }) {
  if (curve.length < 2) {
    return <div className="card text-sm text-gray-500">No equity history yet for {m.label}.</div>
  }
  // Drawdown below the running peak, as a negative %, so it reads as a hole under zero.
  let peak = -Infinity
  const rows = curve.map(p => {
    peak = Math.max(peak, p.equity)
    return { ts: p.ts * 1000, equity: p.equity, dd: peak > 0 ? -(peak - p.equity) / peak * 100 : 0 }
  })
  const up = (m.equity ?? 0) >= (m.initial_balance ?? 0)
  const color = up ? '#10b981' : '#ef4444'
  const ticks = evenTicks(rows.map(r => r.ts), 6)
  const fmtT = (t: number) => new Date(t).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', hour12: false })
  const tip = { background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }
  const gid = `eq-${m.label.replace(/[^a-z0-9]/gi, '')}`
  return (
    <section className="card">
      <div className="flex items-baseline justify-between mb-2">
        <h3 className="font-bold text-white text-sm">{m.label} — equity and drawdown</h3>
        <span className="text-xs text-gray-500">dashed line = starting balance</span>
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <AreaChart data={rows} syncId={gid} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={color} stopOpacity={0.3} />
              <stop offset="95%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" vertical={false} />
          <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']} ticks={ticks} tickFormatter={fmtT}
            tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} axisLine={false} hide />
          <YAxis domain={['auto', 'auto']} width={72} tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} axisLine={false}
            tickFormatter={v => fmtUsd(v, 0)} />
          <Tooltip contentStyle={tip} labelFormatter={t => fmtT(Number(t))} formatter={(v: number) => [fmtUsd(v), 'Equity']} />
          <ReferenceLine y={m.initial_balance ?? 0} stroke="#4b5563" strokeDasharray="3 3" />
          <Area type="monotone" dataKey="equity" stroke={color} strokeWidth={2} fill={`url(#${gid})`} dot={false} />
        </AreaChart>
      </ResponsiveContainer>
      <ResponsiveContainer width="100%" height={80}>
        <AreaChart data={rows} syncId={gid} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" vertical={false} />
          <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']} ticks={ticks} tickFormatter={fmtT}
            tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} axisLine={false} />
          <YAxis width={72} tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} axisLine={false}
            tickFormatter={v => `${Number(v).toFixed(0)}%`} domain={['dataMin', 0]} />
          <Tooltip contentStyle={tip} labelFormatter={t => fmtT(Number(t))} formatter={(v: number) => [`${v.toFixed(2)}%`, 'Drawdown']} />
          <Area type="monotone" dataKey="dd" stroke="#f87171" strokeWidth={1.5} fill="#7f1d1d" fillOpacity={0.35} dot={false} />
        </AreaChart>
      </ResponsiveContainer>
    </section>
  )
}
