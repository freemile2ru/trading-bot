import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../utils/api'
import type { OracleResponse } from '../types/oracle'

const REFRESH_INTERVAL = 1800 // seconds (30 minutes)

// `enabled` keeps this off until the Signal tab is actually opened. Each call runs the
// full pipeline including a Claude call, so firing it on page load — when All Signals
// is now the landing tab — would spend a request nobody asked for.
export function useOracle(symbol: string, enabled = true) {
  const [data, setData] = useState<OracleResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [countdown, setCountdown] = useState(REFRESH_INTERVAL)
  const countdownRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // An analysis takes 30–60 s. Only the latest request may write: a slow answer for the
  // previous symbol would otherwise land on top of the new one.
  const latest = useRef(0)
  const inFlight = useRef(false)

  const refresh = useCallback(async (force = false) => {
    if (!enabled) return
    if (inFlight.current && !force) return
    const req = ++latest.current
    inFlight.current = true
    setLoading(true)
    setError(null)
    try {
      const result = await api.oracle.analyze(symbol)
      if (req !== latest.current) return
      setData(result)
      setLastUpdated(new Date())
      setCountdown(REFRESH_INTERVAL)
    } catch (e) {
      if (req === latest.current) setError(e instanceof Error ? e.message : 'Failed to fetch oracle signal')
    } finally {
      if (req === latest.current) { inFlight.current = false; setLoading(false) }
    }
  }, [symbol, enabled])

  // Reset and fetch when the symbol changes, or the first time this is enabled
  useEffect(() => {
    if (!enabled) return
    setData(null)
    setError(null)
    setCountdown(REFRESH_INTERVAL)
    refresh(true)
  }, [symbol, enabled]) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-refresh interval
  useEffect(() => {
    if (!enabled) return
    const interval = setInterval(() => refresh(), REFRESH_INTERVAL * 1000)
    return () => clearInterval(interval)
  }, [refresh, enabled])

  // Countdown ticker
  useEffect(() => {
    if (countdownRef.current) clearInterval(countdownRef.current)
    countdownRef.current = setInterval(() => {
      setCountdown(s => (s <= 1 ? REFRESH_INTERVAL : s - 1))
    }, 1000)
    return () => {
      if (countdownRef.current) clearInterval(countdownRef.current)
    }
  }, [lastUpdated])

  return { data, loading, error, refresh: () => refresh(true), lastUpdated, countdown }
}
