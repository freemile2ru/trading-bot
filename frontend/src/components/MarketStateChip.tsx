import { useEffect, useState } from 'react'
import { Activity } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import type { MarketState } from '../types/ops'

const STYLE = {
  quiet: ['Quiet', 'bg-sky-950/60 text-sky-300 border-sky-800/60'],
  normal: ['Normal', 'bg-gray-800 text-gray-300 border-gray-700'],
  volatile: ['Volatile', 'bg-amber-950/60 text-amber-300 border-amber-800/60'],
} as const

const NOTE = {
  quiet: 'Expect few or no trades: on a year of data, no quiet-market strategy we tested (range fades, squeeze breakouts, 24h reversal) beat its costs — tight ranges mean tight stops, and fees take ~0.17R a trade. Sitting out is the strategy.',
  normal: 'Normal conditions — the books trade their usual rules.',
  volatile: 'Big moves — the open-risk caps, direction gate and stops matter most now.',
}

/** BTC's activity right now (24h range vs its 30-day median) — why the books are busy or quiet. */
export function MarketStateChip() {
  const [m, setM] = useState<MarketState | null>(null)
  useEffect(() => {
    let live = true
    const load = () => api.ops.market().then(x => { if (live) setM(x) }).catch(() => { /* chip just hides */ })
    load()
    const id = setInterval(load, 300_000)
    return () => { live = false; clearInterval(id) }
  }, [])
  if (!m?.activity) return null
  const [label, cls] = STYLE[m.activity]
  return (
    <span className={clsx('hidden md:inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-1 rounded border whitespace-nowrap', cls)}
      title={`BTC moved ${m.btc_range_24h_pct}% in 24h vs a usual ${m.btc_range_median_pct}% (${m.activity_ratio}×) · ${m.label ?? ''}\n\n${NOTE[m.activity]}`}>
      <Activity className="w-3 h-3" /> Market: {label}
      <span className="font-normal opacity-75">· BTC {m.btc_range_24h_pct}% / {m.btc_range_median_pct}% usual</span>
    </span>
  )
}
