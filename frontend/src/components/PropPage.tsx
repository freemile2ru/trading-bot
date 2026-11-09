import { useEffect, useState } from 'react'
import { Trophy, RefreshCw, RotateCcw } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import type { PropStatus } from '../types/prop'

const PHASE: Record<string, { label: string; cls: string; note: string }> = {
  funding: { label: 'Funding', cls: 'border-gray-700 bg-gray-800 text-gray-300', note: 'waiting for the capital plan to fill the wallet to the account size' },
  evaluation: { label: 'Evaluation', cls: 'border-blue-800/60 bg-blue-900/40 text-blue-200', note: 'reach the target before a floor' },
  funded: { label: 'Passed · funded rules', cls: 'border-emerald-800/60 bg-emerald-900/40 text-emerald-200', note: 'the target was reached; the same floors apply, no target' },
  stopped: { label: 'Stopped', cls: 'border-red-800/60 bg-red-900/40 text-red-200', note: 'the attempt ended on our 5% rule — a new attempt is a new challenge' },
}

function when(ts: number | string | null | undefined): string {
  if (!ts) return '—'
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/** A limit bar: how much of a floor's room is used, with our halt and flatten marks. */
function LimitBar({ label, usedPct, limitPct, haltPct, flattenPct, room, action }: {
  label: string; usedPct: number; limitPct: number; haltPct: number; flattenPct: number; room: number; action: string
}) {
  const used = Math.max(0, Math.min(100, usedPct))
  const halt = haltPct / limitPct * 100, flat = flattenPct / limitPct * 100
  const tone = used >= flat ? 'bg-red-500' : used >= halt ? 'bg-amber-400' : 'bg-emerald-500'
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-gray-400">{label}</span>
        <span className="text-gray-300">{used.toFixed(0)}% of {limitPct}% · {fmtUsd(room, 0)} of room</span>
      </div>
      <div className="relative mt-1 h-2 rounded bg-gray-800">
        <div className={clsx('h-2 rounded', tone)} style={{ width: `${used}%` }} />
        <div className="absolute top-0 h-2 border-l border-amber-300/70" style={{ left: `${halt}%` }} title={`halt entries at ${haltPct}%`} />
        <div className="absolute top-0 h-2 border-l border-red-400/80" style={{ left: `${flat}%` }} title={`flatten at ${flattenPct}%`} />
      </div>
      <div className="mt-0.5 flex justify-between text-[10px] text-gray-600">
        <span>halt {haltPct}% · flatten {flattenPct}%</span>
        <span className={clsx(action === 'ok' ? 'text-gray-600' : action === 'halt' ? 'text-amber-300' : 'text-red-300')}>{action === 'ok' ? '' : action}</span>
      </div>
    </div>
  )
}

