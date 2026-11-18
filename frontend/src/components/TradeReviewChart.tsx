import { useEffect, useMemo, useState } from 'react'
import { api } from '../utils/api'
import type { Candle } from '../types/trade'
import { TradeChart, C, DEFAULT_INDICATORS, type ChartSegment, type Indicators } from './trade/TradeChart'
import { focusAnnotations, readTrade } from './trade/FocusedPosition'

type Rec = Record<string, any>
const REVIEW_INDICATORS: Indicators = { ...DEFAULT_INDICATORS, ma7: false, ma25: false, ma99: false, rsi: false }

/** A closed (or open) trade replayed on its own candles: the window it was held, padded either side, with the entry,
 *  the stop as it moved, the target, the exit and the best price painted the way the terminal paints a followed
 *  position (2026-11-18, the user: "clicking on closed trades will create a pop up and plot the trade snapshot data"). */
export function TradeReviewChart({ t }: { t: Rec }) {
  const trade = useMemo(() => readTrade(t), [t])
  const [data, setData] = useState<{ candles: Candle[]; intervalS: number; interval: string } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const start = trade.openedTs, end = trade.closedTs ?? Math.floor(Date.now() / 1000)

  useEffect(() => {
    if (!trade.symbol || start == null || end == null || end <= start) return
    let stale = false
    setData(null); setErr(null)
    api.market.ohlcvRange(trade.symbol, start, end)
      .then(x => { if (!stale) setData({ candles: x.candles as Candle[], intervalS: x.interval_s, interval: x.interval }) })
      .catch(e => { if (!stale) setErr(e instanceof Error ? e.message : 'No candles for this symbol') })
    return () => { stale = true }
  }, [trade.symbol, start, end])

  // The terminal's annotations, plus the stop as it stepped while the trade was open (the record's adjustments).
  const paint = useMemo(() => {
    const base = focusAnnotations(data ? trade : null, data?.intervalS ?? 60)
    if (!data || !start) return base
    const bar = (x: number) => Math.floor(x / data.intervalS) * data.intervalS
    const steps: { ts: number; stop: number }[] = (Array.isArray(t.adjustments) ? t.adjustments : [])
      .filter((a: Rec) => typeof a?.new_stop === 'number' && a.ts)
      .map((a: Rec) => ({ ts: typeof a.ts === 'number' ? (a.ts > 1e12 ? a.ts / 1000 : a.ts) : Date.parse(a.ts) / 1000, stop: a.new_stop as number }))
      .filter((a: { ts: number }) => Number.isFinite(a.ts))
      .sort((a: { ts: number }, b: { ts: number }) => a.ts - b.ts)
    const segments: ChartSegment[] = [...base.segments]
    if (steps.length && trade.originalStop != null) {
      let from = start, stop = trade.originalStop
      for (const st of steps) {
        segments.push({ a: { time: bar(from), price: stop }, b: { time: bar(st.ts), price: stop }, color: C.down, dashed: true })
        from = st.ts; stop = st.stop
      }
      segments.push({ a: { time: bar(from), price: stop }, b: { time: bar(end), price: stop }, color: C.down, dashed: true })
    }
    return { ...base, segments }
  }, [data, trade, t.adjustments, start, end])

  const precision = trade.entry != null ? (trade.entry >= 1000 ? 1 : trade.entry >= 10 ? 2 : trade.entry >= 0.1 ? 4 : 6) : 4

  if (start == null) return null
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1.5">
        <div className="font-semibold text-gray-300 text-xs">On the chart</div>
        <div className="text-[10px] text-gray-500">{data ? `${data.interval} candles · entry, stop as it moved, target, exit` : err ? '' : 'loading candles…'}</div>
      </div>
      {err && <p className="text-xs text-gray-500">No chart: {err}</p>}
      {!err && (
        <div className="h-[65vh] rounded-lg overflow-hidden border border-gray-800" style={{ background: C.bg }}>
          {data && (
            <TradeChart candles={data.candles} live={null} indicators={REVIEW_INDICATORS} levels={paint.levels} precision={precision}
              tool="cursor" drawings={[]} onDraw={() => undefined} markers={paint.markers} segments={paint.segments} />
          )}
        </div>
      )}
    </div>
  )
}
