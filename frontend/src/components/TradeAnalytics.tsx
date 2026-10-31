import type { ReactNode } from 'react'
import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ReferenceLine, ResponsiveContainer, Scatter, ScatterChart,
  Tooltip, XAxis, YAxis, ZAxis,
} from 'recharts'
import clsx from 'clsx'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import { evenTicks } from '../utils/chart'
import { SYSTEM_LABEL, type GroupStat, type SystemKey, type TradeAnalytics } from '../types/overview'

// Categorical slots (dark steps of the validated reference palette), fixed per system so a
// system keeps its colour whatever the filter — never assigned by rank.
export const SYSTEM_COLOR: Record<SystemKey, string> = {
  // Copy-trading variants share the copy books' hue (secondary encoding tells them apart on the Copy tab).
  auction: '#c98500',
  copy_crypto_wide: '#2fb3c9', copy_crypto_fast: '#2fb3c9', copy_crypto_late: '#2fb3c9', copy_stocks_wide: '#2fb3c9', copy_stocks_fast: '#2fb3c9', copy_stocks_late: '#2fb3c9',
  copy_futures_wide: '#2fb3c9', copy_futures_fast: '#2fb3c9', copy_futures_late: '#2fb3c9',
  main: '#3987e5', auto: '#d95926', scalper: '#199e70', impulse: '#c98500',
  main_live: '#d55181', auto_live: '#9085e9', scalper_live: '#008300',
  // Experiments: slot 8 for Claude swing; the paired books reuse their partner's hue and are told
  // apart by dash pattern (secondary encoding) — a 9th+ generated hue would collide.
  claude_swing: '#e66767', auto_rules: '#d95926', signals_claude: '#3987e5', signals_rules: '#3987e5',
  // Hold-72 reuses the retired scalper's slot (hidden by default) — no new hue.
  hold72: '#199e70',
  fade24: '#9b6bd3',
  events: '#e08a3c',
  unlocks: '#d94f4f',
  vol_premium: '#7c7fe0',
  copy_trading: '#2fb3c9',
  multi_factor: '#8fbf3a',
  copy_stocks: '#2fb3c9', copy_futures: '#2fb3c9',
  weekly_rs: '#2bb3a3',
  cascade: '#d65a8c',
  carry_q: '#c9a227',
  // Carry shares Claude swing's slot, told apart by its dash pattern.
  carry: '#e66767',
  // Stocks books share Main's hue, futures books Impulse's old one; the kind is told apart by dash.
  stocks_trend: '#3987e5', stocks_rules: '#3987e5', stocks_claude: '#3987e5',
  futures_trend: '#c98500', futures_rules: '#c98500', futures_claude: '#c98500',
}
export const SYSTEM_DASH: Partial<Record<SystemKey, string>> = { copy_stocks: '6 3', copy_futures: '2 2', auto_rules: '6 3', signals_claude: '2 2', signals_rules: '6 3', hold72: '8 2 2 2', carry: '1 3',
  stocks_trend: '10 4', stocks_rules: '6 3', stocks_claude: '2 2', futures_trend: '10 4', futures_rules: '6 3', futures_claude: '2 2' }
// Win/loss follow the rest of the app (Portfolio, tables).
const WIN = '#10b981'
const LOSS = '#ef4444'
const GRID = '#1f2937'
const AXIS = { fill: '#6b7280', fontSize: 10 }
const TIP = { background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }
const fmtDay = (ts: number) => new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
const coin = (s: string) => s.replace('USDT', '')
const tone = (v: number | null | undefined) => (v == null || v === 0 ? 'text-gray-300' : v > 0 ? 'text-emerald-400' : 'text-red-400')

function Panel({ title, subtitle, children, className }: { title: string; subtitle?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={clsx('card', className)}>
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      {subtitle && <p className="text-[11px] text-gray-500 mt-0.5 mb-3">{subtitle}</p>}
      {!subtitle && <div className="mb-3" />}
      {children}
    </section>
  )
}

function Empty({ children = 'Not enough trades for this chart yet.' }: { children?: ReactNode }) {
  return <p className="text-xs text-gray-500 py-8 text-center">{children}</p>
}