export function PropPage() {
  const [d, setD] = useState<PropStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    const load = () => api.prop.status().then(x => { if (live) { setD(x); setErr(null) } }).catch(e => { if (live) setErr(String(e?.message ?? e)) })
    load()
    const id = setInterval(load, 30_000)
    return () => { live = false; clearInterval(id) }
  }, [])

  const tick = async () => { setBusy(true); try { setD((await api.prop.tick()).status) } finally { setBusy(false) } }
  const reset = async () => {
    const note = window.prompt('Start a new attempt? Every open leg is closed and the current attempt goes on record. A note for the record:', 'new attempt')
    if (note === null) return
    setBusy(true); try { setD(await api.prop.reset(note || 'new attempt')) } finally { setBusy(false) }
  }

  if (err) return <div className="card text-sm text-red-300">{err}</div>
  if (!d) return <div className="card text-sm text-gray-500">Loading the prop book…</div>
  const ph = PHASE[d.phase] ?? PHASE.evaluation
  const a = d.assess
  const pnl = d.equity - d.plan.size

  return (
    <div className="space-y-4">
      <section className="card">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 text-base font-semibold text-white"><Trophy className="h-4 w-4 text-amber-300" /> Prop · {d.plan.firm} {d.plan.name} {fmtUsd(d.plan.size, 0)}</h2>
            <p className="mt-1 max-w-3xl text-xs text-gray-400">
              The reversal lane's picks and the auction book's entries, run as the firm's account would take them: its size, its leverage caps
              ({d.leverage.default}×, {Object.entries(d.leverage.overrides).map(([k, v]) => `${k} ${v}×`).join(', ')}), its {d.plan.max_open} positions, its
              floors. The book acts first — no entries at {d.ours.halt_daily_pct}% of a day or {d.ours.halt_dd_pct}% down, everything closed at
              {' '}{d.ours.flatten_daily_pct}% or {d.ours.flatten_dd_pct}% — and records where the firm would have ended the account.
              {' '}<a className="text-blue-400 hover:underline" href={d.plan.source} target="_blank" rel="noreferrer">rulebook</a>
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className={clsx('rounded border px-2 py-0.5 text-[11px] uppercase tracking-wider', ph.cls)} title={ph.note}>{ph.label}</span>
            <span className="text-xs text-gray-500">attempt {d.attempt} · day {Math.max(1, Math.ceil(d.days))}</span>
            <button onClick={tick} disabled={busy} className="flex items-center gap-1.5 px-2 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 disabled:opacity-50" title="Read the account against its floors now"><RefreshCw className={clsx('h-3.5 w-3.5', busy && 'animate-spin')} /></button>
            <button onClick={reset} disabled={busy} className="flex items-center gap-1.5 px-2 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 disabled:opacity-50" title="New attempt (what buying another challenge would be)"><RotateCcw className="h-3.5 w-3.5" /></button>
          </div>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-5">
          <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
            <div className="text-[10px] uppercase tracking-wider text-gray-500">Equity</div>
            <div className="text-lg font-semibold text-white">{fmtUsd(d.equity, 0)}</div>
            <div className={clsx('text-xs', pnl >= 0 ? 'text-emerald-300' : 'text-red-300')}>{fmtUsdSigned(pnl, 0)} · open {fmtUsdSigned(d.open_pnl, 0)}</div>
          </div>
          <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
            <div className="text-[10px] uppercase tracking-wider text-gray-500">Balance (realised)</div>
            <div className="text-lg font-semibold text-white">{fmtUsd(d.balance, 0)}</div>
            <div className="text-xs text-gray-500">start of day {fmtUsd(d.sod_balance, 0)}</div>
          </div>
          <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
            <div className="text-[10px] uppercase tracking-wider text-gray-500">Target {d.plan.target_pct}%</div>
            <div className="text-lg font-semibold text-white">{Math.max(0, d.target.progress_pct).toFixed(0)}%</div>
            <div className="mt-1 h-1.5 rounded bg-gray-800"><div className="h-1.5 rounded bg-blue-500" style={{ width: `${Math.max(0, Math.min(100, d.target.progress_pct))}%` }} /></div>
            <div className="mt-0.5 text-[10px] text-gray-500">{fmtUsd(Math.max(0, d.target.to_go_usd), 0)} to {fmtUsd(d.target.usd, 0)}</div>
          </div>
          <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
            <div className="text-[10px] uppercase tracking-wider text-gray-500">Positions</div>
            <div className="text-lg font-semibold text-white">{d.open.length}<span className="text-sm text-gray-500"> / {d.plan.max_open}</span></div>
            <div className="text-xs text-gray-500">{Object.entries(d.by_source).map(([k, v]) => `${k} ${v.open}/${v.cap}`).join(' · ')}</div>
          </div>
          <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
            <div className="text-[10px] uppercase tracking-wider text-gray-500">Record</div>
            <div className="text-lg font-semibold text-white">{d.closed_n}<span className="text-sm text-gray-500"> closed</span></div>
            <div className={clsx('text-xs', d.closed_pnl >= 0 ? 'text-emerald-300' : 'text-red-300')}>{fmtUsdSigned(d.closed_pnl, 0)} · {d.closed_n ? Math.round(d.wins / d.closed_n * 100) : 0}% won</div>
          </div>
        </div>

        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <LimitBar label={`Today's loss (floor ${fmtUsd(a.floors.daily, 0)})`} usedPct={a.daily_used_pct} limitPct={d.plan.daily_loss_pct}
                    haltPct={d.ours.halt_daily_pct} flattenPct={d.ours.flatten_daily_pct} room={a.room_today_usd} action={a.action} />
          <LimitBar label={`Drawdown, static (floor ${fmtUsd(a.floors.max, 0)})`} usedPct={a.dd_used_pct} limitPct={d.plan.max_dd_pct}
                    haltPct={d.ours.halt_dd_pct} flattenPct={d.ours.flatten_dd_pct} room={a.room_total_usd} action={a.action} />
        </div>
        {(d.halted_day || d.breach || d.stopped) && (
          <div className="mt-3 space-y-1 text-xs">
            {d.halted_day && <p className="text-amber-300">Halted for {d.halted_day}: {d.halt_why}</p>}
            {d.breach && <p className="text-red-300">The firm would have closed this account on {when(d.breach.ts)}: {d.breach.rule.replace(/_/g, ' ')} at {fmtUsd(d.breach.equity, 0)}.</p>}
            {d.stopped && <p className="text-red-300">Stopped {when(d.stopped.ts)}: {d.stopped.why}</p>}
          </div>
        )}
        <p className="mt-3 text-[11px] text-gray-500">
          Execution: {d.exec.enabled ? <span className="text-emerald-300">mirroring to {d.exec.mode} ({d.exec.url})</span> : <>paper only — set PROP_EXEC=beta and PROPR_API_KEY to mirror every leg to the firm's sandbox</>}
          {' '}· halts {d.halts} · flattens {d.flattens} · fee {fmtUsd(d.plan.fee_usd, 0)} · {Math.round(d.plan.profit_split * 100)}% split
        </p>
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card">
          <h3 className="text-sm font-semibold text-white">Open legs</h3>
          {d.open.length === 0 ? <p className="mt-2 text-xs text-gray-500">Nothing open. Legs arrive with the reversal lane's 4-hour picks and the auction book's session entries.</p> : (
            <table className="mt-2 w-full text-xs">
              <thead className="text-left text-[10px] uppercase tracking-wider text-gray-500"><tr><th>Leg</th><th>Source</th><th className="text-right">Notional</th><th className="text-right">Lev</th><th className="text-right">Entry → stop</th><th className="text-right">P&L</th><th>Firm</th></tr></thead>
              <tbody>
                {d.open.map(t => (
                  <tr key={t.id} className="border-t border-gray-800/60">
                    <td className="py-1"><span className={clsx('mr-1 rounded px-1 text-[10px] uppercase', t.direction === 'long' ? 'bg-emerald-900/50 text-emerald-200' : 'bg-red-900/50 text-red-200')}>{t.direction}</span>{t.symbol.replace('USDT', '')}<span className="text-gray-600"> {t.coin}</span></td>
                    <td className="text-gray-400">{t.source}</td>
                    <td className="text-right text-gray-300">{fmtUsd(t.notional, 0)}</td>
                    <td className="text-right text-gray-400">{t.leverage}×</td>
                    <td className="text-right text-gray-400">{t.entry.toPrecision(5)} → {t.stop?.toPrecision(5)}</td>
                    <td className={clsx('text-right', (t.pnl_usd ?? 0) >= 0 ? 'text-emerald-300' : 'text-red-300')}>{fmtUsdSigned(t.pnl_usd, 0)}</td>
                    <td className="text-gray-500">{t.exec?.opened ? 'mirrored' : t.exec?.error ? <span className="text-amber-300" title={t.exec.error}>error</span> : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card">
          <h3 className="text-sm font-semibold text-white">By source</h3>
          <table className="mt-2 w-full text-xs">
            <thead className="text-left text-[10px] uppercase tracking-wider text-gray-500"><tr><th>Source</th><th className="text-right">Risk a leg</th><th className="text-right">Closed</th><th className="text-right">Won</th><th className="text-right">P&L</th></tr></thead>
            <tbody>
              {Object.entries(d.by_source).map(([k, v]) => (
                <tr key={k} className="border-t border-gray-800/60">
                  <td className="py-1 text-gray-300">{k}</td>
                  <td className="text-right text-gray-400">{v.risk_pct}%</td>
                  <td className="text-right text-gray-300">{v.n}</td>
                  <td className="text-right text-gray-400">{v.n ? Math.round(v.wins / v.n * 100) : 0}%</td>
                  <td className={clsx('text-right', v.pnl >= 0 ? 'text-emerald-300' : 'text-red-300')}>{fmtUsdSigned(v.pnl, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {d.recent.length > 0 && (
            <>
              <h4 className="mt-4 text-[10px] uppercase tracking-wider text-gray-500">Recent closes</h4>
              <ul className="mt-1 space-y-0.5 text-xs">
                {d.recent.map((t, i) => (
                  <li key={i} className="flex justify-between text-gray-400">
                    <span>{when(t.closed_at)} · {t.direction} {t.symbol.replace('USDT', '')} <span className="text-gray-600">{t.source} · {t.close_reason}</span></span>
                    <span className={(t.pnl_usd ?? 0) >= 0 ? 'text-emerald-300' : 'text-red-300'}>{fmtUsdSigned(t.pnl_usd, 0)}</span>
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
          <ul className="mt-2 max-h-80 space-y-0.5 overflow-y-auto text-xs">
            {d.log.map((e, i) => (
              <li key={i} className="flex gap-2 text-gray-400">
                <span className="w-24 shrink-0 text-gray-600">{when(e.ts)}</span>
                <span className={clsx('w-16 shrink-0 uppercase text-[10px] tracking-wider', e.event === 'opened' ? 'text-emerald-300' : e.event === 'refused' ? 'text-gray-500' :
                  e.event === 'breach' || e.event === 'flatten' || e.event === 'stopped' ? 'text-red-300' : e.event === 'halt' ? 'text-amber-300' : e.event === 'passed' ? 'text-blue-300' : 'text-gray-400')}>{e.event}</span>
                <span className="truncate">
                  {e.symbol ? `${e.direction ?? ''} ${String(e.symbol).replace('USDT', '')} ` : ''}
                  {e.why ?? e.note ?? (e.notional ? `${fmtUsd(e.notional, 0)} at ${e.leverage}× (${e.coin})` : '')}
                  {e.event === 'day' ? `start-of-day balance ${fmtUsd(e.sod_balance, 0)}` : ''}
                  {e.event === 'passed' ? `balance ${fmtUsd(e.balance, 0)} after ${e.days} days` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
        {d.attempts.length > 0 && (
          <p className="mt-3 text-[11px] text-gray-500">Earlier attempts: {d.attempts.map(x => `#${x.attempt} ${x.phase} at ${fmtUsd(x.balance, 0)} (${x.note})`).join(' · ')}</p>
        )}
      </section>
    </div>
  )
}
