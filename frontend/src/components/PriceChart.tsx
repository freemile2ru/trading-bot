import { useEffect, useState } from 'react'
import {
  ComposedChart, Area, Bar, XAxis, YAxis, Tooltip,
  ReferenceLine, ResponsiveContainer, CartesianGrid,
} from 'recharts'
import { api } from '../utils/api'
import type { Candle, TradeRecommendation, TimeframeSignal } from '../types/oracle'
import clsx from 'clsx'

const INTERVALS = ['15m', '30m', '1h', '4h'] as const
type Interval = typeof INTERVALS[number]

interface Props {
  symbol: string
  recommendation?: TradeRecommendation
  timeframeSignals?: Record<string, TimeframeSignal>
}

interface ChartPoint {
  time: string
  price: number
  high: number
  low: number
  volume: number
  volColor: string
}

function formatTime(ms: number, interval: Interval): string {
  const d = new Date(ms)
  if (interval === '4h') return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function toChartData(candles: Candle[], interval: Interval): ChartPoint[] {
  return candles.map((c, i) => ({
    time: formatTime(c.time, interval),
    price: c.close,
    high: c.high,
    low: c.low,
    volume: c.volume,
    volColor: i > 0 && c.close >= candles[i - 1].close ? '#10b981' : '#ef4444',
  }))
}

function PriceTooltip({ active, payload, label }: { active?: boolean; payload?: { value: number; name: string }[]; label?: string }) {
  if (!active || !payload?.length) return null
  const price = payload.find(p => p.name === 'price')?.value
  const vol = payload.find(p => p.name === 'volume')?.value
  return (
    <div className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl">
      <div className="text-gray-400 mb-1">{label}</div>
      {price != null && <div className="text-white font-bold">${price.toLocaleString(undefined, { maximumFractionDigits: 4 })}</div>}
      {vol != null && <div className="text-gray-500">Vol: {(vol / 1e6).toFixed(2)}M</div>}
    </div>
  )
}

export function PriceChart({ symbol, recommendation, timeframeSignals }: Props) {
  const [interval, setInterval] = useState<Interval>('1h')
  const [candles, setCandles] = useState<Candle[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let stale = false                 // a slower reply for the previous symbol/interval must not win
    setLoading(true)
    setError(null)
    api.market.ohlcv(symbol, interval, 120)
      .then(r => { if (!stale) setCandles(r.candles) })
      .catch(e => { if (!stale) setError(e.message) })
      .finally(() => { if (!stale) setLoading(false) })
    return () => { stale = true }
  }, [symbol, interval])

  const srData = timeframeSignals?.[interval]
  const chartData = toChartData(candles, interval)
  const prices = candles.map(c => c.close)

  // Expand domain to include S/R levels so reference lines are visible
  const srLevels = srData
    ? [srData.pivot, srData.resistance_1, srData.resistance_2, srData.support_1, srData.support_2].filter(Boolean)
    : []
  const allPrices = [...prices, ...srLevels]
  const minPrice = allPrices.length ? Math.min(...allPrices) * 0.999 : 0
  const maxPrice = allPrices.length ? Math.max(...allPrices) * 1.001 : 0

  const rec = recommendation
  const hasLevels = rec && rec.action !== 'HOLD' && rec.entry_price > 0

  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <span className="font-bold text-white">Price Chart</span>
        <div className="flex gap-1">
          {INTERVALS.map(iv => (
            <button
              key={iv}
              onClick={() => setInterval(iv)}
              className={clsx(
                'px-2 py-1 rounded text-xs font-semibold transition-colors',
                interval === iv ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white',
              )}
            >
              {iv}
            </button>
          ))}
        </div>
      </div>

      {loading && (
        <div className="h-64 flex items-center justify-center text-gray-600 text-sm animate-pulse">
          Loading chart...
        </div>
      )}

      {error && !loading && (
        <div className="h-64 flex items-center justify-center text-red-500 text-sm">
          {error}
        </div>
      )}

      {!loading && !error && chartData.length > 0 && (
        <>
          {/* Trade level legend */}
          <div className="flex flex-wrap gap-3 text-xs">
            {hasLevels && <>
              <LevelTag color="text-blue-400" label="Entry" value={rec.entry_price} />
              <LevelTag color="text-red-400" label="Stop" value={rec.stop_loss} />
              <LevelTag color="text-emerald-400" label="TP1" value={rec.take_profit_1} />
              <LevelTag color="text-teal-300" label="TP2" value={rec.take_profit_2} />
            </>}
            {srData?.resistance_2 && <LevelTag color="text-orange-300" label="R2" value={srData.resistance_2} />}
            {srData?.resistance_1 && <LevelTag color="text-orange-400" label="R1" value={srData.resistance_1} />}
            {srData?.pivot && <LevelTag color="text-gray-400" label="Pivot" value={srData.pivot} />}
            {srData?.support_1 && <LevelTag color="text-green-400" label="S1" value={srData.support_1} />}
            {srData?.support_2 && <LevelTag color="text-green-300" label="S2" value={srData.support_2} />}
          </div>

          {/* Main price chart */}
          <ResponsiveContainer width="100%" height={440}>
            <ComposedChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="priceGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.25} />
                  <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" vertical={false} />
              <XAxis
                dataKey="time"
                tick={{ fill: '#6b7280', fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                interval="preserveStartEnd"
              />
              <YAxis
                domain={[minPrice, maxPrice]}
                tick={{ fill: '#6b7280', fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                width={70}
                tickFormatter={v => `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
              />
              <Tooltip content={<PriceTooltip />} />

              {hasLevels && <>
                <ReferenceLine y={rec.entry_price} stroke="#3b82f6" strokeDasharray="4 2" strokeWidth={1.5} />
                <ReferenceLine y={rec.stop_loss} stroke="#ef4444" strokeDasharray="4 2" strokeWidth={1.5} />
                <ReferenceLine y={rec.take_profit_1} stroke="#10b981" strokeDasharray="4 2" strokeWidth={1.5} />
                <ReferenceLine y={rec.take_profit_2} stroke="#14b8a6" strokeDasharray="4 2" strokeWidth={1.5} />
              </>}
              {srData?.pivot && <ReferenceLine y={srData.pivot} stroke="#9ca3af" strokeDasharray="2 2" strokeWidth={1} />}
              {srData?.resistance_1 && <ReferenceLine y={srData.resistance_1} stroke="#f97316" strokeDasharray="4 2" strokeWidth={1.5} />}
              {srData?.resistance_2 && <ReferenceLine y={srData.resistance_2} stroke="#fdba74" strokeDasharray="2 4" strokeWidth={1} />}
              {srData?.support_1 && <ReferenceLine y={srData.support_1} stroke="#4ade80" strokeDasharray="4 2" strokeWidth={1.5} />}
              {srData?.support_2 && <ReferenceLine y={srData.support_2} stroke="#86efac" strokeDasharray="2 4" strokeWidth={1} />}

              <Area
                type="monotone"
                dataKey="price"
                stroke="#3b82f6"
                strokeWidth={2}
                fill="url(#priceGrad)"
                dot={false}
                activeDot={{ r: 3, fill: '#3b82f6' }}
              />
            </ComposedChart>
          </ResponsiveContainer>

          {/* Volume chart */}
          <ResponsiveContainer width="100%" height={100}>
            <ComposedChart data={chartData} margin={{ top: 0, right: 4, left: 0, bottom: 0 }}>
              <YAxis hide domain={['auto', 'auto']} />
              <Bar dataKey="volume" name="volume" radius={[1, 1, 0, 0]}>
                {chartData.map((entry, i) => (
                  <rect key={i} fill={entry.volColor} fillOpacity={0.6} />
                ))}
              </Bar>
              <Tooltip content={<PriceTooltip />} />
            </ComposedChart>
          </ResponsiveContainer>
        </>
      )}
    </div>
  )
}

function LevelTag({ color, label, value }: { color: string; label: string; value: number }) {
  return (
    <span className={clsx('font-semibold', color)}>
      {label}: <span className="text-white">${value.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>
    </span>
  )
}