/** Net P&L per group as a signed horizontal bar, with trades · win rate · avg R as text. */
function GroupBars({ rows, label, limit = 12, wide }: { rows: GroupStat[]; label: (k: GroupStat['key']) => string; limit?: number; wide?: boolean }) {
  if (!rows.length) return <Empty />
  const shown = [...rows].sort((a, b) => Math.abs(b.net_pnl) - Math.abs(a.net_pnl)).slice(0, limit)
    .sort((a, b) => b.net_pnl - a.net_pnl)
  const max = Math.max(...shown.map(r => Math.abs(r.net_pnl)), 1)
  return (
    <div className="space-y-1.5">
      {shown.map(r => (
        <div key={String(r.key)} className={clsx('grid items-center gap-2 text-xs', wide ? 'grid-cols-[9.5rem_minmax(0,1fr)_5.5rem]' : 'grid-cols-[6.5rem_minmax(0,1fr)_5.5rem]')}
             title={`${r.trades} trades · ${r.win_rate}% won · avg ${r.avg_r ?? '—'}R · gross ${fmtUsdSigned(r.gross_pnl)}`}>
          <span className="truncate text-gray-300">{label(r.key)}</span>
          <div className="relative h-4">
            <div className="absolute inset-y-0 left-1/2 w-px bg-gray-700" />
            <div className="absolute inset-y-0.5 rounded-sm"
                 style={{ background: r.net_pnl >= 0 ? WIN : LOSS,
                          ...(r.net_pnl >= 0 ? { left: '50%' } : { right: '50%' }),
                          width: `${(Math.abs(r.net_pnl) / max) * 50}%` }} />
          </div>
          <span className="text-right tabular-nums">
            <span className={tone(r.net_pnl)}>{fmtUsdSigned(r.net_pnl)}</span>
            <span className="block text-[10px] text-gray-500">{r.trades}× · {r.win_rate}%</span>
          </span>
        </div>
      ))}
      {rows.length > limit && <p className="text-[10px] text-gray-600">Largest {limit} of {rows.length} by size of P&L.</p>}
    </div>
  )
}

function CumulativeChart({ data }: { data: TradeAnalytics }) {
  const systems = data.systems.filter(s => (data.cumulative.by_system[s]?.length ?? 0) > 0)
  if (data.cumulative.total.length < 2) return <Empty />
  // One row per timestamp, each system carrying its running total forward.
  const events = systems.flatMap(s => (data.cumulative.by_system[s] ?? []).map(p => ({ ts: p.ts, s, pnl: p.pnl })))
    .sort((a, b) => a.ts - b.ts)
  const last: Partial<Record<SystemKey, number>> = {}
  const rows = events.map(e => { last[e.s] = e.pnl; return { ts: e.ts, ...last, total: undefined as number | undefined } })
  const totals = new Map(data.cumulative.total.map(p => [p.ts, p.pnl]))
  rows.forEach(r => { r.total = totals.get(r.ts) })
  const multi = systems.length > 1
  return (
    <>
      <ResponsiveContainer width="100%" height={240}>
        <LineChart data={rows} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']} ticks={evenTicks(rows.map(r => r.ts))}
                 tickFormatter={fmtDay} tick={AXIS} tickLine={false} axisLine={false} />
          <YAxis width={64} tick={AXIS} tickLine={false} axisLine={false} tickFormatter={v => fmtUsdSigned(v, 0)} />
          <ReferenceLine y={0} stroke="#4b5563" />
          <Tooltip contentStyle={TIP} labelFormatter={v => new Date(Number(v) * 1000).toLocaleString()}
                   formatter={(v: number, k: string) => [fmtUsdSigned(v), k === 'total' ? 'All selected' : SYSTEM_LABEL[k as SystemKey]]} />
          {systems.map(s => (
            <Line key={s} dataKey={s} stroke={SYSTEM_COLOR[s]} strokeDasharray={SYSTEM_DASH[s]} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
          ))}
          {multi && <Line dataKey="total" stroke="#e5e7eb" strokeWidth={1.5} strokeDasharray="4 3" dot={false} connectNulls isAnimationActive={false} />}
        </LineChart>
      </ResponsiveContainer>
      <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-[11px] text-gray-400">
        {systems.map(s => (
          <span key={s} className="inline-flex items-center gap-1.5">
            <span className="w-3 h-0.5 rounded" style={{ background: SYSTEM_COLOR[s] }} />
            {SYSTEM_LABEL[s]} <span className={tone(last[s])}>{fmtUsdSigned(last[s] ?? 0)}</span>
          </span>
        ))}
        {multi && <span className="inline-flex items-center gap-1.5"><span className="w-3 border-t border-dashed border-gray-300" />All selected</span>}
      </div>
    </>
  )
}

