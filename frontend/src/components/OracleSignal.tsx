import { TrendingUp, TrendingDown, Minus, Shield, Target, Brain, Activity, Timer } from 'lucide-react'
import type { OracleResult } from '../types/oracle'
import { usePredictionTimer } from '../hooks/usePredictionTimer'
import clsx from 'clsx'

interface Props {
  oracle: OracleResult
  lastUpdated?: Date | null
}

export function OracleSignal({ oracle, lastUpdated }: Props) {
  const rec = oracle.trade_recommendation
  const ctx = oracle.market_context
  const timer = usePredictionTimer(rec.timeframe, lastUpdated ?? null)

  const actionColor = {
    BUY: 'text-emerald-400',
    SELL: 'text-red-400',
    HOLD: 'text-yellow-400',
  }[rec.action]

  const ActionIcon = rec.action === 'BUY' ? TrendingUp : rec.action === 'SELL' ? TrendingDown : Minus

  const trendConfig = {
    bullish: { bg: 'bg-emerald-900/50', border: 'border-emerald-700/60', text: 'text-emerald-300', label: 'BULLISH TREND', Icon: TrendingUp },
    bearish: { bg: 'bg-red-900/50', border: 'border-red-700/60', text: 'text-red-300', label: 'BEARISH TREND', Icon: TrendingDown },
    ranging: { bg: 'bg-yellow-900/40', border: 'border-yellow-700/60', text: 'text-yellow-300', label: 'RANGING MARKET', Icon: Minus },
  }[ctx.trend] ?? { bg: 'bg-gray-800/50', border: 'border-gray-700', text: 'text-gray-300', label: ctx.trend?.toUpperCase(), Icon: Minus }

  const alignmentLabel = {
    aligned_bullish: { text: 'text-emerald-400', label: '↑ Aligned Bullish' },
    aligned_bearish: { text: 'text-red-400', label: '↓ Aligned Bearish' },
    mixed: { text: 'text-yellow-400', label: '⇄ Mixed' },
  }[ctx.tf_alignment] ?? { text: 'text-gray-400', label: ctx.tf_alignment }

  const regimeLabel = {
    trending: 'Trending',
    ranging: 'Ranging',
    volatile: 'Volatile',
  }[ctx.regime] ?? ctx.regime

  const hasEntryZone = rec.entry_zone?.low > 0 && rec.entry_zone?.high > 0

  return (
    <div className="card space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Brain className="w-5 h-5 text-blue-400" />
          <span className="font-bold text-white text-lg">Oracle Signal</span>
          <span className="text-xs text-gray-500">{rec.symbol}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">Confidence</span>
          <ConfidenceBar value={rec.confidence} />
        </div>
      </div>

      {/* Prediction validity countdown */}
      {lastUpdated && (
        <div className="space-y-1">
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-1.5 text-gray-400">
              <Timer className="w-3 h-3" />
              <span>{timer.label}</span>
            </div>
            <span className="text-gray-600">{rec.timeframe} signal · {rec.hold_duration}</span>
          </div>
          <div className="w-full h-1 bg-gray-800 rounded-full overflow-hidden">
            <div
              className={clsx(
                'h-full rounded-full transition-all duration-1000',
                timer.color === 'green' ? 'bg-emerald-500' :
                timer.color === 'yellow' ? 'bg-yellow-500' :
                timer.color === 'red' ? 'bg-red-500' : 'bg-gray-600',
              )}
              style={{ width: `${timer.pct}%` }}
            />
          </div>
        </div>
      )}

      {/* Market trend banner */}
      <div className={clsx('flex items-center justify-between rounded-lg px-4 py-2.5 border', trendConfig.bg, trendConfig.border)}>
        <div className="flex items-center gap-2">
          <trendConfig.Icon className={clsx('w-5 h-5', trendConfig.text)} strokeWidth={2.5} />
          <span className={clsx('font-black text-sm tracking-widest', trendConfig.text)}>{trendConfig.label}</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-400">
            Regime: <span className="text-gray-200 font-semibold">{regimeLabel}</span>
          </span>
          <span className="text-gray-600">·</span>
          <div className="flex items-center gap-1">
            <Activity className="w-3 h-3 text-gray-500" />
            <span className={clsx('text-xs font-semibold', alignmentLabel.text)}>{alignmentLabel.label}</span>
          </div>
        </div>
      </div>

      {/* Main signal */}
      <div className="flex items-center gap-4 bg-gray-800/60 rounded-lg p-4">
        <ActionIcon className={clsx('w-10 h-10', actionColor)} strokeWidth={2.5} />
        <div>
          <div className={clsx('text-3xl font-black', actionColor)}>{rec.action}</div>
          <div className="text-sm text-gray-400 capitalize">{rec.direction} · {rec.timeframe} · {rec.hold_duration}</div>
        </div>
        <div className="ml-auto text-right">
          <div className="text-2xl font-bold text-white">${rec.entry_price.toLocaleString(undefined, { maximumFractionDigits: 4 })}</div>
          <div className="text-xs text-gray-500">Entry price</div>
          {hasEntryZone && (
            <div className="text-xs text-gray-500 mt-0.5">
              Zone: <span className="text-gray-300">${rec.entry_zone.low.toLocaleString(undefined, { maximumFractionDigits: 2 })} – ${rec.entry_zone.high.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span>
            </div>
          )}
        </div>
      </div>

      {/* Levels grid */}
      <div className="grid grid-cols-4 gap-2">
        <LevelCard label="Stop Loss" value={rec.stop_loss} color="text-red-400" icon={<Shield className="w-4 h-4 text-red-500" />} />
        <LevelCard label="Take Profit 1" value={rec.take_profit_1} color="text-emerald-400" icon={<Target className="w-4 h-4 text-emerald-500" />} />
        <LevelCard label="Take Profit 2" value={rec.take_profit_2} color="text-blue-400" icon={<Target className="w-4 h-4 text-blue-500" />} />
        <LevelCard label="Take Profit 3" value={rec.take_profit_3} color="text-purple-400" icon={<Target className="w-4 h-4 text-purple-500" />} />
      </div>

      {/* Meta row */}
      <div className="grid grid-cols-4 gap-2 text-center text-xs">
        <MetaItem label="R:R" value={rec.risk_reward_ratio} />
        <MetaItem label="Size" value={rec.position_size_suggestion} />
        <MetaItem label="Phase" value={ctx.market_phase} />
        <MetaItem label="Volatility" value={ctx.volatility} />
      </div>

      {/* Reasoning */}
      <div className="bg-gray-800/40 rounded-lg p-3 space-y-1">
        <div className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Oracle Thesis</div>
        <div className="text-sm text-gray-200">{oracle.reasoning.primary_thesis}</div>
      </div>

      {/* Macro environment */}
      {ctx.macro_environment && (
        <div className="text-xs text-gray-400 bg-gray-800/30 rounded px-3 py-2">
          <span className="font-semibold text-gray-500">Macro: </span>{ctx.macro_environment}
        </div>
      )}

      {/* Risk factors */}
      {oracle.reasoning.risk_factors && (
        <div className="text-xs text-gray-500 border-t border-gray-800 pt-3">
          <span className="font-semibold text-yellow-600">Risks: </span>{oracle.reasoning.risk_factors}
        </div>
      )}

      {/* Alerts */}
      {oracle.alerts?.length > 0 && (
        <div className="space-y-1">
          {oracle.alerts.map((a, i) => (
            <div key={i} className="text-xs text-yellow-400 bg-yellow-900/20 border border-yellow-800/40 rounded px-2 py-1">
              ⚠ {a}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function LevelCard({ label, value, color, icon }: { label: string; value: number; color: string; icon: React.ReactNode }) {
  return (
    <div className="bg-gray-800/50 rounded-lg p-3">
      <div className="flex items-center gap-1 mb-1">{icon}<span className="text-xs text-gray-500">{label}</span></div>
      <div className={clsx('font-bold text-sm', color)}>${value?.toLocaleString(undefined, { maximumFractionDigits: 4 }) ?? '—'}</div>
    </div>
  )
}

function MetaItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-800/50 rounded-lg p-2">
      <div className="text-gray-500 text-xs">{label}</div>
      <div className="text-white font-semibold capitalize text-xs">{value}</div>
    </div>
  )
}

function ConfidenceBar({ value }: { value: number }) {
  const color = value >= 70 ? 'bg-emerald-500' : value >= 50 ? 'bg-yellow-500' : 'bg-red-500'
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 h-2 bg-gray-700 rounded-full overflow-hidden">
        <div className={clsx('h-full rounded-full transition-all', color)} style={{ width: `${value}%` }} />
      </div>
      <span className="text-xs font-bold text-white">{value}%</span>
    </div>
  )
}
