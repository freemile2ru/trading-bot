import { useEffect, useRef, useState } from 'react'
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type IPriceLine, type ISeriesApi, type MouseEventParams, type Time, type UTCTimestamp,
} from 'lightweight-charts'
import type { Candle } from '../../types/trade'

export const C = {
  bg: '#0b0e11', panel: '#161a1e', grid: '#1e2329', text: '#848e9c', up: '#0ecb81', down: '#f6465d',
  // Binance's default moving-average colours, then the rest.
  ma7: '#f0b90b', ma25: '#eb40b5', ma99: '#b385f8', ema20: '#3dd6d0', ema50: '#fb8c00', bb: '#5d8cff', rsi: '#e2a8ff',
  draw: '#2f9bff',
}

export interface Indicators {
  ma7: boolean; ma25: boolean; ma99: boolean; ema20: boolean; ema50: boolean; bb: boolean; volume: boolean; rsi: boolean
}
export const DEFAULT_INDICATORS: Indicators = { ma7: true, ma25: true, ma99: true, ema20: false, ema50: false, bb: false, volume: true, rsi: true }
export interface Level { price: number; color: string; title: string; dashed?: boolean }

export type Tool = 'cursor' | 'trend' | 'hline'
export type Drawing =
  | { id: string; type: 'hline'; price: number }
  | { id: string; type: 'trend'; a: { time: number; price: number }; b: { time: number; price: number } }
/** Extra annotations a caller can paint: markers on the candles and price segments (entry → exit legs, stop/target lines). */
export type ChartMarker = { time: number; position: 'aboveBar' | 'belowBar' | 'inBar'; color: string; shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square'; text?: string }
export type ChartSegment = { a: { time: number; price: number }; b: { time: number; price: number }; color: string; dashed?: boolean; width?: 1 | 2 }
export type NewDrawing =
  | { type: 'hline'; price: number }
  | { type: 'trend'; a: { time: number; price: number }; b: { time: number; price: number } }

// ── Basic TA ─────────────────────────────────────────────────────────────────

function sma(values: number[], n: number): (number | null)[] {
  let sum = 0
  return values.map((v, i) => {
    sum += v
    if (i >= n) sum -= values[i - n]
    return i >= n - 1 ? sum / n : null
  })
}

function ema(values: number[], n: number): (number | null)[] {
  const k = 2 / (n + 1)
  const out: (number | null)[] = []
  let prev: number | null = null
  values.forEach((v, i) => {
    if (i < n - 1) { out.push(null); return }
    prev = prev === null ? values.slice(0, n).reduce((a, b) => a + b, 0) / n : v * k + prev * (1 - k)
    out.push(prev)
  })
  return out
}

function rsi(closes: number[], n = 14): (number | null)[] {
  const out: (number | null)[] = [null]
  let gain = 0, loss = 0
  for (let i = 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1]
    const g = Math.max(d, 0), l = Math.max(-d, 0)
    if (i <= n) {
      gain += g / n
      loss += l / n
      out.push(i === n ? 100 - 100 / (1 + gain / (loss || 1e-12)) : null)
    } else {
      gain = (gain * (n - 1) + g) / n
      loss = (loss * (n - 1) + l) / n
      out.push(100 - 100 / (1 + gain / (loss || 1e-12)))
    }
  }
  return out
}

function bollinger(closes: number[], n = 20, k = 2) {
  return closes.map((_, i) => {
    if (i < n - 1) return null
    const w = closes.slice(i - n + 1, i + 1)
    const m = w.reduce((a, b) => a + b, 0) / n
    const sd = Math.sqrt(w.reduce((a, b) => a + (b - m) ** 2, 0) / n)
    return { mid: m, up: m + k * sd, low: m - k * sd }
  })
}

/** Every line indicator's values for a candle list, keyed like the series. */
function compute(cs: Candle[]) {
  const closes = cs.map(c => c.close)
  const bb = bollinger(closes)
  return {
    ma7: sma(closes, 7), ma25: sma(closes, 25), ma99: sma(closes, 99), ema20: ema(closes, 20), ema50: ema(closes, 50),
    bbUp: bb.map(b => b?.up ?? null), bbMid: bb.map(b => b?.mid ?? null), bbLow: bb.map(b => b?.low ?? null),
    rsi: rsi(closes),
  }
}
type Computed = ReturnType<typeof compute>
const LINES: [keyof Computed, keyof Indicators][] = [
  ['ma7', 'ma7'], ['ma25', 'ma25'], ['ma99', 'ma99'], ['ema20', 'ema20'], ['ema50', 'ema50'],
  ['bbUp', 'bb'], ['bbMid', 'bb'], ['bbLow', 'bb'], ['rsi', 'rsi'],
]

