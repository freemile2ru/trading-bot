import { useEffect, useState } from 'react'
import { Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import clsx from 'clsx'
import { api } from '../../utils/api'
import type { BacktestStats, StrategyFamily, StrategyLabResults } from '../../types/lab'

const STRATEGY = '#3987e5'   // categorical slot 1
const BTC = '#d95926'        // slot 2
const AXIS = { fill: '#6b7280', fontSize: 10 }
const TIP = { background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }
const pct = (v: number | undefined) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`)
const num = (v: number | null | undefined, d = 2) => (v == null ? '—' : v.toFixed(d))
const tone = (v: number | null | undefined) => (v == null || v === 0 ? 'text-gray-200' : v > 0 ? 'text-emerald-400' : 'text-red-400')

function Badge({ f }: { f: StrategyFamily }) {
  const cls = f.status === 'promising' ? 'bg-emerald-900/50 text-emerald-300' : f.status === 'fails' ? 'bg-red-900/40 text-red-300' : 'bg-gray-800 text-gray-200'
  return <span className={clsx('px-2 py-0.5 rounded text-[11px] font-semibold whitespace-nowrap', cls)}>{f.verdict}</span>
}

function StatsRow({ label, s }: { label: string; s: BacktestStats }) {
  return (
    <tr className="border-t border-gray-800/60">
      <td className="py-1 text-gray-400">{label}</td>
      <td className={clsx('py-1 text-right tabular-nums', tone(s.total_return_pct))}>{pct(s.total_return_pct)}</td>
      <td className="py-1 text-right tabular-nums text-gray-200">{num(s.sharpe)}</td>
      <td className="py-1 text-right tabular-nums text-red-300">{pct(s.max_drawdown_pct)}</td>
      <td className="py-1 text-right tabular-nums text-gray-300">{num(s.t_stat)}</td>
    </tr>
  )
}

function FamilyCard({ f, btc }: { f: StrategyFamily; btc: { day: string; equity: number }[] }) {
  const btcBy = new Map(btc.map(p => [p.day, p.equity]))
  const data = f.curve.map(p => ({ day: p.day, strategy: (p.equity - 1) * 100, btc: btcBy.has(p.day) ? ((btcBy.get(p.day) as number) - 1) * 100 : undefined }))
  const ci = f.test.mean_daily_ci_bps
  return (
    <section className="card space-y-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0">
          <h4 className="text-sm font-semibold text-white">{f.title}</h4>
          <p className="text-[11px] text-gray-500 mt-0.5">Chosen on the first half: {f.chosen}</p>
        </div>
        <div className="ml-auto"><Badge f={f} /></div>
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-gray-500">
            <th className="text-left font-semibold pb-1" /><th className="text-right font-semibold pb-1">Return</th>
            <th className="text-right font-semibold pb-1">Sharpe</th><th className="text-right font-semibold pb-1">Max DD</th>
            <th className="text-right font-semibold pb-1">t</th>
          </tr>
        </thead>
        <tbody>
          <StatsRow label="Training half (tuned here)" s={f.train} />
          <StatsRow label="Test half (unseen)" s={f.test} />
        </tbody>
      </table>
      <p className="text-[11px] text-gray-400">
        Test average {num(f.test.mean_daily_bps, 1)} bps/day, 95% range {ci ? `${ci[0]} to ${ci[1]}` : '—'} bps.{' '}
        Across all {f.grid_size} settings, {f.grid_test_positive_pct}% made money on the test half (median Sharpe {num(f.grid_test_median_sharpe)})
        {f.grid_test_positive_pct != null && f.grid_test_positive_pct < 60 ? ' — the chosen setting looks like luck.' : '.'}
      </p>
      <ResponsiveContainer width="100%" height={150}>
        <LineChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <XAxis dataKey="day" tick={AXIS} tickLine={false} axisLine={false} minTickGap={40}
                 tickFormatter={d => new Date(`${d}T00:00:00Z`).toLocaleDateString(undefined, { month: 'short', year: '2-digit' })} />
          <YAxis width={48} tick={AXIS} tickLine={false} axisLine={false} tickFormatter={v => `${v > 0 ? '+' : ''}${Math.round(v)}%`} />
          <ReferenceLine y={0} stroke="#4b5563" />
          <Tooltip contentStyle={TIP} formatter={(v: number, k: string) => [`${v > 0 ? '+' : ''}${v.toFixed(1)}%`, k === 'strategy' ? 'Strategy' : 'BTC buy & hold']} />
          <Line dataKey="strategy" stroke={STRATEGY} strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line dataKey="btc" stroke={BTC} strokeWidth={1.5} strokeDasharray="5 3" dot={false} connectNulls isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <div className="flex gap-4 text-[11px] text-gray-400">
        <span className="inline-flex items-center gap-1.5"><span className="w-3 h-0.5" style={{ background: STRATEGY }} />Strategy (test half)</span>
        <span className="inline-flex items-center gap-1.5"><span className="w-3 border-t border-dashed" style={{ borderColor: BTC }} />BTC buy & hold</span>
      </div>
    </section>
  )
}

/** Lab section: strategy families backtested walk-forward (research/strategy_lab.py). */
export function StrategyCandidates() {
  const [r, setR] = useState<StrategyLabResults | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { api.lab.strategyLab().then(setR).catch(e => setErr(e instanceof Error ? e.message : 'Not available')) }, [])
  if (err) return <div className="card text-xs text-gray-500">Strategy lab: {err}</div>
  if (!r) return <div className="card h-40 animate-pulse" />
  const b = r.benchmark_btc_test
  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-400">
        {r.dataset}, {r.coins} perpetuals, {r.period[0]} → {r.period[1]}. Tuned on the first half, judged on the second (from {r.split});
        costs {r.cost_per_side_pct.toFixed(2)}% per side plus real funding. Passes only if the test half's 95% range of daily returns is above zero.
        BTC buy & hold over the test half: {pct(b.total_return_pct)} (Sharpe {num(b.sharpe)}). The coins are today's most liquid, which flatters
        long-biased strategies. Rerun: <span className="font-mono">python -m research.strategy_lab --years 4</span>
      </p>
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        {r.families.map(f => <FamilyCard key={f.key} f={f} btc={r.benchmark_btc_curve} />)}
      </div>
    </div>
  )
}
