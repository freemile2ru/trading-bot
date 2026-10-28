import { useEffect, useRef, useState, useCallback } from 'react'
import type { OracleResult, SignalPackage } from '../types/oracle'
import { api } from '../utils/api'

const CONFIDENCE_MIN = 70   // >= 75% confidence
const SCORE_MIN = 0.45      // |composite_score| >= 0.45

function signalKey(oracle: OracleResult): string {
  const r = oracle.trade_recommendation
  return `${r.symbol}:${r.action}:${r.entry_price}:${r.confidence}`
}

function isStrongSignal(oracle: OracleResult | null, signal: SignalPackage | null): boolean {
  if (!oracle || !signal || oracle.error) return false
  const rec = oracle.trade_recommendation
  //return true;
  return (
    rec.action !== 'HOLD' &&
    rec.confidence >= CONFIDENCE_MIN &&
    Math.abs(signal.composite_score) >= SCORE_MIN
  )
}

class AlarmPlayer {
  private ctx: AudioContext
  private running = true
  private timer: number | null = null

  constructor() {
    const Ctx = (window.AudioContext ??
      (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext)!
    this.ctx = new Ctx()
    if (this.ctx.state === 'suspended') this.ctx.resume()
    this.play()
  }

  private tone(freq: number, startAt: number, dur: number) {
    const osc = this.ctx.createOscillator()
    const gain = this.ctx.createGain()
    osc.connect(gain)
    gain.connect(this.ctx.destination)
    osc.type = 'sine'
    osc.frequency.setValueAtTime(freq, startAt)
    gain.gain.setValueAtTime(0, startAt)
    gain.gain.linearRampToValueAtTime(0.28, startAt + 0.015)
    gain.gain.setValueAtTime(0.28, startAt + dur - 0.02)
    gain.gain.linearRampToValueAtTime(0, startAt + dur)
    osc.start(startAt)
    osc.stop(startAt + dur)
  }

  private play() {
    if (!this.running || this.ctx.state === 'closed') return
    const t = this.ctx.currentTime
    // Two-tone pattern: 880 Hz then 1100 Hz, repeat every 950ms
    try {
      this.tone(880, t + 0.0, 0.13)
      this.tone(1100, t + 0.22, 0.13)
    } catch { /* audio may not be ready yet */ }
    this.timer = window.setTimeout(() => this.play(), 950)
  }

  stop() {
    this.running = false
    if (this.timer !== null) clearTimeout(this.timer)
    try { this.ctx.close() } catch { /* ignore */ }
  }
}

export function useAlarm(
  oracle: OracleResult | null,
  signal: SignalPackage | null,
  symbol: string,
  onTradeOpened?: () => void,
) {
  const [isAlarming, setIsAlarming] = useState(false)
  const [autoTradeMsg, setAutoTradeMsg] = useState<string | null>(null)
  const dismissedKeyRef = useRef<string | null>(null)
  const notifiedKeyRef = useRef<string | null>(null)
  const playerRef = useRef<AlarmPlayer | null>(null)

  const dismiss = useCallback(() => {
    if (oracle) dismissedKeyRef.current = signalKey(oracle)
    setIsAlarming(false)
    playerRef.current?.stop()
    playerRef.current = null
  }, [oracle])

  useEffect(() => {
    if (!oracle || !signal) return
    const key = signalKey(oracle)
    const strong = isStrongSignal(oracle, signal)

    if (strong && key !== dismissedKeyRef.current) {
      setIsAlarming(true)
      if (!playerRef.current) {
        try { playerRef.current = new AlarmPlayer() } catch { /* Web Audio unavailable */ }
      }

      // Once per unique signal: notify + auto paper trade
      if (key !== notifiedKeyRef.current) {
        notifiedKeyRef.current = key
        api.notify.signal(oracle, signal).catch(() => {})

        // Auto-execute paper trade (backend checks confidence >= 60 internally)
        api.oracle.executeAndTrade(symbol).then(result => {
          setAutoTradeMsg(result.message)
          if (result.trade) onTradeOpened?.()
        }).catch(() => {})
      }
    } else if (!strong) {
      if (playerRef.current) {
        playerRef.current.stop()
        playerRef.current = null
        setIsAlarming(false)
        dismissedKeyRef.current = null
      }
    }
  }, [oracle, signal, symbol]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => { playerRef.current?.stop() }, [])

  return { isAlarming, dismiss, autoTradeMsg }
}
