import { useEffect, useState } from 'react'
import { Layers, RefreshCw, RotateCcw } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import type { CompositeStatus } from '../types/composite'

const PHASE: Record<string, { label: string; cls: string; note: string }> = {
  funding: { label: 'Funding', cls: 'border-gray-700 bg-gray-800 text-gray-300', note: 'waiting for the capital plan to fill the wallet to $50k' },
  running: { label: 'Running', cls: 'border-blue-800/60 bg-blue-900/40 text-blue-200', note: 'every short the sources open, one risk each, under the floors' },
  stopped: { label: 'Stopped', cls: 'border-red-800/60 bg-red-900/40 text-red-200', note: 'the 5% drawdown floor was hit — the book is flat until it is reset' },
}
const VERDICT: Record<string, string> = {
  collecting: 'border-gray-700 bg-gray-800 text-gray-300', promote: 'border-emerald-800/60 bg-emerald-900/40 text-emerald-200',
  retire: 'border-red-800/60 bg-red-900/40 text-red-200', stopped: 'border-red-800/60 bg-red-900/40 text-red-200',
}

function when(ts: number | string | null | undefined): string {
  if (!ts) return '—'
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function LimitBar({ label, usedPct, haltPct, flattenPct, room, action }: { label: string; usedPct: number; haltPct: number; flattenPct: number; room: number; action: string }) {
  const used = Math.max(0, Math.min(100, usedPct))
  const halt = haltPct / flattenPct * 100
  const tone = used >= 100 ? 'bg-red-500' : used >= halt ? 'bg-amber-400' : 'bg-emerald-500'
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-gray-400">{label}</span>
        <span className="text-gray-300">{used.toFixed(0)}% of {flattenPct}% · {fmtUsd(room, 0)} of room</span>
      </div>
      <div className="relative mt-1 h-2 rounded bg-gray-800">
        <div className={clsx('h-2 rounded', tone)} style={{ width: `${used}%` }} />
        <div className="absolute top-0 h-2 border-l border-amber-300/70" style={{ left: `${halt}%` }} title={`no new legs at ${haltPct}%`} />
      </div>
      <div className="mt-0.5 flex justify-between text-[10px] text-gray-600">
        <span>halt {haltPct}% · flat {flattenPct}%</span>
        <span className={clsx(action === 'ok' ? 'text-gray-600' : action === 'halt' ? 'text-amber-300' : 'text-red-300')}>{action === 'ok' ? '' : action}</span>
      </div>
    </div>
  )
}

/** The daily P&L as a row of bars against the target band — the one picture the book is judged on. */
function DailyBars({ days, low, high }: { days: { day: string; pnl: number }[]; low: number; high: number }) {
  if (!days.length) return <p className="text-xs text-gray-500">No closed day yet.</p>
  const scale = Math.max(high, ...days.map(d => Math.abs(d.pnl)), 1)
  return (
    <div className="mt-2">
      <div className="flex h-28 items-end gap-[2px]">
        {days.map(d => {
          const h = Math.abs(d.pnl) / scale * 100
          return (
            <div key={d.day} className="group relative flex h-full flex-1 flex-col justify-end" title={`${d.day}: ${fmtUsdSigned(d.pnl, 0)}`}>
              <div className={clsx('w-full rounded-t', d.pnl >= 0 ? 'bg-emerald-500/80' : 'bg-red-500/80')} style={{ height: `${Math.max(2, h / 2)}%`, marginBottom: d.pnl >= 0 ? '50%' : undefined, transform: d.pnl < 0 ? 'translateY(100%) scaleY(-1)' : undefined }} />
            </div>
          )
        })}
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-gray-600"><span>{days[0].day}</span><span>target {fmtUsd(low, 0)}–{fmtUsd(high, 0)} a day</span><span>{days[days.length - 1].day}</span></div>
    </div>
  )
}

