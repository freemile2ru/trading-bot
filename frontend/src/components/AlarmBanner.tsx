import { X, BellRing, TrendingUp, TrendingDown } from 'lucide-react'
import type { OracleResult, SignalPackage } from '../types/oracle'
import clsx from 'clsx'

interface Props {
  oracle: OracleResult
  signal: SignalPackage
  onDismiss: () => void
}

export function AlarmBanner({ oracle, signal, onDismiss }: Props) {
  const rec = oracle.trade_recommendation
  const isBuy = rec.action === 'BUY'
  const Icon = isBuy ? TrendingUp : TrendingDown

  return (
    <div
      className={clsx(
        'fixed inset-x-0 top-0 z-50 flex items-center justify-between gap-4 px-5 py-3 shadow-2xl',
        isBuy
          ? 'bg-emerald-700 border-b-2 border-emerald-400'
          : 'bg-red-800 border-b-2 border-red-500',
      )}
    >
      {/* Pulsing icon */}
      <div className="flex items-center gap-3 min-w-0">
        <div className="relative shrink-0">
          <BellRing className="w-5 h-5 text-white animate-bounce" />
          <span className="absolute -top-1 -right-1 w-2 h-2 rounded-full bg-white animate-ping" />
        </div>

        <Icon className="w-6 h-6 text-white shrink-0" strokeWidth={2.5} />

        <div className="min-w-0">
          <div className="text-white font-black text-base leading-tight tracking-wide">
            STRONG {rec.action} SIGNAL — {rec.symbol}
          </div>
          <div className="text-white/75 text-xs leading-tight truncate">
            {rec.confidence}% confidence
            {rec.entry_price > 0 && ` · Entry $${rec.entry_price.toLocaleString(undefined, { maximumFractionDigits: 2 })}`}
            {` · ${rec.hold_duration}`}
            {` · Score ${signal.composite_score > 0 ? '+' : ''}${signal.composite_score.toFixed(3)}`}
            {signal.tf_alignment?.aligned && ` · ${signal.tf_alignment.direction} alignment`}
          </div>
        </div>
      </div>

      {/* Dismiss */}
      <button
        onClick={onDismiss}
        className={clsx(
          'shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-bold transition-colors',
          'bg-white/20 hover:bg-white/35 text-white',
        )}
      >
        <X className="w-4 h-4" />
        Dismiss
      </button>
    </div>
  )
}
