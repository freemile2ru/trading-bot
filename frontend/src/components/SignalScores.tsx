import type { SignalPackage, TimeframeSignal } from '../types/oracle'
import clsx from 'clsx'

interface Props {
  signal: SignalPackage
}

function fmtPrice(n: number): string {
  if (!n) return 'N/A'
  if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 })
  if (n >= 1) return n.toLocaleString(undefined, { maximumFractionDigits: 4 })
  return n.toFixed(6)
}

function fmtVol(n: number): string {
  if (!n) return 'N/A'
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`
  return `$${n.toLocaleString()}`
}

export function SignalScores({ signal }: Props) {
  const { component_scores, ta_summary, fear_greed, social, macro_summary, best_timeframe, coingecko } = signal

  const bestTfData: TimeframeSignal | undefined = ta_summary.timeframe_signals?.[best_timeframe]

  return (
    <div className="card space-y-4">
      <div className="font-bold text-white">Signal Breakdown</div>

      {/* Component score bars */}
      <div className="space-y-2">
        {Object.entries(component_scores).map(([key, val]) => (
          <ScoreBar key={key} label={key} score={val as number} />
        ))}
      </div>

      {/* Timeframe signals */}
      <div className="border-t border-gray-800 pt-3">
        <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">Timeframe Signals</div>
        <div className="space-y-1">
          {Object.entries(ta_summary.timeframe_signals || {}).map(([tf, data]) => (
            <div key={tf} className="flex items-center justify-between text-xs">
              <span className={clsx('font-mono w-8', tf === best_timeframe ? 'text-blue-400 font-bold' : 'text-gray-400')}>{tf}</span>
              <span className="text-gray-300">RSI {data.rsi?.toFixed(1)}</span>
              <SignalBadge label={data.signal} />
              <ScorePill score={data.score} />
            </div>
          ))}
        </div>
      </div>

      {/* Key Levels — pivot, R1/R2, S1/S2 from best timeframe */}
      {bestTfData?.pivot && (
        <div className="border-t border-gray-800 pt-3">
          <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">
            Key Levels
            <span className="text-gray-700 font-normal ml-1 normal-case">({best_timeframe} pivot)</span>
          </div>
          <div className="space-y-1">
            <LevelRow
              label="R2"
              value={bestTfData.resistance_2}
              color="text-red-500"
              current={signal.price}
            />
            <LevelRow
              label="R1"
              value={bestTfData.resistance_1}
              color="text-red-400"
              current={signal.price}
              dist={bestTfData.distance_to_r1_pct}
            />
            <div className="flex items-center gap-2 py-0.5">
              <span className="text-gray-600 text-xs w-8">Pivot</span>
              <div className="h-px flex-1 bg-gray-700" />
              <span className="text-gray-400 text-xs font-mono">${fmtPrice(bestTfData.pivot)}</span>
            </div>
            <LevelRow
              label="S1"
              value={bestTfData.support_1}
              color="text-emerald-400"
              current={signal.price}
              dist={bestTfData.distance_to_s1_pct}
            />
            <LevelRow
              label="S2"
              value={bestTfData.support_2}
              color="text-emerald-500"
              current={signal.price}
            />
          </div>

          {/* Fibonacci nearest levels */}
          {bestTfData.fib_nearest_support && (
            <div className="mt-2 flex gap-3 text-xs text-gray-600">
              <span>Fib support: <span className="text-emerald-600">${fmtPrice(bestTfData.fib_nearest_support)}</span></span>
              {bestTfData.fib_nearest_resistance && (
                <span>Fib resist: <span className="text-red-600">${fmtPrice(bestTfData.fib_nearest_resistance)}</span></span>
              )}
            </div>
          )}
        </div>
      )}

      {/* Volume */}
      <div className="border-t border-gray-800 pt-3">
        <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">Volume</div>
        <div className="grid grid-cols-3 gap-2">
          {/* 24h volume from CoinGecko */}
          <VolumeCard
            label="24h Volume"
            value={fmtVol(coingecko.total_volume_usd)}
            sub={coingecko.price_change_24h_pct != null
              ? `${coingecko.price_change_24h_pct >= 0 ? '+' : ''}${coingecko.price_change_24h_pct?.toFixed(2)}% 24h`
              : undefined}
            valueColor={coingecko.total_volume_usd ? 'text-white' : 'text-gray-600'}
          />
          {/* Volume vs 20-bar avg from TA */}
          {bestTfData?.volume_ratio != null && (
            <VolumeCard
              label={`Vol vs Avg (${best_timeframe})`}
              value={`${bestTfData.volume_ratio}×`}
              sub={bestTfData.volume_signal}
              valueColor={
                bestTfData.volume_signal === 'high' ? 'text-yellow-400'
                  : bestTfData.volume_signal === 'low' ? 'text-gray-500'
                  : 'text-white'
              }
            />
          )}
          {/* OBV trend */}
          {bestTfData?.obv_trend && (
            <VolumeCard
              label="OBV Trend"
              value={bestTfData.obv_trend}
              sub="On-balance vol"
              valueColor={bestTfData.obv_trend === 'rising' ? 'text-emerald-400' : 'text-red-400'}
            />
          )}
        </div>
      </div>

      {/* Market Sentiment */}
      <div className="border-t border-gray-800 pt-3">
        <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">Market Sentiment</div>
        <div className="grid grid-cols-3 gap-2 text-center">
          <SentimentCard
            label="Fear & Greed"
            value={`${fear_greed.value}`}
            sub={fear_greed.classification}
            color={fear_greed.value >= 60 ? 'text-emerald-400' : fear_greed.value <= 30 ? 'text-red-400' : 'text-yellow-400'}
          />
          <SentimentCard
            label="Twitter"
            value={social.sentiment_label}
            sub={`${social.bullish_pct}% bullish`}
            color={social.sentiment_label === 'bullish' ? 'text-emerald-400' : social.sentiment_label === 'bearish' ? 'text-red-400' : 'text-gray-400'}
          />
          <SentimentCard
            label="VIX"
            value={macro_summary.vix?.toFixed(1) ?? 'N/A'}
            sub={macro_summary.yield_curve_inverted ? 'Inverted curve' : 'Normal curve'}
            color={macro_summary.vix > 25 ? 'text-red-400' : 'text-emerald-400'}
          />
        </div>
      </div>

      {/* Headlines */}
      {signal.news_summary.top_headlines.length > 0 && (
        <div className="border-t border-gray-800 pt-3">
          <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">Top Headlines</div>
          <ul className="space-y-1">
            {signal.news_summary.top_headlines.filter(Boolean).slice(0, 4).map((h, i) => (
              <li key={i} className="text-xs text-gray-400 truncate">• {h}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function LevelRow({
  label, value, color, current, dist,
}: {
  label: string
  value: number
  color: string
  current: number
  dist?: number
}) {
  if (!value) return null
  const isCurrent = current > 0
  const isAbove = isCurrent && value > current
  const isBelow = isCurrent && value < current
  return (
    <div className="flex items-center gap-2 text-xs py-0.5">
      <span className={clsx('w-8 font-bold', color)}>{label}</span>
      <div className="flex-1 h-px bg-gray-800" />
      <span className={clsx('font-mono', color)}>${fmtPrice(value)}</span>
      {dist != null && (
        <span className={clsx('text-gray-600 w-14 text-right', isAbove ? 'text-red-700' : 'text-emerald-700')}>
          {isAbove ? '+' : '-'}{Math.abs(dist).toFixed(2)}%
        </span>
      )}
      {!dist && (
        <span className="w-14 text-right text-gray-700">
          {isCurrent ? (isAbove ? '↑ above' : isBelow ? '↓ below' : '') : ''}
        </span>
      )}
    </div>
  )
}

function VolumeCard({ label, value, sub, valueColor }: { label: string; value: string; sub?: string; valueColor: string }) {
  return (
    <div className="bg-gray-800/50 rounded-lg p-2">
      <div className="text-xs text-gray-500 mb-0.5">{label}</div>
      <div className={clsx('font-bold text-sm capitalize', valueColor)}>{value}</div>
      {sub && <div className="text-xs text-gray-600 capitalize">{sub}</div>}
    </div>
  )
}

function ScoreBar({ label, score }: { label: string; score: number }) {
  const pct = ((score + 1) / 2) * 100
  const color = score > 0.2 ? 'bg-emerald-500' : score < -0.2 ? 'bg-red-500' : 'bg-yellow-500'
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-gray-400 capitalize w-20">{label}</span>
      <div className="flex-1 h-2 bg-gray-800 rounded-full overflow-hidden">
        <div className={clsx('h-full rounded-full', color)} style={{ width: `${pct}%` }} />
      </div>
      <span className={clsx('w-12 text-right font-mono font-bold', score > 0 ? 'text-emerald-400' : score < 0 ? 'text-red-400' : 'text-gray-400')}>
        {score > 0 ? '+' : ''}{score.toFixed(3)}
      </span>
    </div>
  )
}

function SignalBadge({ label }: { label: string }) {
  const map: Record<string, string> = {
    strong_buy: 'badge-buy',
    buy: 'badge-buy',
    strong_sell: 'badge-sell',
    sell: 'badge-sell',
    neutral: 'badge-neutral',
  }
  return <span className={clsx(map[label] ?? 'badge-neutral', 'capitalize')}>{label?.replace('_', ' ')}</span>
}

function ScorePill({ score }: { score: number }) {
  return (
    <span className={clsx('font-mono text-xs font-bold', score > 0 ? 'text-emerald-400' : score < 0 ? 'text-red-400' : 'text-gray-500')}>
      {score > 0 ? '+' : ''}{score?.toFixed(3)}
    </span>
  )
}

function SentimentCard({ label, value, sub, color }: { label: string; value: string; sub: string; color: string }) {
  return (
    <div className="bg-gray-800/50 rounded-lg p-2">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={clsx('font-bold text-sm capitalize', color)}>{value}</div>
      <div className="text-xs text-gray-600 truncate">{sub}</div>
    </div>
  )
}
