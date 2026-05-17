import { TrendingUp, TrendingDown, Minus, Shield, Target, Brain } from 'lucide-react'
import type { OracleResult } from '../types/oracle'
import clsx from 'clsx'

interface Props {
  oracle: OracleResult
}

export function OracleSignal({ oracle }: Props) {
  const rec = oracle.trade_recommendation
  const ctx = oracle.market_context

  const actionColor = {
    BUY: 'text-emerald-400',
    SELL: 'text-red-400',
    HOLD: 'text-yellow-400',
  }[rec.action]

  const ActionIcon = rec.action === 'BUY' ? TrendingUp : rec.action === 'SELL' ? TrendingDown : Minus

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
        </div>
      </div>

      {/* Levels grid */}
      <div className="grid grid-cols-3 gap-3">
        <LevelCard label="Stop Loss" value={rec.stop_loss} color="text-red-400" icon={<Shield className="w-4 h-4 text-red-500" />} />
        <LevelCard label="Take Profit 1" value={rec.take_profit_1} color="text-emerald-400" icon={<Target className="w-4 h-4 text-emerald-500" />} />
        <LevelCard label="Take Profit 2" value={rec.take_profit_2} color="text-blue-400" icon={<Target className="w-4 h-4 text-blue-500" />} />
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
      <div className={clsx('font-bold text-sm', color)}>${value.toLocaleString(undefined, { maximumFractionDigits: 4 })}</div>
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
