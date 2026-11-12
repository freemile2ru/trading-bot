import { useEffect, useState } from 'react'
import { FlaskConical, RefreshCw } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import type { ControlAgg, ControlReport, ControlRow, IndexShadowReport, ShadowStat } from '../types/control'

const LABELS: Record<string, string> = {
  cascade: 'Cascade book', 'auto:cascade': 'Autotrader · cascade lane', 'auto:reversal': 'Autotrader · reversal lane',
  burst_up: 'Burst · up', burst_dip: 'Burst · dip', prop: 'Prop book', auction: 'Auction book', weekly_rs: 'Weekly near-high book', 'auto:weekly': 'Autotrader · weekly lane',
}

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

const pct = (n: number | null | undefined, d = 2) => n == null ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(d)}%`
const tone = (n: number | null | undefined) => n == null ? 'text-gray-500' : n > 0 ? 'text-emerald-300' : n < 0 ? 'text-red-300' : 'text-gray-400'

function AggRow({ label, a, horizons, after, note }: { label: string; a: ControlAgg; horizons: string[]; after: string[]; note?: string }) {
  if (!a.n) return <tr className="border-t border-gray-800/60"><td className="py-1.5 text-gray-300">{label}</td><td colSpan={horizons.length + after.length + 3} className="text-xs text-gray-600">nothing yet</td></tr>
  return (
    <tr className="border-t border-gray-800/60" title={note}>
      <td className="py-1.5 text-gray-300">{label}<span className="text-gray-600"> · {a.n}</span></td>
      <td className={clsx('text-right font-semibold', tone(a.actual))}>{fmtUsdSigned(a.actual, 0)}</td>
      {horizons.map(h => <td key={h} className={clsx('text-right', tone(a.held?.[h]), a.held?.[h] != null && a.actual != null && a.held[h] > a.actual && 'underline decoration-dotted')}>{fmtUsdSigned(a.held?.[h], 0)}</td>)}
      {after.map(h => <td key={h} className={clsx('text-right', tone(a.after_exit_avg_pct?.[h]))}>{pct(a.after_exit_avg_pct?.[h])}</td>)}
      <td className="text-right text-gray-400">{a.avg_hold_h?.toFixed(1)}h</td>
      <td className="text-right text-gray-500">{a.stopped_share_pct?.toFixed(0)}%</td>
    </tr>
  )
}

const SIGNAL_LABEL: Record<string, string> = { reversal: 'Reversal score', cascade: 'Cascade fade' }

function Stat({ s }: { s: ShadowStat | undefined }) {
  if (!s?.n) return <td className="text-right text-gray-600">—</td>
  return <td className={clsx('text-right', tone(s.mean_pct))} title={`t ${s.t ?? '—'} · ${s.pos_pct}% of episodes positive`}>{pct(s.mean_pct)}</td>
}

/** The reversal and cascade signals on the same windows three ways — the picked coin, the alt index, BTC+ETH — by episode. */
function IndexShadow() {
  const [reps, setReps] = useState<IndexShadowReport[]>([])
  useEffect(() => {
    let live = true
    Promise.all(['reversal', 'cascade'].map(s => api.indexShadow.report(s).catch(() => null)))
      .then(xs => { if (live) setReps(xs.filter((x): x is IndexShadowReport => !!x)) })
    return () => { live = false }
  }, [])
  if (!reps.length) return null
  const since = new Date(reps[0].since * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  return (
    <section className="card">
      <h3 className="text-sm font-semibold text-white">Index shadow · coin or market?</h3>
      <p className="mt-1 max-w-3xl text-xs text-gray-400">
        Each closed reversal and cascade trade, scored on its own window three ways, net of a {reps[0].fee_rt_pct}% round trip: the coin we picked, an
        equal-weight index of {reps[0].index.length} large alts, and BTC+ETH. Entries within two hours are one episode. The baseline holds the index for the
        signal's median hold from every hour of the same period. Verdict on episodes from {since} at {reps[0].verdict_episodes}: the index must beat the coin
        by {reps[0].min_gap_pct}% an episode with t ≥ 2 and both halves agreeing. Nothing here trades.
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[760px] text-xs">
          <thead className="text-left text-[10px] uppercase tracking-wider text-gray-500">
            <tr><th className="pb-1">Signal</th><th className="text-right">Episodes</th><th className="text-right">What we made</th><th className="text-right">Coin</th>
              <th className="text-right">Alt index</th><th className="text-right">BTC+ETH</th><th className="text-right">Random baseline</th><th className="text-right">Forward</th><th className="text-right">Verdict</th></tr>
          </thead>
          <tbody>
            {reps.flatMap(r => (['short', 'long'] as const).filter(sd => r.sides[sd]).map(sd => {
              const v = r.sides[sd]!
              return (
                <tr key={r.signal + sd} className="border-t border-gray-800/60">
                  <td className="py-1.5 text-gray-300">{SIGNAL_LABEL[r.signal] ?? r.signal} · {sd}s<span className="text-gray-600"> · {v.trades} trades</span></td>
                  <td className="text-right text-gray-400">{v.all.episodes}</td>
                  <Stat s={v.all.actual_pct} /><Stat s={v.all.coin_pct} /><Stat s={v.all.index_pct} /><Stat s={v.all.majors_pct} />
                  <td className={clsx('text-right', tone(v.baseline?.mean_pct))}>{v.baseline ? `${pct(v.baseline.mean_pct)} · ${v.baseline.hold_h}h` : '—'}</td>
                  <td className="text-right text-gray-400">{v.forward.episodes}/{r.verdict_episodes}</td>
                  <td className={clsx('text-right', v.verdict.verdict === 'index better' ? 'text-emerald-300' : v.verdict.verdict === 'measuring' ? 'text-gray-500' : 'text-gray-300')}>{v.verdict.verdict}</td>
                </tr>
              )
            }))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export function ControlPage() {
  const [book, setBook] = useState<string>(() => { try { return localStorage.getItem('control.book') || 'cascade' } catch { return 'cascade' } })
  const [d, setD] = useState<ControlReport | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    setD(null)
    api.control.report(book).then(x => { if (live) { setD(x); setErr(null) } }).catch(e => { if (live) setErr(String(e?.message ?? e)) })
    try { localStorage.setItem('control.book', book) } catch { /* per-viewer convenience only */ }
    return () => { live = false }
  }, [book])

  const run = async () => { setBusy(true); try { setD((await api.control.run(book)).report) } finally { setBusy(false) } }
  const books = d?.books ?? Object.keys(LABELS)

  return (
    <div className="space-y-4">
      <section className="card">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 text-base font-semibold text-white"><FlaskConical className="h-4 w-4 text-violet-300" /> Control data</h2>
            <p className="mt-1 max-w-3xl text-xs text-gray-400">
              Every closed trade of a book, replayed on its 5-minute bars: what the same position would have made held for 1 to 48 hours with only its
              original stop (a bar's wick hits the stop before anything favourable counts), and how far the price went on in its direction after we left.
              "Early" rows left before the book's own horizon. Dotted figures beat what we did. Upper bounds: the controls pay one exit fee and no slippage.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <select value={book} onChange={e => setBook(e.target.value)} className="rounded-lg border border-gray-700 bg-gray-900 px-2 py-1.5 text-xs text-gray-200">
              {books.map(b => <option key={b} value={b}>{LABELS[b] ?? b}</option>)}
            </select>
            <button onClick={run} disabled={busy} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 disabled:opacity-50" title="Replay the closed trades that have no control yet">
              <RefreshCw className={clsx('h-3.5 w-3.5', busy && 'animate-spin')} /> Backfill{d?.pending ? ` (${d.pending} waiting)` : ''}
            </button>
          </div>
        </div>
        {err && <p className="mt-3 text-xs text-red-300">{err}</p>}
        {!d && !err && <p className="mt-3 text-xs text-gray-500">Loading…</p>}
        {d && (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-[10px] uppercase tracking-wider text-gray-500">
                <tr>
                  <th className="pb-1">Trades</th><th className="text-right">Actual</th>
                  {d.horizons.map(h => <th key={h} className="text-right" title="held with the original stop">held {h}</th>)}
                  {d.after_exit.map(h => <th key={h} className="text-right" title="average move in our direction after the exit">after {h}</th>)}
                  <th className="text-right">hold</th><th className="text-right" title="share of trades whose stop the path reached within 48h">stopped</th>
                </tr>
              </thead>
              <tbody>
                <AggRow label="All" a={d.all} horizons={d.horizons} after={d.after_exit} />
                <AggRow label={`Early (left before ${d.horizon_h}h)`} a={d.early} horizons={d.horizons} after={d.after_exit} />
                {Object.entries(d.by_reason).map(([k, v]) => <AggRow key={k} label={`exit: ${k}`} a={v} horizons={d.horizons} after={d.after_exit} />)}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {d && d.rows.length > 0 && (
        <section className="card">
          <h3 className="text-sm font-semibold text-white">Trades <span className="text-gray-500">· {d.rows.length}</span></h3>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-[10px] uppercase tracking-wider text-gray-500">
                <tr><th className="pb-1">Opened</th><th>Trade</th><th>Exit</th><th className="text-right">Hold</th><th className="text-right">Actual</th>
                  {d.horizons.map(h => <th key={h} className="text-right">{h}</th>)}
                  <th className="text-right">best 4h</th><th className="text-right">worst 4h</th>
                  {d.after_exit.map(h => <th key={h} className="text-right">after {h}</th>)}
                  <th className="text-right">stop hit</th></tr>
              </thead>
              <tbody>
                {d.rows.map((r: ControlRow) => (
                  <tr key={r.trade_id} className="border-t border-gray-800/60">
                    <td className="py-1 text-gray-500">{when(r.opened_ts)}</td>
                    <td><span className={clsx('mr-1 rounded px-1 text-[10px] uppercase', r.direction === 'long' ? 'bg-emerald-900/50 text-emerald-200' : 'bg-red-900/50 text-red-200')}>{r.direction}</span>{r.symbol.replace('USDT', '')}<span className="text-gray-600"> {fmtUsd(r.notional, 0)}</span></td>
                    <td className="text-gray-400">{r.hold_rule ?? r.close_reason}</td>
                    <td className={clsx('text-right', r.hold_h < d.horizon_h * 0.9 ? 'text-amber-300' : 'text-gray-400')}>{r.hold_h.toFixed(1)}h</td>
                    <td className={clsx('text-right font-semibold', tone(r.actual))}>{fmtUsdSigned(r.actual, 0)}</td>
                    {d.horizons.map(h => <td key={h} className={clsx('text-right', tone(r.held[h]), r.held[h] != null && r.held[h]! > r.actual && 'underline decoration-dotted')}>{r.held[h] == null ? '—' : fmtUsdSigned(r.held[h], 0)}</td>)}
                    <td className="text-right text-emerald-300/80">{pct(r.best_pct['4h'])}</td>
                    <td className="text-right text-red-300/80">{pct(r.worst_pct['4h'])}</td>
                    {d.after_exit.map(h => <td key={h} className={clsx('text-right', tone(r.after_exit_pct[h]))}>{pct(r.after_exit_pct[h])}</td>)}
                    <td className="text-right text-gray-500">{r.stopped_at_h == null ? '—' : `${r.stopped_at_h}h`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <IndexShadow />
    </div>
  )
}
