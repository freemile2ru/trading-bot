import { useEffect, useState } from 'react'
import { WifiOff } from 'lucide-react'
import { api } from '../utils/api'
import type { WatchdogStatus } from '../types/ops'

const since = (ts: number) => {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60))
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`
}

/** Shown while the backend's watchdog says market data has stopped (backend/ops/watchdog.py). */
export function DataWatchBanner() {
  const [s, setS] = useState<WatchdogStatus | null>(null)
  useEffect(() => {
    let live = true
    const load = () => api.ops.watchdog().then(x => { if (live) setS(x) }).catch(() => { /* backend down: other errors show */ })
    load()
    const id = setInterval(load, 60_000)
    return () => { live = false; clearInterval(id) }
  }, [])
  if (!s || s.status !== 'stale') return null
  return (
    <div role="alert" className="flex items-center gap-2 px-4 sm:px-6 py-2 bg-amber-900/70 border-b border-amber-600 text-amber-100 text-sm">
      <WifiOff className="w-4 h-4 shrink-0" />
      <span className="font-semibold">Market data stopped{s.since ? ` ${since(s.since)} ago` : ''}.</span>
      <span className="text-amber-200/80 text-xs">{s.reason} — nothing is being traded or managed until it's back. Check the network.</span>
    </div>
  )
}
