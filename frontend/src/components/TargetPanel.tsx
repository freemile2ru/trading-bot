import { useEffect, useState } from 'react'
import { Target, PauseCircle, PlayCircle } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import type { PlanSuggestion, TargetReport } from '../types/overview'
import { SYSTEM_LABEL, type SystemKey } from '../types/overview'

const tone = (v: number | null | undefined) => v == null ? 'text-gray-500' : v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : 'text-gray-300'

/** The $/day tracker: every paper book's P&L per day against the daily target, expected vs realised, breaker state. */
export function TargetPanel() {
  const [r, setR] = useState<TargetReport | null>(null)
  const [sug, setSug] = useState<PlanSuggestion | null>(null)
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)
  const [applied, setApplied] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    const load = () => {
      api.portfolio.target().then(x => { if (live) setR(x) }).catch(() => { /* panel just waits */ })
      api.portfolio.targetSuggest().then(x => { if (live) setSug(x) }).catch(() => { /* optional */ })
    }
    load()
    const id = setInterval(load, 60_000)
    return () => { live = false; clearInterval(id) }
  }, [])
  const applySuggestion = async () => {
    if (!sug || !confirm(`Move paper capital as suggested?\n\n${sug.moves.map(m => `${SYSTEM_LABEL[m.book as SystemKey] ?? m.book}: ${fmtUsd(m.from, 0)} → ${fmtUsd(m.to, 0)}`).join('\n')}`)) return
    setBusy(true)
    try {
      const out = await api.portfolio.targetApplySuggestion()
      setR(out.report); setSug(await api.portfolio.targetSuggest()); setApplied('Plan updated — the tracker moves the money over its next ticks.')
    } catch (e) {
      setApplied(e instanceof Error ? e.message : 'Could not apply')
    } finally { setBusy(false) }
  }
  if (!r) return null

  const t = r.totals
  const pct7 = Math.max(0, Math.min(1, t.avg7 / r.target))
  const pctExp = Math.max(0, Math.min(1, t.expected / r.target))
  const resume = async () => { setBusy(true); try { setR(await api.portfolio.targetResume()) } finally { setBusy(false) } }
  const planned = r.books.filter(b => b.planned)
  const rest = r.books.filter(b => !b.planned)

  return (
    <section className={clsx('card space-y-3', r.paused && 'border-amber-800/70')}>
      <div className="flex flex-wrap items-start gap-4">
        <div className="min-w-[14rem]">
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-gray-500"><Target className="w-3.5 h-3.5" /> Daily target · all paper books</div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className={clsx('text-3xl font-black tabular-nums', tone(t.avg7))}>{fmtUsdSigned(t.avg7, 0)}</span>
            <span className="text-sm text-gray-400">/ day, 7-day avg</span>
          </div>
          <div className="text-xs text-gray-400 mt-0.5">
            target <b className="text-gray-200" title={r.target_why}>{fmtUsd(r.target, 0)}</b>{r.goal != null && <> · goal <span className="text-gray-300">{fmtUsd(r.goal, 0)}</span></>} · today <span className={tone(t.today)}>{fmtUsdSigned(t.today, 0)}</span> · 30-day <span className={tone(t.avg30)}>{fmtUsdSigned(t.avg30, 0)}</span>/day
          </div>
          <div className="mt-2 h-2 rounded-full bg-gray-800 overflow-hidden"><div className={clsx('h-full rounded-full', t.avg7 >= r.target ? 'bg-emerald-500' : 'bg-blue-500')} style={{ width: `${pct7 * 100}%` }} /></div>
          <div className="mt-1 text-[11px] text-gray-500">{t.avg7 >= r.target ? 'on target' : `${fmtUsd(r.gap7, 0)}/day short of target`}{r.measuring_since && ` · measuring since ${r.measuring_since}`}</div>
          {r.target_why && <div className="mt-0.5 text-[10px] text-gray-600">Target = {r.target_why}. It rises as books earn their verdicts and the ladder funds them.</div>}
        </div>

        <div className="min-w-[14rem]">
          <div className="text-[10px] uppercase tracking-wider text-gray-500">Expected from the evidence</div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className={clsx('text-2xl font-black tabular-nums', tone(t.expected))}>{fmtUsdSigned(t.expected, 0)}</span>
            <span className="text-sm text-gray-400">/ day at current positions</span>
          </div>
          <div className="mt-2 h-2 rounded-full bg-gray-800 overflow-hidden"><div className="h-full rounded-full bg-gray-500" style={{ width: `${pctExp * 100}%` }} /></div>
          <div className="mt-1 text-[11px] text-gray-500">
            {t.expected >= r.target ? 'the books in place should cover the target' : `${fmtUsd(r.gap_expected, 0)}/day more needed in deployed edge`} · {fmtUsd(t.equity, 0)} equity across the books · target = {(r.target / t.equity * 100).toFixed(2)}% a day
          </div>
        </div>

        <div className="ml-auto text-right text-xs">
          {r.paused ? (
            <div className="space-y-1">
              <div className="inline-flex items-center gap-1.5 rounded border border-amber-800/60 bg-amber-950/40 px-2 py-1 text-amber-300 font-semibold"><PauseCircle className="w-3.5 h-3.5" /> Breaker tripped</div>
              <div className="text-gray-400 max-w-xs">{r.paused.reason}</div>
              <button onClick={resume} disabled={busy} className="inline-flex items-center gap-1 text-blue-400 hover:text-blue-300 disabled:opacity-50"><PlayCircle className="w-3.5 h-3.5" /> Resume programme sizing</button>
            </div>
          ) : (
            <div className="text-gray-500">Breaker: pauses programme sizing if the 7-day average drops below {fmtUsdSigned(r.trip_avg7, 0)}/day</div>
          )}
          <button onClick={() => setOpen(o => !o)} className="mt-2 text-gray-400 hover:text-gray-200">{open ? 'Hide books' : 'Show books'}</button>
        </div>
      </div>

      {open && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs min-w-[46rem]">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-gray-500">
                <th className="text-left font-semibold pb-1">Book</th>
                <th className="text-right font-semibold pb-1">Equity</th>
                <th className="text-right font-semibold pb-1">Programme</th>
                <th className="text-right font-semibold pb-1">Today</th>
                <th className="text-right font-semibold pb-1">7-day</th>
                <th className="text-right font-semibold pb-1">30-day</th>
                <th className="text-right font-semibold pb-1">Expected</th>
                <th className="text-left font-semibold pb-1 pl-3">Why</th>
              </tr>
            </thead>
            <tbody>
              {[...planned, ...rest].map((b, i) => (
                <tr key={b.book} className={clsx('border-t border-gray-800/60', i === planned.length && 'border-t-2 border-gray-700')}>
                  <td className="py-1 text-gray-200">{b.label}{b.open_positions > 0 && <span className="ml-1 text-[10px] text-gray-500">{b.open_positions} open</span>}</td>
                  <td className="py-1 text-right font-mono text-gray-300">{fmtUsd(b.equity, 0)}</td>
                  <td className="py-1 text-right font-mono text-gray-400" title={b.shortfall ? `${fmtUsdSigned(b.shortfall, 0)} still to move — cash is tied up in open positions` : undefined}>
                    {b.planned ? `${fmtUsd(b.planned, 0)}${b.shortfall ? ` (${fmtUsdSigned(b.shortfall, 0)} pending)` : ''}` : '—'}
                  </td>
                  <td className={clsx('py-1 text-right font-mono', tone(b.today))}>{fmtUsdSigned(b.today, 0)}</td>
                  <td className={clsx('py-1 text-right font-mono', tone(b.avg7))}>{fmtUsdSigned(b.avg7, 0)}</td>
                  <td className={clsx('py-1 text-right font-mono', tone(b.avg30))}>{fmtUsdSigned(b.avg30, 0)}</td>
                  <td className={clsx('py-1 text-right font-mono', b.expected == null ? 'text-gray-600' : tone(b.expected))}>{b.expected == null ? '—' : fmtUsdSigned(b.expected, 0)}</td>
                  <td className="py-1 pl-3 text-gray-500 truncate max-w-[18rem]">{b.expected_why}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 rounded-lg border border-gray-800 bg-gray-800/30 px-3 py-2 space-y-1.5">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="text-xs font-semibold text-gray-200">What the evidence would move</span>
              <span className="text-[10px] text-gray-500">advisory — nothing moves until you apply it</span>
              {sug?.changed && (
                <button onClick={applySuggestion} disabled={busy} className="ml-auto text-xs font-semibold text-blue-400 hover:text-blue-300 disabled:opacity-50">
                  {busy ? 'Applying…' : 'Apply suggested plan'}
                </button>
              )}
            </div>
            {!sug ? <div className="text-[11px] text-gray-500">Loading…</div> : !sug.changed ? (
              <div className="text-[11px] text-gray-500">No change suggested: no book with {14}+ days of history has a negative score while another has a positive one.</div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-0.5 text-[11px]">
                {sug.moves.map(m => (
                  <div key={m.book} className="flex items-baseline gap-2">
                    <span className="text-gray-300 w-44 truncate">{SYSTEM_LABEL[m.book as SystemKey] ?? m.book}</span>
                    <span className="font-mono text-gray-400">{fmtUsd(m.from, 0)} → {fmtUsd(m.to, 0)}</span>
                    <span className={clsx('font-mono', tone(m.delta))}>{fmtUsdSigned(m.delta, 0)}</span>
                    <span className="text-gray-600 truncate">{m.why}</span>
                  </div>
                ))}
              </div>
            )}
            {applied && <div className="text-[11px] text-emerald-300">{applied}</div>}
            {r.plan_note && <div className="text-[10px] text-gray-600">Plan in force: {r.plan_note.note} ({new Date(r.plan_note.at * 1000).toLocaleDateString()})</div>}
            {r.auto && (
              <div className={clsx('rounded-lg border px-3 py-2 text-[11px]', r.auto.on ? 'border-emerald-900/60 bg-emerald-950/20 text-emerald-100/90' : 'border-gray-800 bg-gray-900/60 text-gray-400')}>
                <b className="text-gray-100">Automatic allocation {r.auto.on ? 'on' : 'off'}</b> — {r.auto.on ? `runs once a day after ${String(r.auto.hour_utc).padStart(2, '0')}:00 UTC: ` : 'set CAPITAL_AUTO=true to run it: '}
                {sug?.ladder_rule ?? 'a book whose 40-trade verdict shows an edge is promoted to 10% of the total; one that shows no edge is cut to the floor.'}
                {r.auto.last && <div className="mt-1 text-gray-400">Last run {new Date(r.auto.last.at * 1000).toLocaleString()}: {r.auto.last.moves.length ? r.auto.last.moves.map(m => `${SYSTEM_LABEL[m.book as SystemKey] ?? m.book} ${m.delta > 0 ? '+' : ''}${fmtUsd(m.delta, 0)}`).join(' · ') : (r.auto.last.note ?? 'no change')}</div>}
              </div>
            )}
            <div className="text-[10px] text-gray-600">{sug?.rule}</div>
          </div>
          <div className="mt-2 text-[10px] text-gray-600">
            Per-day figures come from each book's equity curve with deposits and withdrawals taken out; days without data count as zero in the averages. "Expected" is what the evidence behind a book says it should earn at its current positions — blank where no edge has been shown. "Programme" is each book's share of the $50k; a withdrawal that cannot be made yet (cash tied up in open positions) shows as pending and completes as they close.
          </div>
        </div>
      )}
    </section>
  )
}