function DailyChart({ data }: { data: TradeAnalytics }) {
  if (!data.daily.length) return <Empty />
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data.daily} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="day" tick={AXIS} tickLine={false} axisLine={false} minTickGap={24}
               tickFormatter={d => new Date(`${d}T00:00:00Z`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} />
        <YAxis width={64} tick={AXIS} tickLine={false} axisLine={false} tickFormatter={v => fmtUsdSigned(v, 0)} />
        <ReferenceLine y={0} stroke="#4b5563" />
        <Tooltip contentStyle={TIP} cursor={{ fill: '#ffffff08' }}
                 formatter={(v: number, _k, p) => [`${fmtUsdSigned(v)} · ${p.payload.trades} trades, ${p.payload.wins} won`, 'Net']} />
        <Bar dataKey="net_pnl" radius={[3, 3, 0, 0]} isAnimationActive={false}>
          {data.daily.map(d => <Cell key={d.day} fill={d.net_pnl >= 0 ? WIN : LOSS} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

function RDistribution({ data }: { data: TradeAnalytics }) {
  const bins = data.r_distribution
  if (!bins.some(b => b.count)) return <Empty>No trades with a known risk (R) yet.</Empty>
  const avg = data.confidence.expectancy_r
  return (
    <>
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={bins} margin={{ top: 4, right: 8, left: 0, bottom: 0 }} barCategoryGap={2}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} interval={1} />
          <YAxis width={32} tick={AXIS} tickLine={false} axisLine={false} allowDecimals={false} />
          <Tooltip contentStyle={TIP} cursor={{ fill: '#ffffff08' }} formatter={(v: number) => [v, 'trades']} />
          <Bar dataKey="count" radius={[3, 3, 0, 0]} isAnimationActive={false}>
            {bins.map(b => <Cell key={b.label} fill={(b.hi ?? 1) <= 0 ? LOSS : WIN} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <p className="text-[11px] text-gray-500 mt-1">
        Average {avg == null ? '—' : `${avg > 0 ? '+' : ''}${avg.toFixed(2)}R`} per trade. A working system needs a fat right tail
        (a few +2R… +5R winners) to pay for a cluster of −1R losses.
      </p>
    </>
  )
}

function CostsWaterfall({ data }: { data: TradeAnalytics }) {
  const c = data.costs
  if (!data.trades) return <Empty />
  const steps = [
    { label: 'Gross', value: c.gross_pnl, base: 0, end: c.gross_pnl },
    { label: 'Fees', value: -c.fees, base: c.gross_pnl, end: c.gross_pnl - c.fees },
    { label: 'Funding', value: -c.funding, base: c.gross_pnl - c.fees, end: c.net_pnl },
    { label: 'Net', value: c.net_pnl, base: 0, end: c.net_pnl },
  ]
  const lo = Math.min(0, ...steps.map(s => Math.min(s.base, s.end)))
  const hi = Math.max(0, ...steps.map(s => Math.max(s.base, s.end)))
  const span = hi - lo || 1
  const y = (v: number) => ((hi - v) / span) * 100
  return (
    <>
      <div className="relative h-44 grid grid-cols-4 gap-3 px-1">
        <div className="absolute inset-x-0 border-t border-gray-600" style={{ top: `${y(0)}%` }} />
        {steps.map(s => {
          const top = y(Math.max(s.base, s.end))
          const height = Math.max(1, Math.abs(y(s.base) - y(s.end)))
          const colour = s.label === 'Fees' || s.label === 'Funding' ? '#6b7280' : s.value >= 0 ? WIN : LOSS
          return (
            <div key={s.label} className="relative" title={`${s.label}: ${fmtUsdSigned(s.value)}`}>
              <div className="absolute inset-x-1 rounded-sm" style={{ top: `${top}%`, height: `${height}%`, background: colour }} />
            </div>
          )
        })}
      </div>
      <div className="grid grid-cols-4 gap-3 mt-2 text-center text-[11px]">
        {steps.map(s => (
          <div key={s.label}><div className="text-gray-400">{s.label}</div><div className={clsx('tabular-nums', tone(s.value))}>{fmtUsdSigned(s.value)}</div></div>
        ))}
      </div>
      <p className="text-[11px] text-gray-500 mt-2">
        Costs took {c.gross_wins ? `${((c.fees + c.funding) / c.gross_wins * 100).toFixed(0)}%` : '—'} of everything the winners made
        before costs; {c.won_before_costs} trade{c.won_before_costs === 1 ? '' : 's'} were right on price but lost after fees.
      </p>
    </>
  )
}

function Heatmap({ data }: { data: TradeAnalytics }) {
  if (!data.heatmap.length) return <Empty />
  const cells = new Map(data.heatmap.map(c => [`${c.weekday}-${c.hour}`, c]))
  const max = Math.max(...data.heatmap.map(c => Math.abs(c.net_pnl)), 1)
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
  // Diverging: loss red ← grey (nothing) → win green; strength ∝ √|P&L| so small cells stay visible.
  const fill = (v: number) => {
    const a = Math.sqrt(Math.abs(v) / max)
    return v === 0 ? '#383835' : v > 0 ? `rgba(16,185,129,${0.2 + a * 0.8})` : `rgba(239,68,68,${0.2 + a * 0.8})`
  }
  return (
    <div className="overflow-x-auto">
      <div className="min-w-[34rem]">
        <div className="grid gap-[2px]" style={{ gridTemplateColumns: '2.25rem repeat(24, minmax(0, 1fr))' }}>
          <span />
          {Array.from({ length: 24 }, (_, h) => (
            <span key={h} className="text-[9px] text-gray-500 text-center">{h % 3 === 0 ? h : ''}</span>
          ))}
          {days.map((d, wd) => (
            <div key={d} className="contents">
              <span className="text-[10px] text-gray-400 self-center">{d}</span>
              {Array.from({ length: 24 }, (_, h) => {
                const c = cells.get(`${wd}-${h}`)
                return (
                  <div key={h} className="h-5 rounded-[3px]" style={{ background: c ? fill(c.net_pnl) : '#1a1f29' }}
                       title={c ? `${d} ${String(h).padStart(2, '0')}:00 UTC — ${c.trades} trades, ${fmtUsdSigned(c.net_pnl)}` : `${d} ${h}:00 — no trades`} />
                )
              })}
            </div>
          ))}
        </div>
        <p className="text-[10px] text-gray-500 mt-2">Entry time, UTC. Green made money, red lost it; stronger colour = bigger P&L. Hover a cell for details.</p>
      </div>
    </div>
  )
}

function RollingChart({ data }: { data: TradeAnalytics }) {
  const pts = data.rolling.points
  if (pts.length < 2) return <Empty>Needs at least 10 closed trades.</Empty>
  return (
    <>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={pts} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="n" type="number" domain={['dataMin', 'dataMax']} tick={AXIS} tickLine={false} axisLine={false}
                 ticks={evenTicks(pts.map(p => p.n))} tickFormatter={n => `#${n}`} />
          <YAxis width={40} tick={AXIS} tickLine={false} axisLine={false} tickFormatter={v => `${v}R`} />
          <ReferenceLine y={0} stroke="#4b5563" />
          <Tooltip contentStyle={TIP} labelFormatter={n => `Trades up to #${n}`}
                   formatter={(v: number, k: string, p) => k === 'avg_r'
                     ? [`${v > 0 ? '+' : ''}${v.toFixed(2)}R · ${p.payload.win_rate}% won`, `Last ${data.rolling.window} trades`] : [v, k]} />
          <Line dataKey="avg_r" stroke={SYSTEM_COLOR.main} strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <p className="text-[11px] text-gray-500 mt-1">Average R of the last {data.rolling.window} trades at each point. Staying above zero is the edge; a slide toward it is decay.</p>
    </>
  )
}

function EfficiencyChart({ data }: { data: TradeAnalytics }) {
  const e = data.efficiency
  if (e.points.length < 3) return <Empty>No trades with a recorded best move yet.</Empty>
  const systems = [...new Set(e.points.map(p => p.system))]
  // Scatter carries at most three categorical colours before hues collide; past that, one colour.
  const coloured = systems.length <= 3
  // Zoom to where the trades are: a couple of +25R / −11R outliers otherwise squash everything
  // into a corner. Outliers are pinned to the edge and counted.
  const q = (xs: number[], p: number) => [...xs].sort((a, b) => a - b)[Math.floor(p * (xs.length - 1))]
  const maxX = Math.max(1, Math.ceil(q(e.points.map(p => p.mfe_r), 0.98) * 1.1))
  const minY = Math.min(-1, Math.floor(q(e.points.map(p => p.r), 0.02) * 1.1))
  const maxY = Math.max(1, Math.ceil(q(e.points.map(p => p.r), 0.98) * 1.1))
  const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))
  const shown = e.points.map(p => ({ ...p, x: clamp(p.mfe_r, 0, maxX), y: clamp(p.r, minY, maxY) }))
  const pinned = shown.filter(p => p.x !== p.mfe_r || p.y !== p.r).length
  return (
    <>
      <ResponsiveContainer width="100%" height={220}>
        <ScatterChart margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" />
          <XAxis type="number" dataKey="x" name="Best move" tick={AXIS} tickLine={false} axisLine={false}
                 domain={[0, maxX]} tickFormatter={v => `${v}R`} />
          <YAxis type="number" dataKey="y" name="Result" width={40} tick={AXIS} tickLine={false} axisLine={false}
                 domain={[minY, maxY]} tickFormatter={v => `${v}R`} />
          <ZAxis range={[28, 28]} />
          <ReferenceLine y={0} stroke="#4b5563" />
          <ReferenceLine segment={[{ x: 0, y: 0 }, { x: Math.min(maxX, maxY), y: Math.min(maxX, maxY) }]} stroke="#6b7280" strokeDasharray="4 3" />
          <Tooltip contentStyle={TIP} cursor={{ strokeDasharray: '3 3' }}
                   content={({ payload }) => {
                     const p = payload?.[0]?.payload as (typeof shown)[number] | undefined
                     return p ? (
                       <div style={TIP} className="px-2 py-1.5 text-gray-200">
                         <div className="font-semibold">{coin(p.symbol)} · {SYSTEM_LABEL[p.system]}</div>
                         <div>best {p.mfe_r > 0 ? '+' : ''}{p.mfe_r}R → kept {p.r > 0 ? '+' : ''}{p.r}R</div>
                       </div>
                     ) : null
                   }} />
          {(coloured ? systems : ['all' as const]).map(s => (
            <Scatter key={s} data={s === 'all' ? shown : shown.filter(p => p.system === s)}
                     fill={s === 'all' ? SYSTEM_COLOR.main : SYSTEM_COLOR[s]} fillOpacity={0.7} stroke="#111827" strokeWidth={1}
                     isAnimationActive={false} />
          ))}
        </ScatterChart>
      </ResponsiveContainer>
      <div className="flex flex-wrap gap-x-4 gap-y-1 mt-1 text-[11px] text-gray-400">
        {coloured && systems.map(s => (
          <span key={s} className="inline-flex items-center gap-1.5"><span className="w-2 h-2 rounded-full" style={{ background: SYSTEM_COLOR[s] }} />{SYSTEM_LABEL[s]}</span>
        ))}
      </div>
      <p className="text-[11px] text-gray-500 mt-1">
        Each dot is a trade: how far it ran our way (→) against what it finished with (↑). The dashed line is "kept it all".
        Average best move {e.avg_best_r ?? '—'}R, kept {e.avg_kept_r ?? '—'}R; {e.reached_1r_then_lost} of {e.reached_1r} trades that
        reached +1R ended in a loss.{pinned ? ` ${pinned} outlier${pinned === 1 ? '' : 's'} pinned to the edge.` : ''}
      </p>
    </>
  )
}

export function TradeCharts({ data }: { data: TradeAnalytics }) {
  if (!data.trades) return <div className="card text-sm text-gray-500">No closed trades match these filters.</div>
  const hourLabel = (h: GroupStat['key']) => `${String(h).padStart(2, '0')}:00 UTC`
  return (
    <div className="space-y-4">
      <Panel title="Cumulative P&L" subtitle={`Net of fees and funding, ${data.trades} closed trades — one line per system`}>
        <CumulativeChart data={data} />
      </Panel>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <Panel title="Daily P&L" subtitle="Net result per UTC day"><DailyChart data={data} /></Panel>
        <Panel title="Result distribution" subtitle="Each trade's net result in R (1R = the loss at its stop)"><RDistribution data={data} /></Panel>
        <Panel title="By coin" subtitle="Net P&L · trades × win rate"><GroupBars rows={data.by_symbol} label={k => coin(String(k))} /></Panel>
        <Panel title="By exit" subtitle="How trades ended, and what each exit made"><GroupBars rows={data.by_exit} label={k => String(k).replace(/_/g, ' ')} wide /></Panel>
        <Panel title="Where the money went" subtitle="From the price move (gross) to what was kept (net)"><CostsWaterfall data={data} /></Panel>
        <Panel title="By hold time" subtitle="Net P&L by how long the trade was open"><GroupBars rows={data.by_hold} label={k => String(k)} limit={6} /></Panel>
        <Panel title="Is the edge holding?" subtitle="Rolling average result over trade order"><RollingChart data={data} /></Panel>
        <Panel title="Exit efficiency" subtitle="Best move reached vs result kept, in R"><EfficiencyChart data={data} /></Panel>
      </div>
      <Panel title="When trades are opened" subtitle="Net P&L by weekday and hour of entry (UTC)"><Heatmap data={data} /></Panel>
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Panel title="By hour" subtitle="Best and worst entry hours"><GroupBars rows={data.by_hour} label={hourLabel} limit={8} /></Panel>
        <Panel title="By weekday"><GroupBars rows={data.by_weekday} label={k => String(k)} limit={7} /></Panel>
        <Panel title="Long vs short"><GroupBars rows={data.by_side} label={k => String(k)} limit={2} /></Panel>
      </div>
    </div>
  )
}

// ── Research ───────────────────────────────────────────────────────────────────

function sqnGrade(sqn: number): string {
  if (sqn < 0) return 'losing'
  if (sqn < 1.6) return 'poor'
  if (sqn < 2) return 'below average'
  if (sqn < 2.5) return 'average'
  if (sqn < 3) return 'good'
  if (sqn < 5) return 'excellent'
  return 'superb'
}

function Metric({ label, value, sub, title }: { label: string; value: ReactNode; sub?: ReactNode; title?: string }) {
  return (
    <div className="bg-gray-800/50 rounded-lg px-3 py-2.5" title={title}>
      <div className="text-[10px] uppercase tracking-wider text-gray-500">{label}</div>
      <div className="text-lg font-bold tabular-nums leading-tight mt-0.5 text-white">{value}</div>
      {sub && <div className="text-[11px] text-gray-500 mt-0.5">{sub}</div>}
    </div>
  )
}

/** A value range on a scale that always includes zero, labelled where the values actually sit. */
function RangeBar({ lo, hi, mid, midLabel }: { lo: number; hi: number; mid?: number; midLabel: string }) {
  const min = Math.min(0, lo), max = Math.max(0, hi)
  const pad = (max - min) * 0.12 || 1
  const [a, b] = [min - pad, max + pad]
  const pct = (v: number) => ((v - a) / (b - a)) * 100
  const colour = hi < 0 ? LOSS : lo > 0 ? WIN : '#9ca3af'
  return (
    <div className="relative h-16 my-1">
      <div className="absolute inset-x-0 top-6 h-px bg-gray-700" />
      <div className="absolute top-3.5 bottom-6 w-px bg-gray-400" style={{ left: `${pct(0)}%` }} />
      <span className="absolute top-0 -translate-x-1/2 text-[10px] text-gray-400" style={{ left: `${pct(0)}%` }}>$0</span>
      <div className="absolute top-5 h-2 rounded-full" style={{ left: `${pct(lo)}%`, width: `${pct(hi) - pct(lo)}%`, background: colour }} />
      {mid != null && <div className="absolute top-4 h-4 w-1 -ml-0.5 rounded-sm bg-white" style={{ left: `${pct(mid)}%` }} />}
      <span className={clsx('absolute top-9 -translate-x-full pr-1 text-[11px] tabular-nums', tone(lo))} style={{ left: `${pct(lo)}%` }}>{fmtUsdSigned(lo)}</span>
      <span className={clsx('absolute top-9 pl-1 text-[11px] tabular-nums', tone(hi))} style={{ left: `${pct(hi)}%` }}>{fmtUsdSigned(hi)}</span>
      {mid != null && (
        <span className="absolute top-12 -translate-x-1/2 text-[11px] tabular-nums text-white whitespace-nowrap" style={{ left: `${pct(mid)}%` }}>
          {midLabel} {fmtUsdSigned(mid)}
        </span>
      )}
    </div>
  )
}

function findings(d: TradeAnalytics): string[] {
  const out: string[] = []
  const c = d.confidence
  const costs = d.costs
  if (c.enough && c.expectancy_ci) {
    const [lo, hi] = c.expectancy_ci
    out.push(hi < 0 ? `These trades lose money on average, and that's not noise: even the optimistic end of the 95% range is ${fmtUsdSigned(hi)} per trade.`
      : lo > 0 ? `These trades make money on average, and the whole 95% range (${fmtUsdSigned(lo)} to ${fmtUsdSigned(hi)} per trade) is above zero.`
        : `Not proven either way yet: the average trade could be anywhere from ${fmtUsdSigned(lo)} to ${fmtUsdSigned(hi)}.${c.trades_needed ? ` About ${c.trades_needed.toLocaleString()} trades would settle it at this effect size.` : ''}`)
  }
  const zeroFee = d.fee_scenarios.find(f => f.label === 'Zero fees')
  if (costs.gross_pnl > 0 && costs.net_pnl < 0) {
    out.push(`The calls were right on price (${fmtUsdSigned(costs.gross_pnl)} gross) but costs turned it into a loss — cheaper execution (maker orders, a fee tier) is the fix.`)
  } else if (zeroFee && zeroFee.net_pnl < 0 && costs.fees > Math.abs(costs.gross_pnl)) {
    out.push(`Two problems at once: even with zero fees these trades would lose ${fmtUsdSigned(zeroFee.net_pnl)} (no edge in the entries), and fees (${fmtUsd(costs.fees)}) more than double the damage.`)
  } else if (zeroFee && zeroFee.net_pnl < 0) {
    out.push(`Even with zero fees these trades would lose ${fmtUsdSigned(zeroFee.net_pnl)} — the entries have no edge; cheaper execution alone won't fix it.`)
  }
  const e = d.efficiency
  if (e.reached_1r >= 5 && e.reached_1r_then_lost / e.reached_1r >= 0.3) {
    out.push(`${e.reached_1r_then_lost} of ${e.reached_1r} trades that were +1R up still closed at a loss — exits give back too much; a breakeven or trailing rule would keep more.`)
  }
  const sides = Object.fromEntries(d.by_side.map(s => [s.key, s]))
  if (sides.long && sides.short && sides.long.trades >= 10 && sides.short.trades >= 10 &&
      Math.sign(sides.long.net_pnl) !== Math.sign(sides.short.net_pnl)) {
    const [good, bad] = sides.long.net_pnl > 0 ? ['long', 'short'] : ['short', 'long']
    out.push(`${good[0].toUpperCase() + good.slice(1)}s made ${fmtUsdSigned(sides[good].net_pnl)} while ${bad}s lost ${fmtUsdSigned(sides[bad].net_pnl)}.`)
  }
  const sym = d.by_symbol.filter(s => s.trades >= 5)
  if (sym.length >= 3) {
    const worst = sym[sym.length - 1]
    if (worst.net_pnl < 0) out.push(`${coin(String(worst.key))} is the biggest drain: ${fmtUsdSigned(worst.net_pnl)} over ${worst.trades} trades (${worst.win_rate}% won).`)
  }
  const hours = d.by_hour.filter(h => h.trades >= 5)
  if (hours.length >= 4) {
    const sorted = [...hours].sort((a, b) => a.net_pnl - b.net_pnl)
    out.push(`Worst entry hour ${String(sorted[0].key).padStart(2, '0')}:00 UTC (${fmtUsdSigned(sorted[0].net_pnl)}), best ${String(sorted[sorted.length - 1].key).padStart(2, '0')}:00 UTC (${fmtUsdSigned(sorted[sorted.length - 1].net_pnl)}).`)
  }
  if (d.streaks.longest_loss >= 8) out.push(`Longest losing run: ${d.streaks.longest_loss} trades in a row — size positions so a run like that is survivable.`)
  return out
}

export function TradeResearch({ data }: { data: TradeAnalytics }) {
  const c = data.confidence
  const mc = data.monte_carlo
  if (!c.enough) return <div className="card text-sm text-gray-500">At least 5 closed trades are needed for the research view.</div>
  const [lo, hi] = c.expectancy_ci ?? [0, 0]
  const notes = findings(data)
  return (
    <div className="space-y-4">
      <Panel title="What the numbers say" subtitle="Plain-language findings for the trades selected by the filters">
        {notes.length ? (
          <ul className="space-y-2 text-sm text-gray-300 list-disc pl-5">{notes.map(n => <li key={n}>{n}</li>)}</ul>
        ) : <p className="text-sm text-gray-500">Nothing stands out yet.</p>}
      </Panel>

      <Panel title="Is there an edge?" subtitle={`${c.trades} closed trades. The interval comes from resampling these trades 1,000 times.`}>
        <div className="text-xs text-gray-400">Average net result per trade — 95% range</div>
        <div className="mb-3"><RangeBar lo={lo} hi={hi} mid={c.expectancy_usd} midLabel="average" /></div>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
          <Metric label="Avg result" value={c.expectancy_r == null ? '—' : `${c.expectancy_r > 0 ? '+' : ''}${c.expectancy_r.toFixed(2)}R`}
                  sub={c.sd_r != null ? `spread ±${c.sd_r.toFixed(2)}R` : undefined} />
          <Metric label="t-stat" value={c.t_stat ?? '—'} sub={c.p_value != null ? `p = ${c.p_value < 0.0001 ? '<0.0001' : c.p_value}` : undefined}
                  title="How many standard errors the average is from zero. |t| above ~2 is unlikely to be luck." />
          <Metric label="Chance it's positive" value={`${c.positive_share ?? 0}%`} sub="share of resamples > $0" />
          <Metric label="SQN" value={c.sqn ?? '—'} sub={c.sqn != null ? sqnGrade(c.sqn) : undefined}
                  title="System Quality Number (Van Tharp): √N × average R ÷ spread of R, N capped at 100." />
          <Metric label="Trades to prove it" value={c.trades_needed ? c.trades_needed.toLocaleString() : '—'}
                  sub={c.trades_needed ? `have ${c.trades}` : (c.expectancy_r ?? 0) <= 0 ? 'no positive edge to prove' : undefined} />
          <Metric label="Kelly" value={c.kelly_pct == null ? '—' : `${c.kelly_pct}%`}
                  sub={(c.kelly_pct ?? 0) <= 0 ? 'no edge → don\'t size up' : 'of equity — use a quarter of it'}
                  title="Growth-optimal stake from win rate and payoff. Real edges are uncertain, so practitioners use ¼–½ Kelly." />
        </div>
      </Panel>

      {mc && (
        <Panel title="How bad could it get?"
               subtitle={`Monte Carlo: these ${mc.trades} trades resampled into ${mc.simulations.toLocaleString()} alternative histories of the same length`}>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div>
              <div className="text-xs text-gray-400">Final P&L — 5th to 95th percentile (white = median)</div>
              <RangeBar lo={mc.final.p5} hi={mc.final.p95} mid={mc.final.p50} midLabel="median" />
              <p className="text-xs text-gray-400 mt-3">
                Ends in a loss in <span className="text-white font-semibold">{mc.loss_probability_pct}%</span> of histories
                (actual: {fmtUsdSigned(mc.actual_final)}). This reshuffles the trades already taken — it shows the range of luck
                around this record, not a forecast for a changed strategy.
              </p>
            </div>
            <div className="grid grid-cols-3 gap-2 content-start">
              <Metric label="Typical max drawdown" value={fmtUsd(mc.max_drawdown.p50, 0)} sub="median history" />
              <Metric label="Bad case (1 in 10)" value={fmtUsd(mc.max_drawdown.p90, 0)} sub="90th percentile" />
              <Metric label="Worst case (1 in 100)" value={fmtUsd(mc.max_drawdown.p99, 0)} sub={`actual ${fmtUsd(mc.actual_max_drawdown, 0)}`} />
              {mc.drawdown_odds.map(o => (
                <Metric key={o.pct} label={`Drawdown ≥ ${o.pct}%`} value={`${o.odds_pct}%`} sub={`of ${fmtUsd(mc.initial_balance, 0)} start`} />
              ))}
            </div>
          </div>
        </Panel>
      )}

      <Panel title="What if fees were lower?"
             subtitle="The same trades re-costed at other fee rates (entry and exit). Funding and slippage stay as they were.">
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[28rem]">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-gray-500 border-b border-gray-800">
                <th className="text-left font-semibold py-2">Fee schedule</th>
                <th className="text-right font-semibold py-2">Fees</th>
                <th className="text-right font-semibold py-2">Net P&L</th>
                <th className="text-right font-semibold py-2">Win rate</th>
              </tr>
            </thead>
            <tbody>
              {data.fee_scenarios.map((f, i) => (
                <tr key={f.label} className={clsx('border-b border-gray-800/60 last:border-0', i === 0 && 'text-white font-semibold')}>
                  <td className="py-2 text-gray-300">{f.label}</td>
                  <td className="py-2 text-right tabular-nums text-gray-400">{fmtUsd(f.fees)}</td>
                  <td className={clsx('py-2 text-right tabular-nums', tone(f.net_pnl))}>{fmtUsdSigned(f.net_pnl)}</td>
                  <td className="py-2 text-right tabular-nums text-gray-300">{f.win_rate == null ? '—' : `${f.win_rate}%`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-[11px] text-gray-500 mt-2">
          If a row turns positive the calls were fine and the fee schedule is the problem: resting orders (maker) or a VIP tier
          fix it. If even "zero fees" is negative, the entries themselves have no edge.
        </p>
      </Panel>

      <Panel title="By system" subtitle="The same numbers, one row per system">
        <GroupBars rows={data.by_system} label={k => SYSTEM_LABEL[k as SystemKey] ?? String(k)} limit={8} wide />
      </Panel>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <Metric label="Current streak" value={(() => {
          const n = Math.abs(data.streaks.current)
          return data.streaks.current > 0 ? `${n} win${n === 1 ? '' : 's'}` : `${n} loss${n === 1 ? '' : 'es'}`
        })()} />
        <Metric label="Longest win streak" value={data.streaks.longest_win} />
        <Metric label="Longest loss streak" value={data.streaks.longest_loss} />
        <Metric label="Won before costs" value={data.costs.won_before_costs} sub="right on price, lost to fees" />
      </div>
    </div>
  )
}
