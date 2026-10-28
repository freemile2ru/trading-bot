import { useCallback, useEffect, useState } from 'react'
import { ShieldCheck, ShieldAlert } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import type { GuardsStatus } from '../types/guards'

const FAMILY: Record<string, string> = { autotrader: 'Autotrader + its rules-only twin', signals: 'Signal books (Claude + rules only)' }
const until = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' })
const ago = (ts: number) => { const m = Math.round((Date.now() / 1000 - ts) / 60); return m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ago` }

/** The direction gate, the open-risk cap and strategy reviews after consistent losses (trading/guards.py). */
export function GuardsPanel() {
  const [g, setG] = useState<GuardsStatus | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [showSkips, setShowSkips] = useState(false)
  const load = useCallback(() => api.guards.status().then(setG).catch(() => {}), [])
  useEffect(() => { load(); const id = setInterval(load, 60_000); return () => clearInterval(id) }, [load])
  if (!g) return <div className="card h-28 animate-pulse" />

  async function clear(f: string) {
    setBusy(f)
    try { setG(await api.guards.clear(f)) } finally { setBusy(null) }
  }
  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-sm font-semibold text-white">Guards</h3>
        <span className="text-[11px] text-gray-500">
          A direction is traded only while its last {g.gate_n} closed trades are net positive; open risk is capped at {g.max_open_risk_pct}% of equity;
          after {g.triggers.losses_in_a_row} losses in a row (or the last 10 at {g.triggers.last10_net_r}R or worse) risk is halved and Claude reviews the strategy —
          its changes are bounded and expire after {g.triggers.adjust_hours}h.{!g.enabled && ' Turned OFF in .env.'}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2">
        {(['long', 'short'] as const).map(d => {
          const s = g.directions[d]
          return (
            <div key={d} className={clsx('rounded-lg px-3 py-2 text-xs', s.open ? 'bg-emerald-950/40 border border-emerald-900/50' : 'bg-red-950/40 border border-red-900/50')}>
              <div className="flex items-center gap-1.5 font-semibold">
                {s.open ? <ShieldCheck className="w-4 h-4 text-emerald-400" /> : <ShieldAlert className="w-4 h-4 text-red-400" />}
                <span className="capitalize text-gray-100">{d}s</span>
                <span className={s.open ? 'text-emerald-300' : 'text-red-300'}>{s.open ? 'allowed' : 'paused by the gate'}</span>
              </div>
              <div className="text-[11px] text-gray-400 mt-0.5">
                last {s.n} {d}s: <span className={s.net_r > 0 ? 'text-emerald-400' : s.net_r < 0 ? 'text-red-400' : ''}>{s.net_r > 0 ? '+' : ''}{s.net_r}R</span>
                {s.n < g.gate_n && ` (fewer than ${g.gate_n} yet — not gated)`}
                {!!s.shadows && <> · {s.shadows} of them skipped setups tracked as shadows</>}
              </div>
              {s.next_needs_r != null && (
                <div className="text-[11px] text-gray-500">reopens when the next close beats {s.next_needs_r > 0 ? '+' : ''}{s.next_needs_r}R — or as better trades push the old ones out</div>
              )}
              {!!s.trades.length && (
                <details className="mt-1">
                  <summary className="text-[11px] text-gray-500 cursor-pointer hover:text-gray-300">the {s.trades.length} trades, newest first</summary>
                  <div className="mt-1 space-y-0.5">
                    {s.trades.map((t, i) => (
                      <div key={i} className="flex gap-2 text-[11px] text-gray-400">
                        <span className="w-16 font-semibold text-gray-300">{t.symbol.replace('USDT', '')}</span>
                        <span className="flex-1 truncate">{t.book.replace(/_/g, ' ')}{t.gated && ' · shadow'}</span>
                        <span className={clsx('tabular-nums', t.r > 0 ? 'text-emerald-400' : t.r < 0 ? 'text-red-400' : '')}>{t.r > 0 ? '+' : ''}{t.r.toFixed(2)}R</span>
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          )
        })}
      </div>
      {g.thesis_exit?.enabled && (
        <div className="rounded-lg px-3 py-2 text-xs bg-gray-800/40">
          <span className="font-semibold text-gray-100">Thesis-faded exit</span>
          <span className="text-gray-400"> — a trade {g.thesis_exit.min_age_h}h+ old closes once its coin's conviction is below {g.thesis_exit.fade_below} on two readings. </span>
          {g.thesis_exit.closed === 0 ? <span className="text-gray-500">No trade closed this way yet.</span> : (
            <span className="text-gray-300 tabular-nums">
              {g.thesis_exit.closed} closed{g.thesis_exit.twins_running ? ` (${g.thesis_exit.twins_running} twin${g.thesis_exit.twins_running > 1 ? 's' : ''} still running)` : ''}
              {g.thesis_exit.pairs > 0 && g.thesis_exit.saved_r != null && <> · closing made {g.thesis_exit.avg_r}R vs {g.thesis_exit.twin_avg_r}R holding ·{' '}
                <span className={g.thesis_exit.saved_r >= 0 ? 'text-emerald-400' : 'text-red-400'}>{g.thesis_exit.saved_r > 0 ? '+' : ''}{g.thesis_exit.saved_r}R a trade</span></>}
            </span>
          )}
        </div>
      )}
      <div className="space-y-2">
        {Object.entries(g.families).map(([f, fam]) => {
          const a = fam.adjustments
          const active = a && a.until
          return (
            <div key={f} className={clsx('rounded-lg px-3 py-2 text-xs', active ? 'bg-amber-950/30 border border-amber-900/50' : 'bg-gray-800/40')}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-gray-100">{FAMILY[f] ?? f}</span>
                {active ? (
                  <>
                    <span className="text-amber-300">adjusted until {until(a.until!)} ({a.source})</span>
                    <span className="text-gray-300">risk ×{a.risk_mult}</span>
                    {!!a.conviction_add && <span className="text-gray-300">conviction +{a.conviction_add}</span>}
                    {a.max_open_risk_pct != null && a.max_open_risk_pct < g.max_open_risk_pct && <span className="text-gray-300">open-risk cap {a.max_open_risk_pct}%</span>}
                    {Object.entries(a.paused ?? {}).map(([d, t]) => <span key={d} className="text-red-300">{d}s paused to {until(t)}</span>)}
                    <button onClick={() => clear(f)} disabled={busy === f} className="ml-auto px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 text-gray-200 disabled:opacity-50">
                      {busy === f ? 'Clearing…' : 'Clear now'}
                    </button>
                  </>
                ) : fam.active === false ? <span className="text-gray-500">no books running — not reviewed</span>
                  : <span className="text-gray-500">normal rules{fam.review_due ? ` — review pending: ${fam.review_due}` : ''}</span>}
              </div>
              {active && a.reason && <p className="text-[11px] text-gray-400 mt-1">{a.reason}</p>}
            </div>
          )
        })}
      </div>
      {g.skips.length > 0 && (
        <div>
          <button onClick={() => setShowSkips(s => !s)} className="text-[11px] text-blue-300 hover:underline">
            {showSkips ? 'Hide' : 'Show'} what the guards kept out ({g.skips.length})
          </button>
          {showSkips && (
            <ul className="mt-1 space-y-0.5 max-h-48 overflow-y-auto text-[11px] text-gray-400">
              {g.skips.map((s, i) => (
                <li key={i}><span className="text-gray-500">{ago(s.ts)}</span> · {s.book} · <span className="text-gray-200">{s.direction} {s.symbol.replace('USDT', '')}</span> — {s.why}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
