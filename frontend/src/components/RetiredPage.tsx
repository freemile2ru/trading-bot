import { useEffect, useState } from 'react'
import { Area, AreaChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Archive, FlaskConical } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsdSigned } from '../utils/format'
import { evenTicks } from '../utils/chart'
import { SortHeader, useSort } from './ui/Sortable'
import type { FailureScore, RejectedResearch, RetiredExperiment, ScalperEvidence, ScoreInputs } from '../types/retired'

const AXIS = { fill: '#6b7280', fontSize: 10 }
const TIP = { background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }
const DEFAULT_ROUND_TRIP_BPS = 7  // the order-flow scalper's: maker entry + taker exit (0.02% + 0.05%)

// Score parts — categorical slots 1–2, in fixed order.
const PARTS: { key: 'performance' | 'certainty'; label: string; max: number; color: string; help: string }[] = [
  { key: 'performance', label: 'Performance', max: 60, color: '#3987e5', help: 'average result per trade after costs: 0R or better → 0, −0.5R or worse → 60' },
  { key: 'certainty', label: 'Certainty', max: 40, color: '#d95926', help: 'how conclusive: |t| ≥ 5 → 40; without a t-stat, sample size at a discount' },
]
const GRADE_TONE: Record<FailureScore['grade'], string> = {
  severe: 'bg-red-900/60 text-red-200', 'clear failure': 'bg-red-900/30 text-red-300',
  mild: 'bg-amber-900/30 text-amber-300', harmless: 'bg-gray-800 text-gray-300',
}

function inputsText(i: ScoreInputs): string {
  const parts = [
    i.result_r == null ? 'result not measurable' : `${i.result_r > 0 ? '+' : ''}${i.result_r.toFixed(2)}R per trade`,
    i.t != null ? `t ${i.t.toFixed(1)}` : null,
    i.n != null ? `${i.n.toLocaleString()} ${i.n_note ?? ''}`.trim() : null,
  ]
  return parts.filter(Boolean).join(' · ')
}

function ScoreBar({ s }: { s: FailureScore }) {
  return (
    <div className="flex h-3 w-full rounded-full bg-gray-800 overflow-hidden gap-[2px]" role="img"
         aria-label={`Failure score ${s.score} of 100: performance ${s.performance}, certainty ${s.certainty}`}>
      {PARTS.map(p => s[p.key] > 0 && (
        <div key={p.key} style={{ width: `${s[p.key]}%`, background: p.color }} title={`${p.label}: ${s[p.key]} of ${p.max}`} />
      ))}
    </div>
  )
}

