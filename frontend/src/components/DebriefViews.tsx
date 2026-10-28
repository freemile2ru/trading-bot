import { useEffect, useState } from 'react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsdSigned } from '../utils/format'
import { SYSTEM_LABEL, type SystemKey } from '../types/overview'
import type { DebriefSummary, TradeDebrief } from '../types/debrief'

// Outcome tags: green for wins, amber for "right but…" losses, red for plain losses.
export const TAG_TONE: Record<string, string> = {
  'clean win': 'bg-emerald-900/40 text-emerald-300',
  'win, exited early': 'bg-emerald-900/40 text-emerald-300',
  'fees ate it': 'bg-amber-900/30 text-amber-300',
  'gave back the gain': 'bg-amber-900/30 text-amber-300',
  'stopped, then reversed': 'bg-amber-900/30 text-amber-300',
  'exited early': 'bg-amber-900/30 text-amber-300',
  'never worked': 'bg-red-900/40 text-red-300',
  'clean loss': 'bg-red-900/40 text-red-300',
}
const TAG_HELP: Record<string, string> = {
  'clean win': 'made money and price didn\'t run much further after the exit',
  'win, exited early': 'made money, but price kept going 1R+ further after the exit',
  'fees ate it': 'right on price (gross profit) but lost after costs',
  'gave back the gain': 'was +1R or better at some point, then closed at a loss',
  'stopped, then reversed': 'stopped out, then price went back through the entry',
  'exited early': 'after the exit, price moved 1R+ in the trade\'s favour',
  'never worked': 'never got more than +0.3R in its favour',
  'clean loss': 'a loss with none of the patterns above',
}
const THESIS_TONE: Record<string, string> = { right: 'text-emerald-400', 'partly right': 'text-amber-300', wrong: 'text-red-400', 'no thesis': 'text-gray-400' }
const r = (v: number | null | undefined) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}R`)

export function Tag({ t, primary }: { t: string; primary?: boolean }) {
  return (
    <span title={TAG_HELP[t]} className={clsx('px-1.5 py-0.5 rounded text-[10px] font-semibold whitespace-nowrap', TAG_TONE[t] ?? 'bg-gray-800 text-gray-300',
      primary && 'ring-1 ring-current')}>{t}</span>
  )
}

/** The debrief block inside the trade drawer. */
export function TradeDebriefBlock({ system, id, status }: { system: SystemKey; id: string; status?: string }) {
  const [d, setD] = useState<TradeDebrief | null>(null)
  const [missing, setMissing] = useState(false)
  useEffect(() => {
    let stale = false
    setD(null); setMissing(false)
    api.debriefs.one(system, id).then(x => { if (!stale) setD(x) }).catch(() => { if (!stale) setMissing(true) })
    return () => { stale = true }
  }, [system, id])
  if (status === 'open') return null
  if (missing) {
    return (
      <p className="text-xs text-gray-500">
        Debrief pending — trades are debriefed once the window after the exit has passed (1 h for short trades, 24 h for swing trades),
        so it can see whether price kept going.
      </p>
    )
  }
  if (!d) return null
  const m = d.metrics
  return (
    <div className="space-y-2 text-xs">
      <div className="flex flex-wrap gap-1.5">{d.tags.map(t => <Tag key={t} t={t} primary={t === d.primary} />)}</div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {([['Best during', m.best_r], ['Worst during', m.worst_r], [`Best ${m.window_h ?? ''}h after exit`, m.post_best_r],
           [`Worst ${m.window_h ?? ''}h after exit`, m.post_worst_r]] as const).map(([k, v]) => (
          <div key={k} className="bg-gray-800/50 rounded px-2 py-1.5">
            <div className="text-[10px] text-gray-500">{k}</div>
            <div className="tabular-nums text-gray-200">{r(v as number | null)}</div>
          </div>
        ))}
      </div>
      {d.claude ? (
        <div className="bg-gray-800/40 rounded-lg px-3 py-2 space-y-1">
          <div className="text-[11px] text-gray-500">
            Claude's debrief · thesis <span className={clsx('font-semibold', THESIS_TONE[d.claude.thesis])}>{d.claude.thesis}</span> · cause: {d.claude.cause}
          </div>
          <p className="text-gray-300">{d.claude.what_happened}</p>
          <p className="text-gray-100"><span className="text-gray-500">Lesson:</span> {d.claude.lesson}</p>
        </div>
      ) : <p className="text-gray-500">No Claude debrief (scalps get the automatic tags only, or it's queued).</p>}
    </div>
  )
}

/** History page section: which patterns keep recurring, and Claude's recent lessons. */
export function DebriefSection({ onOpen }: { onOpen: (system: SystemKey, id: string) => void }) {
  const [s, setS] = useState<DebriefSummary | null>(null)
  useEffect(() => {
    const load = () => api.debriefs.summary().then(setS).catch(() => {})
    load()
    const id = setInterval(load, 120_000)
    return () => clearInterval(id)
  }, [])
  if (!s) return <div className="card h-40 animate-pulse" />
  const max = Math.max(1, ...s.by_tag.map(t => t.trades))
  return (
    <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)] gap-4 items-start">
      <section className="card space-y-3">
        <div>
          <div className="text-sm font-semibold text-white">How trades end</div>
          <p className="text-[11px] text-gray-500 mt-0.5">
            {s.trades} closed trades from every active system, tagged automatically after the exit (a trade can carry more than one tag).
          </p>
        </div>
        <div className="space-y-1.5">
          {s.by_tag.map(t => (
            <div key={t.tag} className="grid grid-cols-[10.5rem_minmax(0,1fr)_7rem] items-center gap-2 text-xs" title={TAG_HELP[t.tag]}>
              <Tag t={t.tag} />
              <div className="h-3 bg-gray-800 rounded-full overflow-hidden">
                <div className="h-full rounded-full bg-gray-400" style={{ width: `${(t.trades / max) * 100}%` }} />
              </div>
              <span className="text-right tabular-nums text-gray-300">
                {t.trades} · <span className={t.net >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtUsdSigned(t.net, 0)}</span>
              </span>
            </div>
          ))}
        </div>
        {!!s.causes.length && (
          <div>
            <div className="text-xs font-semibold text-gray-200 mb-1">What Claude says caused them</div>
            <div className="flex flex-wrap gap-1.5">
              {s.causes.map(c => <span key={c.cause} className="px-2 py-0.5 rounded bg-gray-800 text-[11px] text-gray-300">{c.cause} · {c.trades}</span>)}
            </div>
          </div>
        )}
        <p className="text-[10px] text-gray-500">Claude debriefs today: {s.calls_today}/{s.daily_calls} calls (its own budget, up to 5 trades per call).</p>
      </section>

      <section className="card space-y-2">
        <div className="text-sm font-semibold text-white">Claude's recent lessons</div>
        {!s.recent.length ? <p className="text-xs text-gray-500">No Claude debriefs yet.</p> : s.recent.slice(0, 10).map(d => (
          <button key={d.system + d.trade_id} onClick={() => onOpen(d.system as SystemKey, d.trade_id)}
            className="w-full text-left bg-gray-800/40 hover:bg-gray-800/70 rounded-lg px-3 py-2 text-xs transition-colors">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-white">{d.symbol.replace('USDT', '')}</span>
              <span className="text-gray-500">{SYSTEM_LABEL[d.system as SystemKey] ?? d.system}</span>
              <Tag t={d.primary} />
              <span className={clsx('ml-auto text-[11px]', THESIS_TONE[d.claude!.thesis])}>thesis {d.claude!.thesis}</span>
              <span className={clsx('tabular-nums', (d.metrics.r ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400')}>{r(d.metrics.r)}</span>
            </div>
            <p className="text-gray-200 mt-1">{d.claude!.lesson}</p>
          </button>
        ))}
      </section>
    </div>
  )
}
