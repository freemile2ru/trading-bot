import { useEffect, useState } from 'react'
import clsx from 'clsx'
import { api } from '../../utils/api'
import type { SignalHorizon, SignalScores } from '../../types/lab'

type H = '4h' | '24h' | '72h'
const tone = (v: number | null | undefined) => (v == null || v === 0 ? 'text-gray-300' : v > 0 ? 'text-emerald-400' : 'text-red-400')
const signed = (v: number | null | undefined, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}`)

function Buckets({ h, cost }: { h: SignalHorizon; cost: number }) {
  const max = Math.max(cost * 1.5, ...h.buckets.map(b => Math.abs(b.avg_bps)))
  return (
    <div className="space-y-1.5">
      {h.buckets.map(b => (
        <div key={b.bucket} className="grid grid-cols-[6.5rem_minmax(0,1fr)_7rem] items-center gap-2 text-xs"
             title={`${b.n.toLocaleString()} readings · right ${b.hit_pct}% of the time · ${signed(b.after_costs_bps)} bps after costs`}>
          <span className="text-gray-300">{b.bucket}</span>
          <div className="relative h-4">
            <div className="absolute inset-y-0 left-1/2 w-px bg-gray-600" />
            <div className="absolute -top-0.5 -bottom-0.5 border-l border-dashed border-amber-400/80" style={{ left: `${50 + (cost / max) * 50}%` }} />
            <div className={clsx('absolute inset-y-0.5 rounded-sm', b.avg_bps >= 0 ? 'bg-emerald-500' : 'bg-red-500')}
                 style={b.avg_bps >= 0 ? { left: '50%', width: `${Math.min(1, b.avg_bps / max) * 50}%` } : { right: '50%', width: `${Math.min(1, -b.avg_bps / max) * 50}%` }} />
          </div>
          <span className={clsx('tabular-nums text-right', tone(b.avg_bps))}>{signed(b.avg_bps)} bps <span className="text-gray-500">· {b.n}</span></span>
        </div>
      ))}
      <p className="text-[10px] text-gray-500">Move in the conviction's direction. Amber dashed line: the ~{cost} bps round-trip cost a trade must beat.</p>
    </div>
  )
}

/** Lab section: labelled scan readings — does any input predict the next move? */
export function SignalScoresPanel() {
  const [s, setS] = useState<SignalScores | null>(null)
  const [h, setH] = useState<H>('24h')
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { api.lab.signalScores().then(setS).catch(e => setErr(e instanceof Error ? e.message : 'Not available')) }, [])
  if (err) return <div className="card text-xs text-gray-500">Signal scoring: {err}</div>
  if (!s) return <div className="card h-40 animate-pulse" />
  const d = s.horizons[h]
  const collecting = (d?.days ?? 0) < s.min_days
  return (
    <section className="card space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0">
          <p className="text-xs text-gray-400">
            Every scanner reading (conviction, scores, positioning, fundamentals) labelled with what the coin did next.
            {' '}{s.readings.toLocaleString()} readings labelled over {s.days ?? 0} days; labels are added every hour.
          </p>
          {collecting && (
            <p className="text-xs text-amber-300 mt-1">
              Collecting — {d?.days ?? 0} of {s.min_days} days at this horizon. With this few days one market swing dominates every number,
              so they're shown greyed and without t-stats. Nothing here should be acted on yet.
            </p>
          )}
        </div>
        <div className="ml-auto inline-flex rounded-lg bg-gray-800/70 p-0.5">
          {(['4h', '24h', '72h'] as H[]).map(k => (
            <button key={k} onClick={() => setH(k)} disabled={!s.horizons[k]}
              className={clsx('px-2.5 py-1 rounded-md text-xs font-semibold disabled:opacity-30', h === k ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-gray-200')}>
              next {k}
            </button>
          ))}
        </div>
      </div>
      {!d ? <p className="text-xs text-gray-500">No readings old enough for this horizon yet.</p> : (
        <div className={clsx('grid grid-cols-1 xl:grid-cols-2 gap-6', collecting && 'opacity-60')}>
          <div className="space-y-4">
            <div>
              <div className="text-xs font-semibold text-gray-200 mb-2">Does stronger conviction mean a better move?</div>
              <Buckets h={d} cost={s.round_trip_bps} />
            </div>
            {d.claude && (
              <div className="text-xs text-gray-300">
                Coins Claude fired a signal on: <span className={tone(d.claude.fired_avg_bps)}>{signed(d.claude.fired_avg_bps)} bps</span> ({d.claude.fired_n} readings)
                {' '}vs all others <span className={tone(d.claude.other_avg_bps)}>{signed(d.claude.other_avg_bps)} bps</span> ({d.claude.other_n.toLocaleString()}).
              </div>
            )}
            {!!d.by_regime.length && (
              <div>
                <div className="text-xs font-semibold text-gray-200 mb-1">By market regime</div>
                <table className="w-full text-xs">
                  <tbody>
                    {d.by_regime.map(r => (
                      <tr key={r.regime} className="border-t border-gray-800/60">
                        <td className="py-1 text-gray-300">{r.regime}</td>
                        <td className="py-1 text-right tabular-nums text-gray-400">{r.n}</td>
                        <td className="py-1 text-right tabular-nums text-gray-400">{r.hit_pct}% right</td>
                        <td className={clsx('py-1 text-right tabular-nums', tone(r.avg_bps))}>{signed(r.avg_bps)} bps</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          <div>
            <div className="text-xs font-semibold text-gray-200">Which inputs predict the move? (information coefficient)</div>
            <p className="text-[10px] text-gray-500 mb-2">
              Rank correlation of each input with the next move, averaged per day; "vs BTC" uses the move minus BTC's.
              Worth attention from |IC| ≈ 0.03 with |t| above 2 — t appears after {s.min_days} days.
            </p>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-[10px] uppercase tracking-wider text-gray-500">
                  <th className="text-left font-semibold pb-1">Input</th><th className="text-right font-semibold pb-1">IC</th>
                  <th className="text-right font-semibold pb-1">t</th><th className="text-right font-semibold pb-1">IC vs BTC</th>
                  <th className="text-right font-semibold pb-1">Days</th>
                </tr>
              </thead>
              <tbody>
                {d.inputs.map(i => (
                  <tr key={i.input} className="border-t border-gray-800/60">
                    <td className="py-1 text-gray-300 font-mono text-[11px]">{i.input}</td>
                    <td className={clsx('py-1 text-right tabular-nums', tone(i.ic))}>{signed(i.ic, 3)}</td>
                    <td className="py-1 text-right tabular-nums text-gray-300">{i.t == null ? '—' : i.t.toFixed(1)}</td>
                    <td className={clsx('py-1 text-right tabular-nums', tone(i.ic_vs_btc))}>{signed(i.ic_vs_btc, 3)}</td>
                    <td className="py-1 text-right tabular-nums text-gray-500">{i.days}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  )
}