function Ranking({ items, jump }: { items: RetiredExperiment[]; jump: (key: string) => void }) {
  const t = useSort(items, {
    rank: e => e.rank, experiment: e => e.title, score: e => e.score.score, grade: e => e.score.grade, result: e => e.score_inputs.result_r,
  })
  const th = (k: string, label: string, cls: string) => (
    <th className={clsx('font-semibold pb-2', cls)}><SortHeader k={k} sort={t.sort} onSort={t.toggle} align={cls.includes('right') ? 'right' : 'left'}>{label}</SortHeader></th>
  )
  return (
    <section className="card space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-white">How badly each one failed</h3>
        <p className="text-[11px] text-gray-500 mt-0.5">
          Failure score 0–100, higher is worse: how much each lost per trade after costs, plus how conclusive that result is —
          so a strategy that lost conclusively ranks above an idea that simply did nothing. Money lost isn't scored (every book
          traded paper or the Binance testnet). Where a study reported a range, the least-bad end is used.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs min-w-[44rem]">
          <thead>
            <tr className="text-[10px] uppercase tracking-wider text-gray-500">
              {th('rank', '#', 'text-left w-8')}
              {th('experiment', 'Experiment', 'text-left')}
              {th('score', 'Score', 'text-left w-[28%]')}
              <th className="text-right font-semibold pb-2 w-12" />
              {th('result', 'Based on', 'text-left pl-3')}
            </tr>
          </thead>
          <tbody>
            {t.rows.map(e => (
              <tr key={e.key} className="border-t border-gray-800/60 align-middle">
                <td className="py-2 text-gray-500 tabular-nums">{e.rank}</td>
                <td className="py-2 pr-3">
                  <button onClick={() => jump(e.key)} className="text-left font-semibold text-gray-100 hover:text-white">{e.title}</button>
                  <div className="text-[10px] font-semibold text-green-400">{e.category}</div>
                </td>
                <td className="py-2"><ScoreBar s={e.score} /></td>
                <td className="py-2 text-right">
                  <span className="text-sm font-bold text-white tabular-nums">{e.score.score}</span>
                </td>
                <td className="py-2 pl-3">
                  <span className={clsx('px-1.5 py-0.5 rounded text-[10px] font-semibold whitespace-nowrap', GRADE_TONE[e.score.grade])}>{e.score.grade}</span>
                  <span className="ml-2 text-gray-400 underline decoration-dotted decoration-gray-700 underline-offset-2 cursor-help"
                        title={[e.score_inputs.result_note, e.score_inputs.t_note].filter(Boolean).join(' · ') || undefined}>
                    {inputsText(e.score_inputs)}
                  </span>
                  {e.score.partial && <span className="ml-1 text-gray-500">(partial: no result to score)</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-gray-400">
        {PARTS.map(p => (
          <span key={p.key} className="inline-flex items-center gap-1.5" title={p.help}>
            <span className="w-2.5 h-2.5 rounded-sm" style={{ background: p.color }} />{p.label} (up to {p.max}) — {p.help}
          </span>
        ))}
      </div>
    </section>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500 mb-1">{title}</div>
      {children}
    </div>
  )
}

function ScalperCharts({ d }: { d: ScalperEvidence }) {
  const entered = d.signal_forward.filter(h => h.set === 'entered')
  const all = new Map(d.signal_forward.filter(h => h.set === 'all candidates').map(h => [h.horizon_s, h]))
  const roundTrip = d.round_trip_bps ?? DEFAULT_ROUND_TRIP_BPS
  const maxBps = Math.max(roundTrip, ...d.signal_forward.map(h => h.avg_bps)) * 1.1
  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div className="bg-gray-800/30 rounded-lg p-3">
        <div className="text-xs font-semibold text-gray-200">Paper P&L, trade by trade</div>
        <ResponsiveContainer width="100%" height={170}>
          <AreaChart data={d.curve} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']} ticks={evenTicks(d.curve.map(p => p.ts), 3)}
                   tickFormatter={ts => new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
                   tick={AXIS} tickLine={false} axisLine={false} />
            <YAxis width={56} tick={AXIS} tickLine={false} axisLine={false} tickFormatter={v => fmtUsdSigned(v, 0)} />
            <ReferenceLine y={0} stroke="#4b5563" />
            <Tooltip contentStyle={TIP} labelFormatter={ts => new Date(Number(ts) * 1000).toLocaleString()}
                     formatter={(v: number) => [fmtUsdSigned(v), 'Cumulative net']} />
            <Area dataKey="pnl" stroke="#ef4444" fill="#7f1d1d" fillOpacity={0.35} strokeWidth={1.5} dot={false} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <div className="bg-gray-800/30 rounded-lg p-3">
        <div className="text-xs font-semibold text-gray-200">The signal: price move after it, vs the cost to trade</div>
        <p className="text-[10px] text-gray-500 mb-2">Mid-price move in the signal's direction. The bar would need to pass the dashed cost line to pay.</p>
        <div className="space-y-1.5">
          {entered.map(h => {
            const a = all.get(h.horizon_s)
            return (
              <div key={h.horizon_s} className="grid grid-cols-[2.5rem_minmax(0,1fr)_5.5rem] items-center gap-2 text-[11px]"
                   title={`${h.n.toLocaleString()} traded signals (t = ${h.t}); all candidates ${a ? `${a.avg_bps} bps on ${a.n.toLocaleString()}` : '—'}`}>
                <span className="text-gray-400">+{h.horizon_s < 60 ? `${h.horizon_s}s` : `${h.horizon_s / 60}m`}</span>
                <div className="relative h-4 bg-gray-900/60 rounded">
                  <div className="absolute inset-y-0.5 left-0 rounded-sm bg-emerald-600" style={{ width: `${Math.max(0, h.avg_bps) / maxBps * 100}%` }} />
                  <div className="absolute -top-0.5 -bottom-0.5 border-l border-dashed border-red-400" style={{ left: `${roundTrip / maxBps * 100}%` }} />
                </div>
                <span className="tabular-nums text-gray-200 text-right">{h.avg_bps > 0 ? '+' : ''}{h.avg_bps} bps</span>
              </div>
            )
          })}
        </div>
        <p className="text-[10px] text-gray-500 mt-2">
          Red dashed line: ~{roundTrip} bps round-trip cost. {d.signals_logged.toLocaleString()} signals logged. Hover a row for its t-stat.
        </p>
      </div>

      <div className="bg-gray-800/30 rounded-lg p-3">
        <div className="text-xs font-semibold text-gray-200 mb-2">How the paper trades ended</div>
        <table className="w-full text-[11px]">
          <tbody>
            {d.by_exit.map(e => (
              <tr key={e.exit} className="border-t border-gray-800/60">
                <td className="py-1 text-gray-300 capitalize">{e.exit.replace(/_/g, ' ')}</td>
                <td className="py-1 text-right tabular-nums text-gray-400">{e.trades}</td>
                <td className="py-1 text-right tabular-nums text-gray-400">{e.win_rate}%</td>
                <td className={clsx('py-1 text-right tabular-nums', e.net >= 0 ? 'text-emerald-400' : 'text-red-400')}>{fmtUsdSigned(e.net, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Card({ e }: { e: RetiredExperiment }) {
  return (
    <section id={`retired-${e.key}`} className="card space-y-4 scroll-mt-4">
      <div className="flex flex-wrap items-start gap-2">
        <div>
          <h3 className="text-base font-bold text-white">{e.title}</h3>
          <div className="text-[11px] text-gray-500 mt-0.5"><span className="font-semibold text-green-400">{e.category}</span> · {e.active}</div>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <span className={clsx('px-2 py-0.5 rounded text-[11px] font-semibold', GRADE_TONE[e.score.grade])}
                title={`Performance ${e.score.performance} · certainty ${e.score.certainty}`}>
            #{e.rank} · failure score {e.score.score}/100 · {e.score.grade}
          </span>
          <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-red-900/40 text-red-300">Retired {e.retired}</span>
        </div>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 text-sm">
        <Section title="What we believed"><p className="text-gray-300 leading-relaxed">{e.hypothesis}</p></Section>
        <Section title="How we tested it"><p className="text-gray-400 leading-relaxed">{e.method}</p></Section>
      </div>
      {!!e.numbers.length && (
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2">
          {e.numbers.map(n => (
            <div key={n.label} className="bg-gray-800/50 rounded-lg px-3 py-2">
              <div className="text-[10px] uppercase tracking-wider text-gray-500">{n.label}</div>
              <div className="text-sm font-bold text-white tabular-nums mt-0.5">{n.value}</div>
              {n.note && <div className="text-[11px] text-gray-500 mt-0.5">{n.note}</div>}
            </div>
          ))}
        </div>
      )}
      {e.data && <ScalperCharts d={e.data} />}
      <Section title="Why it failed">
        <ul className="list-disc pl-5 space-y-1.5 text-sm text-gray-300">{e.why.map(w => <li key={w}>{w}</li>)}</ul>
      </Section>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 text-xs">
        <Section title="What we kept"><p className="text-gray-400 leading-relaxed">{e.kept}</p></Section>
        <Section title="What was removed"><p className="text-gray-400 leading-relaxed">{e.removed}</p></Section>
        <Section title="Evidence"><p className="text-gray-400 font-mono leading-relaxed">{e.evidence}</p></Section>
      </div>
    </section>
  )
}

/** Studies the Lab's research shelf rejected: the question, what the data said, and what was decided. */
function RejectedStudies({ items }: { items: RejectedResearch[] }) {
  const t = useSort(items, { study: r => r.title, ran: r => r.ran_at, book: r => r.book })
  if (!items.length) return null
  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-sm font-semibold text-white inline-flex items-center gap-2"><FlaskConical className="w-4 h-4 text-gray-400" /> Rejected research</h3>
        <span className="text-[11px] text-gray-500">{items.length} studies the Lab ran and set aside — the finding is read from each one's results file; rerun the script to reproduce</span>
        <span className="ml-auto inline-flex gap-3 text-[10px] uppercase tracking-wider text-gray-500">
          sort <SortHeader k="study" sort={t.sort} onSort={t.toggle}>name</SortHeader>
          <SortHeader k="ran" sort={t.sort} onSort={t.toggle}>date</SortHeader>
          <SortHeader k="book" sort={t.sort} onSort={t.toggle}>book</SortHeader>
        </span>
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        {t.rows.map(r => (
          <section key={r.key} className="rounded-lg bg-gray-800/40 px-3 py-2.5 space-y-1.5 text-xs">
            <div className="flex items-start gap-2">
              <span className="shrink-0 rounded border border-gray-700 bg-gray-800 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-gray-400">rejected</span>
              <div className="min-w-0">
                <div className="font-semibold text-gray-100">{r.title}</div>
                <div className="text-[10px] text-gray-500 font-mono">{r.script}{r.ran_at ? ` · ${new Date(r.ran_at * 1000).toLocaleDateString()}` : ''}{r.book ? ` · ${r.book}` : ''}</div>
              </div>
            </div>
            <div className="text-gray-400"><span className="text-gray-500">Question — </span>{r.question}</div>
            <div className="text-gray-300"><span className="text-gray-500">Found — </span>{r.finding}</div>
            <div className="text-gray-300"><span className="text-gray-500">Done — </span>{r.decision}</div>
          </section>
        ))}
      </div>
    </section>
  )
}

export function RetiredPage() {
  const [items, setItems] = useState<RetiredExperiment[] | null>(null)
  const [research, setResearch] = useState<RejectedResearch[]>([])
  const [error, setError] = useState<string | null>(null)
  // The chosen type is remembered in the URL (#retired/Strategy) so a filtered view can be bookmarked.
  const [category, setCategoryState] = useState<string>(() => decodeURIComponent(window.location.hash.split('/')[1] ?? ''))
  const setCategory = (c: string) => {
    setCategoryState(c)
    window.history.replaceState(null, '', c ? `#retired/${encodeURIComponent(c)}` : '#retired')
  }
  useEffect(() => {
    api.lab.retired().then(d => { setItems(d.retired); setResearch(d.research ?? []) }).catch(e => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [])
  if (error) return <div className="card text-sm text-red-400">{error}</div>
  if (!items) return <div className="card text-sm text-gray-500 animate-pulse">Loading the retired experiments…</div>

  const counts = items.reduce<Record<string, number>>((acc, e) => ({ ...acc, [e.category]: (acc[e.category] ?? 0) + 1 }), {})
  const categories = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b))
  const active = categories.includes(category) ? category : ''
  const shown = active ? items.filter(e => e.category === active) : items
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Archive className="w-5 h-5 text-gray-400" />
        <h2 className="text-base font-bold text-white">Retired experiments</h2>
        <span className="text-xs text-gray-500">what we tried, how we tested it, and why it didn't work — so it isn't tried again blind</span>
      </div>
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by experiment type">
        <span className="text-xs text-gray-500">Type</span>
        {[['', `All · ${items.length}`] as const, ...categories.map(c => [c, `${c} · ${counts[c]}`] as const)].map(([c, label]) => (
          <button key={c || 'all'} onClick={() => setCategory(c)} aria-pressed={active === c}
            className={clsx('px-2.5 py-1 rounded-md text-xs font-semibold border transition-colors',
              active === c
                ? 'bg-green-500/15 border-green-500/60 text-green-300'
                : 'bg-gray-800/70 border-transparent text-gray-400 hover:text-gray-200')}>
            {label}
          </button>
        ))}
      </div>
      <Ranking items={shown} jump={key => document.getElementById(`retired-${key}`)?.scrollIntoView({ behavior: 'smooth' })} />
      {!active && <RejectedStudies items={research} />}
      {shown.map(e => <Card key={e.key} e={e} />)}
    </div>
  )
}
