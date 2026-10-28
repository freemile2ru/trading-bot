import { useCallback, useEffect, useState } from 'react'

const KEY = 'oracle.includeRetired'
const EVENT = 'oracle:include-retired'

function read(): boolean {
  try { return localStorage.getItem(KEY) === '1' } catch { return false }
}

/** Whether retired strategies' trades are included in the Trades and Portfolio views. Off by
 *  default; one setting shared by both pages and remembered in this browser. */
export function useIncludeRetired(): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState(read)
  useEffect(() => {
    const sync = () => setValue(read())
    window.addEventListener(EVENT, sync)
    window.addEventListener('storage', sync)
    return () => { window.removeEventListener(EVENT, sync); window.removeEventListener('storage', sync) }
  }, [])
  const set = useCallback((v: boolean) => {
    try { localStorage.setItem(KEY, v ? '1' : '0') } catch { /* private mode: this page only */ }
    setValue(v)
    window.dispatchEvent(new Event(EVENT))
  }, [])
  return [value, set]
}