const pts = (cs: Candle[], vals: (number | null)[]) =>
  cs.flatMap((c, i) => (vals[i] == null ? [] : [{ time: c.time as UTCTimestamp, value: vals[i] as number }]))
const fmt = (v: number | null | undefined, p: number) =>
  (v == null ? '—' : v.toLocaleString(undefined, { minimumFractionDigits: p, maximumFractionDigits: p }))

// ── Chart ────────────────────────────────────────────────────────────────────

export function TradeChart({ candles, live, indicators, levels, precision, tool, drawings, onDraw, markers = [], segments = [] }: {
  candles: Candle[]; live: Candle | null; indicators: Indicators; levels: Level[]; precision: number
  tool: Tool; drawings: Drawing[]; onDraw: (d: NewDrawing) => void; markers?: ChartMarker[]; segments?: ChartSegment[]
}) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const s = useRef<Record<string, ISeriesApi<any>>>({})
  const data = useRef<Candle[]>([])
  const vals = useRef<Computed | null>(null)
  const lines = useRef<IPriceLine[]>([])
  const drawn = useRef<{ price: IPriceLine[]; series: ISeriesApi<'Line'>[] }>({ price: [], series: [] })
  const painted = useRef<ISeriesApi<'Line'>[]>([])
  const markerApi = useRef<ReturnType<typeof createSeriesMarkers<Time>> | null>(null)
  const toolRef = useRef(tool)
  const pending = useRef<{ time: number; price: number } | null>(null)
  const [hasPending, setHasPending] = useState(false)
  const onDrawRef = useRef(onDraw)
  onDrawRef.current = onDraw
  const [legend, setLegend] = useState<{ c: Candle; i: number } | null>(null)

  // Build once.
  useEffect(() => {
    if (!box.current) return
    const ch = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: C.bg }, textColor: C.text, fontSize: 11,
                panes: { separatorColor: C.grid } },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: C.grid },
      timeScale: { borderColor: C.grid, timeVisible: true, secondsVisible: false },
    })
    s.current.candles = ch.addSeries(CandlestickSeries, {
      upColor: C.up, downColor: C.down, borderVisible: false, wickUpColor: C.up, wickDownColor: C.down,
    })
    s.current.volume = ch.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'vol',
                                                       lastValueVisible: false, priceLineVisible: false })
    ch.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0.02 } })
    const colors: Record<string, string> = { ma7: C.ma7, ma25: C.ma25, ma99: C.ma99, ema20: C.ema20, ema50: C.ema50,
                                             bbUp: C.bb, bbMid: C.bb, bbLow: C.bb, rsi: C.rsi }
    for (const [key] of LINES) {
      s.current[key] = ch.addSeries(LineSeries, { color: colors[key], lineWidth: 1, priceLineVisible: false,
        lastValueVisible: false, crosshairMarkerVisible: false }, key === 'rsi' ? 1 : 0)
    }
    s.current.rsi.createPriceLine({ price: 70, color: '#474d57', lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: '' })
    s.current.rsi.createPriceLine({ price: 30, color: '#474d57', lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: '' })
    s.current.preview = ch.addSeries(LineSeries, { color: C.draw, lineWidth: 1, lineStyle: LineStyle.Dashed,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
    ch.panes()[1]?.setStretchFactor(0.3)

    const timeAt = (p: MouseEventParams<Time>) =>
      (p.time as number | undefined) ?? (p.point ? (ch.timeScale().coordinateToTime(p.point.x) as number | null) : null)

    // Changing series data inside this handler makes the chart fire it again synchronously —
    // updating the trend-line preview here directly recursed until the page crashed. So the
    // preview is drawn on the next animation frame, and the legend only re-renders when the
    // hovered bar actually changes.
    let lastIdx = -2
    let frame = 0
    ch.subscribeCrosshairMove((p: MouseEventParams<Time>) => {
      const i = p.time != null ? data.current.findIndex(c => c.time === p.time) : -1
      if (i !== lastIdx) {
        lastIdx = i
        setLegend(i >= 0 ? { c: data.current[i], i } : null)
      }
      if (toolRef.current === 'trend' && pending.current && p.point) {
        const t = timeAt(p)
        const price = s.current.candles.coordinateToPrice(p.point.y)
        if (t != null && price != null && t !== pending.current.time) {
          const start = pending.current
          cancelAnimationFrame(frame)
          frame = requestAnimationFrame(() => {
            if (!pending.current || !s.current.preview) return
            const pair = [start, { time: t, price }].sort((a, b) => a.time - b.time)
            s.current.preview.setData(pair.map(x => ({ time: x.time as UTCTimestamp, value: x.price })))
          })
        }
      }
    })
    ch.subscribeClick((p: MouseEventParams<Time>) => {
      const tl = toolRef.current
      if (tl === 'cursor' || !p.point) return
      const price = s.current.candles.coordinateToPrice(p.point.y)
      if (price == null) return
      if (tl === 'hline') { onDrawRef.current({ type: 'hline', price }); return }
      const t = timeAt(p)
      if (t == null) return
      if (!pending.current) {
        pending.current = { time: t, price }
        setHasPending(true)
      } else if (t !== pending.current.time) {
        const [a, b] = [pending.current, { time: t, price }].sort((x, y) => x.time - y.time)
        pending.current = null
        setHasPending(false)
        s.current.preview.setData([])
        onDrawRef.current({ type: 'trend', a, b })
      }
    })
    chart.current = ch
    return () => { ch.remove(); chart.current = null; s.current = {}; markerApi.current = null; painted.current = [] }
  }, [])

  // Changing tool cancels a half-drawn trend line.
  useEffect(() => {
    toolRef.current = tool
    pending.current = null
    setHasPending(false)
    s.current.preview?.setData([])
  }, [tool])

  useEffect(() => {
    s.current.candles?.applyOptions({ priceFormat: { type: 'price', precision, minMove: 1 / 10 ** precision } })
  }, [precision])

  // Full history (symbol/interval change) or indicator toggles.
  useEffect(() => {
    data.current = [...candles]
    s.current.candles?.setData(candles.map(c => ({ ...c, time: c.time as UTCTimestamp })))
    s.current.volume?.setData(indicators.volume ? candles.map(c => ({
      time: c.time as UTCTimestamp, value: c.volume, color: c.close >= c.open ? '#0ecb8155' : '#f6465d55' })) : [])
    const v = compute(candles)
    vals.current = v
    for (const [key, flag] of LINES) s.current[key]?.setData(indicators[flag] ? pts(candles, v[key]) : [])
    chart.current?.panes()[1]?.setStretchFactor(indicators.rsi ? 0.3 : 0.0001)
  }, [candles, indicators])

  // Streaming candle: update the last bar (or append), then each indicator's tail.
  useEffect(() => {
    if (!live || !data.current.length) return
    const cs = data.current
    const last = cs[cs.length - 1]
    if (live.time < last.time) return
    if (live.time === last.time) cs[cs.length - 1] = live
    else cs.push(live)
    s.current.candles?.update({ ...live, time: live.time as UTCTimestamp })
    if (indicators.volume) {
      s.current.volume?.update({ time: live.time as UTCTimestamp, value: live.volume,
                                 color: live.close >= live.open ? '#0ecb8155' : '#f6465d55' })
    }
    const v = compute(cs.slice(-300))           // enough history for correct tail values (MA 99, RSI)
    const t = live.time as UTCTimestamp
    for (const [key, flag] of LINES) {
      const x = v[key][v[key].length - 1]
      if (indicators[flag] && x != null) s.current[key]?.update({ time: t, value: x })
    }
    vals.current = compute(cs)
  }, [live, indicators])

  // Position / order levels.
  useEffect(() => {
    const series = s.current.candles
    if (!series) return
    lines.current.forEach(l => series.removePriceLine(l))
    lines.current = levels.map(l => series.createPriceLine({
      price: l.price, color: l.color, lineWidth: 1, lineStyle: l.dashed ? LineStyle.Dashed : LineStyle.Solid,
      axisLabelVisible: true, title: l.title,
    }))
  }, [levels])

  // The user's drawings.
  useEffect(() => {
    const ch = chart.current, series = s.current.candles
    if (!ch || !series) return
    drawn.current.price.forEach(l => series.removePriceLine(l))
    drawn.current.series.forEach(x => ch.removeSeries(x))
    drawn.current = { price: [], series: [] }
    for (const d of drawings) {
      if (d.type === 'hline') {
        drawn.current.price.push(series.createPriceLine({ price: d.price, color: C.draw, lineWidth: 1,
          lineStyle: LineStyle.Solid, axisLabelVisible: true, title: '' }))
      } else {
        const ln = ch.addSeries(LineSeries, { color: C.draw, lineWidth: 2, priceLineVisible: false,
          lastValueVisible: false, crosshairMarkerVisible: false })
        ln.setData([{ time: d.a.time as UTCTimestamp, value: d.a.price }, { time: d.b.time as UTCTimestamp, value: d.b.price }])
        drawn.current.series.push(ln)
      }
    }
  }, [drawings])

  // Caller's annotations: markers on the candles, and price segments.
  useEffect(() => {
    const series = s.current.candles
    if (!series) return
    const ms = [...markers].sort((x, y) => x.time - y.time).map(m => ({ ...m, time: m.time as UTCTimestamp }))
    if (!markerApi.current) markerApi.current = createSeriesMarkers(series, ms)
    else markerApi.current.setMarkers(ms)
  }, [markers, candles])
  useEffect(() => {
    const ch = chart.current
    if (!ch) return
    painted.current.forEach(x => ch.removeSeries(x))
    painted.current = []
    for (const sg of segments) {
      if (sg.b.time <= sg.a.time) continue
      const ln = ch.addSeries(LineSeries, { color: sg.color, lineWidth: sg.width ?? 1, lineStyle: sg.dashed ? LineStyle.Dotted : LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
      ln.setData([{ time: sg.a.time as UTCTimestamp, value: sg.a.price }, { time: sg.b.time as UTCTimestamp, value: sg.b.price }])
      painted.current.push(ln)
    }
  }, [segments, candles])

  // Legend: the hovered bar, or the latest one.
  const cs = data.current
  const idx = legend?.i ?? cs.length - 1
  const bar = legend?.c ?? cs[cs.length - 1]
  const v = vals.current
  const chg = bar ? (bar.close - bar.open) / bar.open * 100 : 0
  const tone = chg >= 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]'
  const items: [string, keyof Indicators, keyof Computed, string][] = [
    ['MA(7)', 'ma7', 'ma7', C.ma7], ['MA(25)', 'ma25', 'ma25', C.ma25], ['MA(99)', 'ma99', 'ma99', C.ma99],
    ['EMA(20)', 'ema20', 'ema20', C.ema20], ['EMA(50)', 'ema50', 'ema50', C.ema50],
  ]

  return (
    <div className="relative w-full h-full" style={{ cursor: tool === 'cursor' ? 'default' : 'crosshair' }}>
      <div ref={box} className="w-full h-full" />
      {bar && (
        <div className="pointer-events-none absolute left-2 top-1 right-16 z-10 text-[10px] sm:text-[11px] leading-4 tabular-nums space-y-0.5">
          <div className="flex flex-wrap gap-x-2 text-[#848e9c]">
            {(['O', 'H', 'L', 'C'] as const).map(k => (
              <span key={k}>{k} <b className={tone}>{fmt(bar[{ O: 'open', H: 'high', L: 'low', C: 'close' }[k] as 'open'], precision)}</b></span>
            ))}
            <span className={tone}>{chg >= 0 ? '+' : ''}{chg.toFixed(2)}%</span>
          </div>
          <div className="flex flex-wrap gap-x-2">
            {items.filter(([, flag]) => indicators[flag]).map(([label, , key, color]) => (
              <span key={label} style={{ color }}>{label} {fmt(v?.[key][idx], precision)}</span>
            ))}
            {indicators.rsi && <span style={{ color: C.rsi }}>RSI(14) {fmt(v?.rsi[idx], 2)}</span>}
          </div>
        </div>
      )}
      {tool !== 'cursor' && (
        <div className="pointer-events-none absolute bottom-8 left-1/2 -translate-x-1/2 z-10 text-[11px] px-2 py-1 rounded bg-black/70 text-[#eaecef] whitespace-nowrap">
          {tool === 'hline' ? 'Click to place a horizontal line'
            : hasPending ? 'Click the second point' : 'Click the first point of the trend line'} · Esc to cancel
        </div>
      )}
    </div>
  )
}
