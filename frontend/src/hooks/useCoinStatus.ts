import { useEffect, useState } from 'react'
import type { CoinStatus } from '../types/signals'

/** Signal tally + open positions per coin, polled — positions change on their own schedule. */
export function useCoinStatus(intervalMs = 15000) {
  const [coins, setCoins] = useState<Record<string, CoinStatus>>({})
  useEffect(() => {
    let alive = true
    const load = () => fetch('/api/signals/coins')
      .then(r => (r.ok ? r.json() : null))
      .then(d => { if (alive && d) setCoins(d) })
      .catch(() => {})
    load()
    const id = setInterval(load, intervalMs)
    return () => { alive = false; clearInterval(id) }
  }, [intervalMs])
  return coins
}