export function CompositePage() {
  const [d, setD] = useState<CompositeStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    const load = () => api.composite.status().then(x => { if (live) { setD(x); setErr(null) } }).catch(e => { if (live) setErr(String(e?.message ?? e)) })
    load()
    const id = setInterval(load, 30_000)
    return () => { live = false; clearInterval(id) }
  }, [])

  const tick = async () => { setBusy(true); try { setD((await api.composite.tick()).status) } finally { setBusy(false) } }
  const reset = async () => {
    const note = window.prompt('Reset the floors? Every open leg is closed; the record stays on the ledger. A note for the log:', 'reset')
    if (note === null) return
    setBusy(true); try { await api.composite.reset(note || 'reset'); setD(await api.composite.status()) } finally { setBusy(false) }
  }

  if (err) return <div className="card text-sm text-red-300">{err}</div>
  if (!d) return <div className="card text-sm text-gray-500">Loading the one book…</div>
  const ph = PHASE[d.phase] ?? PHASE.running
  const a = d.assess
  const r = d.rules
  const dy = d.daily
  const pnl = d.equity - d.size

  return (
    <div className="space-y-4">
      <section className="card">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 text-base font-semibold text-white"><Layers className="h-4 w-4 text-amber-300" /> One book · {fmtUsd(d.size, 0)}</h2>
            <p className="mt-1 max-w-3xl text-xs text-gray-400">
              The single system: every short the reversal lane, the weekly, factor, unlock and event lanes, the weekly_rs book and the copy-stocks book open —
              the slices with a record — taken into one book at {r.risk_pct}% of equity a leg ({fmtUsd(r.risk_usd, 0)}), a leg's notional at most {r.notional_cap_pct}% of
              equity, {r.max_legs} legs, the ATR trail and a {r.hold_h}h cap for the exit. No new legs at {r.halt_daily_pct}% down on the day, flat at {r.flatten_daily_pct}%;
              flat and stopped at a {r.flatten_dd_pct}% drawdown. Target {fmtUsd(d.target.low, 0)}–{fmtUsd(d.target.high, 0)} a day; the replay of both ledgers said $219–$345.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className={clsx('rounded border px-2 py-0.5 text-xs', ph.cls)} title={ph.note}>{ph.label}</span>
            <button className="btn-ghost text-xs" onClick={tick} disabled={busy}><RefreshCw className={clsx('mr-1 inline h-3 w-3', busy && 'animate-spin')} />Tick</button>
            <button className="btn-ghost text-xs" onClick={reset} disabled={busy}><RotateCcw className="mr-1 inline h-3 w-3" />Reset</button>
          </div>
        </div>

        <div className="mt-4 grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <div><div className="text-[10px] uppercase tracking-wider text-gray-500">Equity</div><div className="text-lg font-semibold text-white">{fmtUsd(d.equity, 0)}</div><div className={clsx('text-xs', pnl >= 0 ? 'text-emerald-300' : 'text-red-300')}>{fmtUsdSigned(pnl, 0)} since the start</div></div>
          <div><div className="text-[10px] uppercase tracking-wider text-gray-500">Today</div><div className={clsx('text-lg font-semibold', d.today >= 0 ? 'text-emerald-300' : 'text-red-300')}>{fmtUsdSigned(d.today, 0)}</div><div className="text-xs text-gray-500">from {fmtUsd(d.sod_balance, 0)} at 00:00 UTC</div></div>
          <div><div className="text-[10px] uppercase tracking-wider text-gray-500">A calendar day</div><div className={clsx('text-lg font-semibold', dy.per_cal_day >= d.target.low ? 'text-emerald-300' : dy.per_cal_day > 0 ? 'text-amber-200' : 'text-red-300')}>{fmtUsdSigned(dy.per_cal_day, 0)}</div><div className="text-xs text-gray-500">{d.target.progress_pct}% of the {fmtUsd(d.target.low, 0)} floor · {dy.cal_days} days</div></div>
          <div><div className="text-[10px] uppercase tracking-wider text-gray-500">Days positive</div><div className="text-lg font-semibold text-white">{dy.pos_days_pct}%</div><div className="text-xs text-gray-500">median day {fmtUsdSigned(dy.median_day, 0)} · t {dy.t ?? '—'}</div></div>
          <div><div className="text-[10px] uppercase tracking-wider text-gray-500">Worst day · drawdown</div><div className="text-lg font-semibold text-red-300">{fmtUsdSigned(dy.worst_day, 0)}</div><div className="text-xs text-gray-500">max DD {fmtUsdSigned(dy.max_dd, 0)} · best {fmtUsdSigned(dy.best_day, 0)}</div></div>
          <div><div className="text-[10px] uppercase tracking-wider text-gray-500">Legs</div><div className="text-lg font-semibold text-white">{d.closed_n} <span className="text-sm text-gray-400">+ {d.open.length} open</span></div><div className="text-xs text-gray-500">{d.closed_n ? Math.round(d.wins / d.closed_n * 100) : 0}% won · {fmtUsdSigned(d.closed_pnl, 0)}</div></div>
        </div>

        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <LimitBar label="Today's loss (realised and floating, against the start-of-day balance)" usedPct={a.daily_used_pct} haltPct={r.halt_daily_pct} flattenPct={r.flatten_daily_pct} room={a.room_today_usd} action={a.action} />
          <LimitBar label={`Drawdown from ${fmtUsd(d.size, 0)} (static)`} usedPct={a.dd_used_pct} haltPct={r.halt_dd_pct} flattenPct={r.flatten_dd_pct} room={a.room_total_usd} action={a.action} />
        </div>
        {(d.halted_day || d.stopped) && (
          <p className="mt-3 text-xs text-amber-200">{d.stopped ? `Stopped ${when(d.stopped.ts)}: ${d.stopped.why}` : `Halted for ${d.halted_day}: ${d.halt_why}`} · {d.halts} halts, {d.flattens} flattens so far</p>
        )}
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <span className={clsx('rounded border px-2 py-0.5', VERDICT[d.verdict.status])}>{d.verdict.status}</span>
          <span className="text-gray-400">{d.verdict.note}</span>
        </div>
      </section>

      <section className="card">
        <h3 className="text-sm font-semibold text-white">Daily P&L</h3>
        <DailyBars days={dy.days} low={d.target.low} high={d.target.high} />
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card">
          <h3 className="text-sm font-semibold text-white">Open legs</h3>
          {d.open.length === 0 ? <p className="mt-2 text-xs text-gray-500">None.</p> : (
            <table className="mt-2 w-full text-xs">
              <thead className="text-left text-[10px] uppercase tracking-wider text-gray-500"><tr><th>Leg</th><th>Source</th><th className="text-right">Notional</th><th className="text-right">Risk</th><th className="text-right">Entry · stop</th><th className="text-right">P&L</th></tr></thead>
              <tbody>
                {d.open.map(t => (
                  <tr key={t.id} className="border-t border-gray-800/60">
                    <td className="py-1 text-gray-200">{t.direction} {t.symbol}</td>
                    <td className="text-gray-400">{t.source ?? '—'}{t.bound ? <span className="ml-1 text-amber-300" title="the size was cut by a cap">·cap</span> : null}</td>
                    <td className="text-right text-gray-300">{fmtUsd(t.notional ?? 0, 0)}</td>
                    <td className="text-right text-gray-400">{fmtUsd(t.risk_usd ?? 0, 0)}</td>
                    <td className="text-right text-gray-400">{t.entry} · {t.stop ?? '—'}</td>
                    <td className={clsx('text-right', (t.pnl_usd ?? 0) >= 0 ? 'text-emerald-300' : 'text-red-300')}>{fmtUsdSigned(t.pnl_usd ?? 0, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card">
          <h3 className="text-sm font-semibold text-white">By source</h3>
          <table className="mt-2 w-full text-xs">
            <thead className="text-left text-[10px] uppercase tracking-wider text-gray-500"><tr><th>Source</th><th className="text-right">Closed</th><th className="text-right">Won</th><th className="text-right">Mean R</th><th className="text-right">P&L</th><th className="text-right">Open</th></tr></thead>
            <tbody>
              {Object.entries(d.by_source).map(([k, v]) => (
                <tr key={k} className="border-t border-gray-800/60">
                  <td className="py-1 text-gray-300" title={v.book}>{k}</td>
                  <td className="text-right text-gray-300">{v.n}</td>
                  <td className="text-right text-gray-400">{v.n ? Math.round(v.wins / v.n * 100) : 0}%</td>
                  <td className="text-right text-gray-400">{v.mean_r == null ? '—' : v.mean_r.toFixed(2)}</td>
                  <td className={clsx('text-right', v.pnl >= 0 ? 'text-emerald-300' : 'text-red-300')}>{fmtUsdSigned(v.pnl, 0)}</td>
                  <td className="text-right text-gray-500">{v.open}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {d.recent.length > 0 && (
            <>
              <h4 className="mt-4 text-[10px] uppercase tracking-wider text-gray-500">Recent closes</h4>
              <ul className="mt-1 space-y-0.5 text-xs">
                {d.recent.map((t, i) => (
                  <li key={i} className="flex justify-between gap-2">
                    <span className="text-gray-300">{t.direction} {t.symbol} <span className="text-gray-500">· {t.source} · {t.close_reason ?? '—'} · {when(t.closed_at)}</span></span>
                    <span className={clsx((t.pnl_usd ?? 0) >= 0 ? 'text-emerald-300' : 'text-red-300')}>{fmtUsdSigned(t.pnl_usd ?? 0, 0)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>

      <section className="card">
        <h3 className="text-sm font-semibold text-white">Log</h3>
        {d.log.length === 0 ? <p className="mt-2 text-xs text-gray-500">Nothing yet.</p> : (
          <ul className="mt-2 max-h-72 space-y-0.5 overflow-auto text-xs">
            {d.log.map((e, i) => (
              <li key={i} className="flex gap-2">
                <span className="w-24 shrink-0 text-gray-600">{when(e.ts as number)}</span>
                <span className={clsx('w-16 shrink-0', e.event === 'opened' ? 'text-emerald-300' : e.event === 'refused' ? 'text-gray-500' : e.event === 'day' ? 'text-gray-600' : 'text-amber-300')}>{String(e.event)}</span>
                <span className="text-gray-300">
                  {e.symbol ? `${String(e.direction ?? '')} ${String(e.symbol)} (${String(e.source ?? '')}) ` : ''}
                  {String(e.why ?? e.note ?? (e.notional ? `${fmtUsd(e.notional as number, 0)} · risk ${fmtUsd((e.risk_usd as number) ?? 0, 0)}${e.bound ? ` · ${String(e.bound)}` : ''}` : e.sod_balance ? `start of day ${fmtUsd(e.sod_balance as number, 0)}` : ''))}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
