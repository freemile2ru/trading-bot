import { useEffect, useState } from 'react'
import { CalendarClock } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import type { CalendarStatus } from '../types/ops'

const fmtCentral = (ts: number) =>
  new Date(ts * 1000).toLocaleString('en-US', { timeZone: 'America/Chicago', weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' })

const until = (ts: number) => {
  const m = Math.round((ts * 1000 - Date.now()) / 60000)
  if (m <= 0) return 'now'
  if (m < 60) return `${m}m`
  if (m < 48 * 60) return `${Math.floor(m / 60)}h ${m % 60}m`
  return `${Math.round(m / 1440)}d`
}

/** The next scheduled release (FOMC, CPI, a stock perp's earnings) and whether an entry blackout is on. */
export function CalendarChip() {
  const [c, setC] = useState<CalendarStatus | null>(null)
  useEffect(() => {
    let live = true
    const load = () => api.ops.calendar().then(x => { if (live) setC(x) }).catch(() => { /* chip just hides */ })
    load()
    const id = setInterval(load, 60_000)
    return () => { live = false; clearInterval(id) }
  }, [])
  if (!c?.upcoming.length) return null
  const nxt = c.upcoming[0]
  const list = c.upcoming.slice(0, 8).map(e => `${e.label} — ${fmtCentral(e.ts)}`).join('\n')
  return (
    <span className={clsx('hidden lg:inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-1 rounded border whitespace-nowrap',
      c.blackout ? 'bg-rose-950/60 text-rose-300 border-rose-800/60' : 'bg-gray-800 text-gray-300 border-gray-700')}
      title={`${c.blackout ? c.blackout + '\n\n' : ''}Upcoming:\n${list}\n\nRules: no new short-term crypto entries from ${c.rules.macro_before_min} min before to ${c.rules.macro_after_min} min after FOMC/CPI; no new stock-trend entries within ${c.rules.earnings_days} days of that stock's earnings.`}>
      <CalendarClock className="w-3 h-3" /> {c.blackout ? 'Blackout: ' : ''}{nxt.label} in {until(nxt.ts)}
    </span>
  )
}
