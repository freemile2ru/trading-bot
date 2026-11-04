import { useEffect, useState } from 'react'

/** The time where the viewer is (the browser's zone), ticking; UTC and New York on hover — the books keep UTC slots and
 *  the mentor's system trades the New York session, so both are one glance away. */
function fmt(d: Date, timeZone?: string, withZone = true): string {
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone, timeZoneName: withZone ? 'short' : undefined })
}

export default function Clock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
  const title = `${zone}\nUTC ${fmt(now, 'UTC', false)}\nNew York ${fmt(now, 'America/New_York')}`
  return (
    <div className="text-right tabular-nums leading-tight cursor-default" title={title}>
      <div className="text-sm font-semibold text-gray-200">{fmt(now)}</div>
      <div className="hidden sm:block text-[10px] text-gray-500">
        {now.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })} · UTC {fmt(now, 'UTC', false).slice(0, 5)} · NY {fmt(now, 'America/New_York', false).slice(0, 5)}
      </div>
    </div>
  )
}
