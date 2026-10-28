import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../utils/api'
import type {
  AutoSummary,
  FiredSignal,
  ScanRow,
  ScanSnapshot,
  WatchlistChange,
  WatchlistEntry,
} from '../types/signals'

type Connection = 'connecting' | 'live' | 'offline'

function sortRows(rows: ScanRow[]): ScanRow[] {
  return [...rows].sort((a, b) => Math.abs(b.conviction) - Math.abs(a.conviction))
}

export function useSignalStream() {
  const [rows, setRows] = useState<ScanRow[]>([])
  const [fired, setFired] = useState<FiredSignal[]>([])
  const [watchlist, setWatchlist] = useState<WatchlistEntry[]>([])
  const [meta, setMeta] = useState<Omit<ScanSnapshot, 'rows' | 'fired' | 'watchlist'> | null>(null)
  const [connection, setConnection] = useState<Connection>('connecting')
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [auto, setAuto] = useState<AutoSummary | null>(null)

  // Latest fired signal, surfaced so App can drive the existing alarm from it.
  const [latestSignal, setLatestSignal] = useState<FiredSignal | null>(null)

  const sourceRef = useRef<EventSource | null>(null)

  const applySnapshot = useCallback((snap: ScanSnapshot) => {
    setRows(sortRows(snap.rows ?? []))
    setFired(snap.fired ?? [])
    setWatchlist(snap.watchlist ?? [])
    const { rows: _r, fired: _f, watchlist: _w, ...rest } = snap
    setMeta(rest)
  }, [])

  // Seed from REST so the tab renders immediately rather than waiting for a sweep.
  useEffect(() => {
    let cancelled = false
    api.signals
      .list()
      .then(snap => { if (!cancelled) applySnapshot(snap) })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load signals') })
    api.auto.summary().then(a => { if (!cancelled) setAuto(a) }).catch(() => {})
    return () => { cancelled = true }
  }, [applySnapshot])

  useEffect(() => {
    const source = new EventSource('/api/signals/stream')
    sourceRef.current = source

    source.onopen = () => { setConnection('live'); setError(null) }
    // EventSource reconnects on its own; surface the gap without tearing anything down.
    source.onerror = () => setConnection('offline')

    source.addEventListener('scan', e => {
      setConnection('live')
      applySnapshot(JSON.parse((e as MessageEvent).data))
    })

    // Watchlist tier refreshes only a few symbols — merge them into the existing table.
    source.addEventListener('watch', e => {
      setConnection('live')
      const payload = JSON.parse((e as MessageEvent).data) as {
        rows: ScanRow[]
        watchlist: WatchlistEntry[]
        updated_at: number
      }
      setRows(prev => {
        const merged = new Map(prev.map(r => [r.symbol, r]))
        payload.rows.forEach(r => merged.set(r.symbol, r))
        return sortRows([...merged.values()])
      })
      setWatchlist(payload.watchlist)
      setMeta(m => (m ? { ...m, updated_at: payload.updated_at } : m))
    })

    source.addEventListener('signal', e => {
      const signal = JSON.parse((e as MessageEvent).data) as FiredSignal
      setFired(prev => [signal, ...prev.filter(f => f.fired_at !== signal.fired_at)].slice(0, 20))
      setLatestSignal(signal)
    })

    source.addEventListener('watchlist', e => {
      const change = JSON.parse((e as MessageEvent).data) as WatchlistChange
      setWatchlist(change.entries)
    })

    // The autotrader publishes its full summary after every sweep and on each open/close.
    source.addEventListener('auto', e => {
      setAuto(JSON.parse((e as MessageEvent).data) as AutoSummary)
    })

    return () => {
      source.close()
      sourceRef.current = null
    }
  }, [applySnapshot])

  const rescan = useCallback(async () => {
    setScanning(true)
    setError(null)
    try {
      applySnapshot(await api.signals.scan())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Scan failed')
    } finally {
      setScanning(false)
    }
  }, [applySnapshot])

  const pin = useCallback(async (symbol: string) => {
    try {
      await api.signals.pin(symbol)
      setWatchlist((await api.signals.watchlist()).entries)
    } catch { /* the stream will correct this on the next sweep */ }
  }, [])

  const unpin = useCallback(async (symbol: string) => {
    try {
      await api.signals.unpin(symbol)
      setWatchlist((await api.signals.watchlist()).entries)
    } catch { /* the stream will correct this on the next sweep */ }
  }, [])

  // Actions on real positions must say when they fail — a silent catch made a refused
  // close look like a button that did nothing.
  const failed = (what: string) => (e: unknown) => setError(`${what} failed: ${e instanceof Error ? e.message : String(e)}`)

  const setAutoPaused = useCallback(async (paused: boolean) => {
    try { setAuto(await api.auto.pause(paused)) } catch (e) { failed(paused ? 'Pausing' : 'Resuming')(e) }
  }, [])

  const closeAutoTrade = useCallback(async (id: string) => {
    try {
      setError(null)
      await api.auto.close(id)
      setAuto(await api.auto.summary())
    } catch (e) { failed('Closing the position')(e) }
  }, [])

  const cancelAutoOrder = useCallback(async (id: string) => {
    try {
      setError(null)
      await api.auto.cancelOrder(id)
      setAuto(await api.auto.summary())
    } catch (e) { failed('Cancelling the order')(e) }
  }, [])

  return {
    rows, fired, watchlist, meta, connection, scanning, error,
    latestSignal, rescan, pin, unpin,
    auto, setAutoPaused, closeAutoTrade, cancelAutoOrder,
  }
}
