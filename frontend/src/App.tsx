import { useState, useEffect, useCallback } from 'react'
import { Zap, BarChart2, Briefcase, RefreshCw, AlertCircle, Activity, Clock } from 'lucide-react'
import { OracleSignal } from './components/OracleSignal'
import { SignalScores } from './components/SignalScores'
import { Portfolio } from './components/Portfolio'
import { PriceChart } from './components/PriceChart'
import { PipelineStatus } from './components/PipelineStatus'
import { AlarmBanner } from './components/AlarmBanner'
import { useOracle } from './hooks/useOracle'
import { useAlarm } from './hooks/useAlarm'
import { api } from './utils/api'
import type { Portfolio as PortfolioType } from './types/oracle'
import clsx from 'clsx'

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT']
type Tab = 'signal' | 'portfolio'

export default function App() {
  const [tab, setTab] = useState<Tab>('signal')
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [portfolio, setPortfolio] = useState<PortfolioType | null>(null)
  const [executing, setExecuting] = useState(false)
  const [execMessage, setExecMessage] = useState<string | null>(null)

  const { data: oracle, loading, error, refresh, lastUpdated, countdown } = useOracle(symbol)

  const { isAlarming, dismiss } = useAlarm(
    oracle?.oracle ?? null,
    oracle?.signal_package ?? null,
  )

  const fetchPortfolio = useCallback(async () => {
    try {
      setPortfolio(await api.paper.portfolio())
    } catch {
      // silently ignore — portfolio is secondary
    }
  }, [])

  // Auto-refresh portfolio every 30s if there are open trades
  useEffect(() => {
    fetchPortfolio()
    const interval = setInterval(() => {
      if (portfolio?.stats.open_trades) fetchPortfolio()
    }, 30_000)
    return () => clearInterval(interval)
  }, [fetchPortfolio, portfolio?.stats.open_trades])

  async function handleExecute() {
    setExecuting(true)
    setExecMessage(null)
    try {
      const result = await api.oracle.executeAndTrade(symbol)
      setExecMessage(result.message)
      await fetchPortfolio()
      if (result.trade) setTab('portfolio')
    } catch (e) {
      setExecMessage(e instanceof Error ? e.message : 'Execution failed')
    } finally {
      setExecuting(false)
    }
  }

  const rec = oracle?.oracle?.trade_recommendation
  const canTrade = rec && rec.action !== 'HOLD' && rec.confidence >= 60

  return (
    <div className={clsx('min-h-screen bg-oracle-bg text-gray-100', isAlarming && 'pt-14')}>
      {/* Alarm banner — fixed at top, pushes layout down via pt-14 */}
      {isAlarming && oracle && (
        <AlarmBanner
          oracle={oracle.oracle}
          signal={oracle.signal_package}
          onDismiss={dismiss}
        />
      )}

      {/* Header */}
      <header className={clsx(
        'border-b border-gray-800 px-6 py-3 z-10 bg-oracle-bg/95 backdrop-blur',
        isAlarming ? 'sticky top-14' : 'sticky top-0',
      )}>
        <div className="max-w-6xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center">
              <Zap className="w-5 h-5 text-white" />
            </div>
            <div>
              <div className="font-black text-white text-lg leading-none">Crypto Oracle</div>
              <div className="text-xs text-gray-500">AI-Powered Trading Signals</div>
            </div>
          </div>

          <div className="flex items-center gap-4">
            {/* Live price */}
            {oracle?.signal_package?.price && (
              <div className="text-right hidden sm:block">
                <div className="text-white font-bold text-sm">
                  ${oracle.signal_package.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                </div>
                <div className="text-xs text-gray-500">{symbol.replace('USDT', '/USDT')}</div>
              </div>
            )}

            {/* Refresh countdown */}
            {lastUpdated && (
              <div className="flex items-center gap-1.5 text-xs text-gray-500">
                <Clock className="w-3 h-3" />
                <span>{countdown}s</span>
              </div>
            )}

            {/* Live pulse */}
            {loading && (
              <div className="flex items-center gap-1.5 text-xs text-blue-400">
                <Activity className="w-3 h-3 animate-pulse" />
                <span>Analyzing...</span>
              </div>
            )}

            {/* Cached badge */}
            {oracle?.cached && !loading && (
              <span className="text-xs bg-gray-800 text-gray-500 px-2 py-0.5 rounded">cached</span>
            )}
          </div>
        </div>
      </header>

      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6 space-y-5">
        {/* Symbol selector + controls */}
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex gap-1.5 flex-wrap">
            {SYMBOLS.map(s => (
              <button
                key={s}
                onClick={() => setSymbol(s)}
                className={clsx(
                  'px-3 py-1.5 rounded-lg text-sm font-semibold transition-all',
                  symbol === s ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white',
                )}
              >
                {s.replace('USDT', '')}
              </button>
            ))}
          </div>

          <div className="ml-auto flex gap-2">
            <button
              onClick={refresh}
              disabled={loading}
              className="flex items-center gap-2 bg-gray-800 hover:bg-gray-700 text-white px-3 py-1.5 rounded-lg text-sm font-semibold transition-all disabled:opacity-50"
            >
              <RefreshCw className={clsx('w-4 h-4', loading && 'animate-spin')} />
              Refresh
            </button>

            {canTrade && (
              <button
                onClick={handleExecute}
                disabled={executing}
                className={clsx(
                  'flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-bold transition-all disabled:opacity-50',
                  rec?.action === 'BUY' ? 'bg-emerald-600 hover:bg-emerald-500' : 'bg-red-600 hover:bg-red-500',
                  'text-white',
                )}
              >
                <Zap className="w-4 h-4" />
                {executing ? 'Opening...' : `Paper ${rec?.action}`}
              </button>
            )}
          </div>
        </div>

        {/* Exec message */}
        {execMessage && (
          <div className={clsx(
            'text-sm px-4 py-2 rounded-lg border',
            execMessage.includes('opened')
              ? 'bg-emerald-900/30 border-emerald-800 text-emerald-400'
              : 'bg-gray-800 border-gray-700 text-gray-400',
          )}>
            {execMessage}
          </div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 border-b border-gray-800">
          {([
            ['signal', BarChart2, 'Signal'],
            ['portfolio', Briefcase, 'Portfolio'],
          ] as const).map(([t, Icon, label]) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={clsx(
                'flex items-center gap-2 pb-3 px-1 mr-3 text-sm font-semibold border-b-2 transition-all',
                tab === t ? 'border-blue-500 text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-300',
              )}
            >
              <Icon className="w-4 h-4" />
              {label}
            </button>
          ))}
        </div>

        {/* Error */}
        {error && (
          <div className="flex items-center gap-2 bg-red-900/30 border border-red-800 rounded-lg px-4 py-3 text-red-400 text-sm">
            <AlertCircle className="w-4 h-4 shrink-0" />
            {error}
          </div>
        )}

        {/* Loading skeleton */}
        {loading && !oracle && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {[1, 2].map(i => (
              <div key={i} className="card animate-pulse h-64">
                <div className="h-5 bg-gray-800 rounded w-1/3 mb-4" />
                <div className="space-y-3">
                  <div className="h-16 bg-gray-800 rounded" />
                  <div className="h-8 bg-gray-800 rounded" />
                  <div className="h-8 bg-gray-800 rounded" />
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Signal tab */}
        {tab === 'signal' && oracle && (
          <div className="space-y-4">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <OracleSignal oracle={oracle.oracle} />
              <SignalScores signal={oracle.signal_package} />
            </div>
            <PriceChart
              symbol={symbol}
              recommendation={oracle.oracle?.trade_recommendation}
              timeframeSignals={oracle.signal_package.ta_summary.timeframe_signals}
            />
            {oracle.pipeline_status && (
              <PipelineStatus status={oracle.pipeline_status} />
            )}
          </div>
        )}

        {/* Portfolio tab */}
        {tab === 'portfolio' && portfolio && (
          <Portfolio portfolio={portfolio} onRefresh={fetchPortfolio} />
        )}
      </div>
    </div>
  )
}
