import { useState } from 'react'
import { TrendingUp, TrendingDown, X, RefreshCw, RotateCcw } from 'lucide-react'
import {
  AreaChart, Area, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, CartesianGrid,
} from 'recharts'
import type { Portfolio as PortfolioType, PaperTrade } from '../types/oracle'
import { api } from '../utils/api'
import clsx from 'clsx'

interface Props {
  portfolio: PortfolioType
  onRefresh: () => void
}

export function Portfolio({ portfolio, onRefresh }: Props) {
  const { stats, open_trades, closed_trades, equity_history } = portfolio
  const [resetting, setResetting] = useState(false)

  async function handleClose(trade: PaperTrade) {
    await api.paper.closeTrade(trade.id, trade.current_price)
    onRefresh()
  }

  async function handleUpdatePrices() {
    await api.paper.updatePrices()
    onRefresh()
  }

  async function handleReset() {
    if (!confirm('Reset portfolio? All trades will be cleared.')) return
    setResetting(true)
    await api.paper.reset(10000)
    onRefresh()
    setResetting(false)
  }

  // Equity curve data
  const equityData = equity_history.map(e => ({
    time: new Date(e.ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
    equity: e.equity,
  }))
  const initialEquity = portfolio.initial_balance
  const currentEquity = portfolio.total_equity
  const equityUp = currentEquity >= initialEquity

  return (
    <div className="space-y-4">
      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Total Equity"
          value={`$${portfolio.total_equity.toLocaleString()}`}
          sub={`${portfolio.total_return_pct >= 0 ? '+' : ''}${portfolio.total_return_pct.toFixed(2)}% return`}
          color={portfolio.total_return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}
        />
        <StatCard label="Cash Balance" value={`$${portfolio.balance.toLocaleString()}`} sub="Available" />
        <StatCard
          label="Open P&L"
          value={`${portfolio.open_pnl >= 0 ? '+' : ''}$${portfolio.open_pnl.toFixed(2)}`}
          sub="Unrealized"
          color={portfolio.open_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}
        />
        <StatCard
          label="Win Rate"
          value={`${stats.win_rate_pct.toFixed(1)}%`}
          sub={`PF: ${stats.profit_factor}x · ${stats.total_trades} trades`}
          color={stats.win_rate_pct >= 50 ? 'text-emerald-400' : 'text-red-400'}
        />
      </div>

      {/* Equity curve */}
      {equityData.length > 1 && (
        <div className="card">
          <div className="font-bold text-white text-sm mb-3">Equity Curve</div>
          <ResponsiveContainer width="100%" height={120}>
            <AreaChart data={equityData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={equityUp ? '#10b981' : '#ef4444'} stopOpacity={0.3} />
                  <stop offset="95%" stopColor={equityUp ? '#10b981' : '#ef4444'} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" vertical={false} />
              <XAxis dataKey="time" hide />
              <YAxis
                domain={['auto', 'auto']}
                tick={{ fill: '#6b7280', fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                width={65}
                tickFormatter={v => `$${v.toLocaleString()}`}
              />
              <Tooltip
                contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
                formatter={(v: number) => [`$${v.toLocaleString()}`, 'Equity']}
              />
              <ReferenceLine y={initialEquity} stroke="#4b5563" strokeDasharray="3 3" />
              <Area
                type="monotone"
                dataKey="equity"
                stroke={equityUp ? '#10b981' : '#ef4444'}
                strokeWidth={2}
                fill="url(#equityGrad)"
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Open trades */}
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <div className="font-bold text-white">Open Trades ({open_trades.length})</div>
          <div className="flex gap-2">
            <button
              onClick={handleUpdatePrices}
              className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300 transition-colors"
            >
              <RefreshCw className="w-3 h-3" /> Update Prices
            </button>
            <button
              onClick={handleReset}
              disabled={resetting}
              className="flex items-center gap-1 text-xs text-gray-500 hover:text-red-400 transition-colors"
            >
              <RotateCcw className="w-3 h-3" /> Reset
            </button>
          </div>
        </div>
        {open_trades.length === 0 ? (
          <div className="text-center text-gray-600 py-6 text-sm">No open trades</div>
        ) : (
          <div className="space-y-2">
            {open_trades.map(trade => (
              <TradeRow key={trade.id} trade={trade} onClose={() => handleClose(trade)} />
            ))}
          </div>
        )}
      </div>

      {/* Closed trades */}
      {closed_trades.length > 0 && (
        <div className="card">
          <div className="font-bold text-white mb-3">Recent Closed Trades</div>
          <div className="space-y-2">
            {closed_trades.slice(0, 10).map(trade => (
              <TradeRow key={trade.id} trade={trade} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function TradeRow({ trade, onClose }: { trade: PaperTrade; onClose?: () => void }) {
  const isLong = trade.direction === 'long'
  const Icon = isLong ? TrendingUp : TrendingDown

  return (
    <div className="flex items-center gap-3 bg-gray-800/50 rounded-lg px-3 py-2 text-xs">
      <Icon className={clsx('w-4 h-4 shrink-0', isLong ? 'text-emerald-400' : 'text-red-400')} />
      <span className="font-mono font-bold text-white w-20">{trade.symbol}</span>
      <span className="text-gray-500 capitalize">{trade.direction} · {trade.timeframe}</span>
      <span className="text-gray-400">
        ${trade.entry_price.toLocaleString(undefined, { maximumFractionDigits: 4 })}
      </span>
      {trade.status === 'open' && (
        <span className="text-gray-500">
          → ${trade.current_price.toLocaleString(undefined, { maximumFractionDigits: 4 })}
        </span>
      )}
      <span className={clsx('ml-auto font-bold', trade.pnl_usd >= 0 ? 'text-emerald-400' : 'text-red-400')}>
        {trade.pnl_usd >= 0 ? '+' : ''}${trade.pnl_usd.toFixed(2)}
        <span className="text-gray-600 font-normal ml-1">({trade.pnl_pct.toFixed(2)}%)</span>
      </span>
      {trade.close_reason && (
        <span className="text-gray-600 capitalize">{trade.close_reason.replace('_', ' ')}</span>
      )}
      {onClose && (
        <button onClick={onClose} className="p-1 text-gray-600 hover:text-red-400 transition-colors">
          <X className="w-3 h-3" />
        </button>
      )}
    </div>
  )
}

function StatCard({ label, value, sub, color }: { label: string; value: string; sub: string; color?: string }) {
  return (
    <div className="card">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      <div className={clsx('text-xl font-black', color ?? 'text-white')}>{value}</div>
      <div className="text-xs text-gray-600">{sub}</div>
    </div>
  )
}
