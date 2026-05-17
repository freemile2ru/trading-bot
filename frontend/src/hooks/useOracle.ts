import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../utils/api'
import type { OracleResponse } from '../types/oracle'

const REFRESH_INTERVAL = 60 // seconds

export function useOracle(symbol: string) {
  const [data, setData] = useState<OracleResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [countdown, setCountdown] = useState(REFRESH_INTERVAL)
  const countdownRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const refresh = useCallback(async (force = false) => {
    if (loading && !force) return
    setLoading(true)
    setError(null)
    try {
      const result = await api.oracle.analyze(symbol)
      setData(result)
      setLastUpdated(new Date())
      setCountdown(REFRESH_INTERVAL)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch oracle signal')
    } finally {
      setLoading(false)
    }
  }, [symbol]) // eslint-disable-line react-hooks/exhaustive-deps

  // Reset and re-fetch when symbol changes
  useEffect(() => {
    setData(null)
    setError(null)
    setCountdown(REFRESH_INTERVAL)
    refresh(true)
  }, [symbol]) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-refresh interval
  useEffect(() => {
    const interval = setInterval(() => refresh(), REFRESH_INTERVAL * 1000)
    return () => clearInterval(interval)
  }, [refresh])

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
