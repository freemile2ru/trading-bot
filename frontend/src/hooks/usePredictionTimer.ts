import { useState, useEffect } from 'react'

const TF_MS: Record<string, number> = {
  '15m': 15 * 60_000,
  '30m': 30 * 60_000,
  '1h':  60 * 60_000,
  '4h':  4 * 60 * 60_000,
}

function fmt(ms: number): string {
  if (ms <= 0) return 'Expired'
  const h = Math.floor(ms / 3_600_000)
  const m = Math.floor((ms % 3_600_000) / 60_000)
  const s = Math.floor((ms % 60_000) / 1_000)
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m ${String(s).padStart(2, '0')}s`
  return `${m}m ${String(s).padStart(2, '0')}s`
}

export function usePredictionTimer(timeframe: string | undefined, lastUpdated: Date | null) {
  const duration = TF_MS[timeframe ?? '1h'] ?? TF_MS['1h']
  const [remaining, setRemaining] = useState(duration)

  useEffect(() => {
    if (!lastUpdated) { setRemaining(duration); return }
    const tick = () => setRemaining(Math.max(0, duration - (Date.now() - lastUpdated.getTime())))
    tick()
    const id = setInterval(tick, 1_000)
    return () => clearInterval(id)
  }, [lastUpdated, duration])

  const pct = Math.max(0, Math.min(100, (remaining / duration) * 100))
  const expired = remaining <= 0
  const label = expired ? 'Expired — refresh recommended' : `Valid for ${fmt(remaining)}`
  const color = expired ? 'gray' : pct > 50 ? 'green' : pct > 25 ? 'yellow' : 'red'

  return { remaining, pct, expired, label, color }
}
