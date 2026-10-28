import { useCallback, useEffect, useState } from 'react'

export type Ledger = 'both' | 'paper' | 'live'
const KEY = 'oracle.ledger'
const EVENT = 'oracle:ledger'

function read(): Ledger {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'paper' || v === 'live' ? v : 'both'
  } catch { return 'both' }
}

/** Which ledgers the decision views count: each system's paper wallet, its live ledger (mirroring
 *  the Binance account), or both. One setting for the Lab, History and Books pages. */
export function useLedger(): [Ledger, (v: Ledger) => void] {
  const [value, setValue] = useState<Ledger>(read)
  useEffect(() => {
    const sync = () => setValue(read())
    window.addEventListener(EVENT, sync)
    window.addEventListener('storage', sync)
    return () => { window.removeEventListener(EVENT, sync); window.removeEventListener('storage', sync) }
  }, [])
  const set = useCallback((v: Ledger) => {
    try { localStorage.setItem(KEY, v) } catch { /* private mode: this page only */ }
    setValue(v)
    window.dispatchEvent(new Event(EVENT))
  }, [])
  return [value, set]
}
