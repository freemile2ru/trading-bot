import type { PipelineStatus as PipelineStatusType } from '../types/oracle'
import clsx from 'clsx'

interface Props {
  status: PipelineStatusType
}

const SOURCE_LABELS: Record<string, string> = {
  binance: 'Binance',
  coingecko: 'CoinGecko',
  global_market: 'Global Market',
  news: 'News',
  macro: 'FRED Macro',
  fear_greed: 'Fear & Greed',
  social: 'Twitter/X',
}

export function PipelineStatus({ status }: Props) {
  const { data_sources, ta_timeframes, elapsed_seconds, oracle_ok } = status

  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <span className="font-bold text-white text-sm">Data Sources</span>
        <div className="flex items-center gap-3 text-xs text-gray-500">
          <span>{elapsed_seconds.toFixed(1)}s</span>
          <span className={clsx('font-semibold', oracle_ok ? 'text-emerald-400' : 'text-red-400')}>
            Claude {oracle_ok ? '✓' : '✗'}
          </span>
        </div>
      </div>

      {/* Source status grid */}
      <div className="grid grid-cols-2 gap-1.5">
        {Object.entries(data_sources).map(([key, val]) => {
          const ok = val.ok
          return (
            <div
              key={key}
              className={clsx(
                'flex items-center gap-2 rounded-lg px-2 py-1.5 text-xs',
                ok === true && 'bg-emerald-900/20 border border-emerald-800/30',
                ok === 'partial' && 'bg-yellow-900/20 border border-yellow-800/30',
                ok === false && 'bg-red-900/20 border border-red-800/30',
              )}
              title={ok !== true ? (val as { ok: false | 'partial'; error: string }).error : undefined}
            >
              <span className={clsx(
                'shrink-0 font-bold',
                ok === true && 'text-emerald-400',
                ok === 'partial' && 'text-yellow-400',
                ok === false && 'text-red-400',
              )}>
                {ok === true ? '✓' : ok === 'partial' ? '~' : '✗'}
              </span>
              <span className={clsx(
                'truncate',
                ok === true ? 'text-gray-300' : ok === 'partial' ? 'text-yellow-300' : 'text-red-300',
              )}>
                {SOURCE_LABELS[key] ?? key}
              </span>
              {ok !== true && (
                <span className="ml-auto shrink-0 text-gray-600 text-xs truncate max-w-[80px]">
                  {(val as { ok: false | 'partial'; error: string }).error.split(':')[0]}
                </span>
              )}
            </div>
          )
        })}
      </div>

      {/* TA timeframes */}
      <div className="border-t border-gray-800 pt-2">
        <div className="text-xs text-gray-500 mb-1.5">TA Timeframes</div>
        <div className="flex gap-2">
          {Object.entries(ta_timeframes).map(([tf, status]) => {
            const ok = !status.includes('error')
            return (
              <div
                key={tf}
                className={clsx(
                  'px-2 py-1 rounded text-xs font-mono font-semibold',
                  ok ? 'bg-emerald-900/30 text-emerald-400' : 'bg-red-900/30 text-red-400',
                )}
                title={status}
              >
                {tf}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
