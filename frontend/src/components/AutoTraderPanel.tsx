import { useMemo } from 'react'
import {
  CartesianGrid, Legend, Line, LineChart, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import {
  Bot, BrainCircuit, CheckCircle2, Clock, Hourglass, LogOut, Pause, Play, ShieldCheck, Timer, X, XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { evenTicks } from '../utils/chart'
import type { EquityPoint } from '../types/oracle'
import type { AutoOrder, AutoStatus, AutoSummary, AutoTrade } from '../types/signals'

// Validated pair (reference palette, dark slots 1–2) against the gray-900 card surface.
const AUTO_COLOR = '#3987e5'
const MAIN_COLOR = '#d95926'
const MAX_POINTS = 400

function fmtPrice(n: number | null | undefined): string {
  if (n == null) return '—'
  const digits = n >= 1000 ? 2 : n >= 1 ? 3 : 6
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: digits })}`
}

function fmtUsd(n: number): string {
  return `${n >= 0 ? '+' : '−'}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 3600) return `${Math.floor(s / 60)}m`
  const h = Math.floor(s / 3600)
  return h < 48 ? `${h}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(h / 24)}d ${h % 24}h`
}

/** Both wallets indexed to % return on one axis — never two y-scales. */
function buildSeries(
  auto: EquityPoint[], autoBase: number, main: EquityPoint[], mainBase: number,
) {
  type Row = { t: number; auto?: number; main?: number }
  const events: { t: number; key: 'auto' | 'main'; v: number }[] = [
    ...auto.map(p => ({ t: Date.parse(p.ts), key: 'auto' as const, v: (p.equity / autoBase - 1) * 100 })),
    ...main.map(p => ({ t: Date.parse(p.ts), key: 'main' as const, v: (p.equity / mainBase - 1) * 100 })),
  ].filter(e => Number.isFinite(e.t))
  events.sort((a, b) => a.t - b.t)

  // The comparison window starts when the experiment does.
  const start = auto.length ? Date.parse(auto[0].ts) : 0
  const rows: Row[] = []
  let lastAuto: number | undefined
  let lastMain: number | undefined
  for (const e of events) {
    if (e.key === 'auto') lastAuto = e.v
    else lastMain = e.v
    if (e.t < start) continue
    rows.push({ t: e.t, auto: lastAuto, main: lastMain })
  }
  if (rows.length <= MAX_POINTS) return rows
  const step = rows.length / MAX_POINTS
  return [...Array.from({ length: MAX_POINTS - 1 }, (_, i) => rows[Math.floor(i * step)]), rows[rows.length - 1]]
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'up' | 'down' }) {
  return (
    <div className="bg-gray-800/60 rounded-lg px-2.5 py-2">
      <div className="text-[10px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={clsx(
        'text-sm font-bold tabular-nums',
        tone === 'up' ? 'text-emerald-400' : tone === 'down' ? 'text-red-400' : 'text-white',
      )}>
        {value}
      </div>
    </div>
  )
}

function EquityChart({ auto }: { auto: AutoSummary }) {
  const data = useMemo(
    () => buildSeries(
      auto.equity_history, auto.initial_balance,
      auto.benchmark.equity_history, auto.benchmark.initial_balance,
    ),
    [auto.equity_history, auto.initial_balance, auto.benchmark],
  )

  if (data.length < 2) {
    return (
      <div className="h-[160px] flex items-center justify-center text-xs text-gray-500 text-center px-6">
        The chart fills in as the experiment runs — equity is recorded every couple of
        minutes and on every entry and exit.
      </div>
    )
  }

  const last = data[data.length - 1]
  const fmtTick = (t: number) => {
    const d = new Date(t)
    const spanHours = (last.t - data[0].t) / 3.6e6
    return spanHours > 36
      ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
      : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  }

  return (
    <div>
      <ResponsiveContainer width="100%" height={170}>
        <LineChart data={data} margin={{ top: 6, right: 44, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="#1f2937" vertical={false} />
          <XAxis
            dataKey="t" type="number" domain={['dataMin', 'dataMax']} ticks={evenTicks(data.map(d => d.t), 4)}
            tickFormatter={fmtTick} tick={{ fill: '#6b7280', fontSize: 10 }}
            tickLine={false} axisLine={{ stroke: '#374151' }} interval={0}
          />
          <YAxis
            tickFormatter={v => `${v > 0 ? '+' : ''}${Number(v).toFixed(1)}%`}
            tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} axisLine={false} width={46}
            domain={['auto', 'auto']}
          />
          <ReferenceLine y={0} stroke="#4b5563" />
          <Tooltip
            cursor={{ stroke: '#6b7280', strokeWidth: 1 }}
            contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
            labelStyle={{ color: '#9ca3af' }}
            itemStyle={{ color: '#e5e7eb' }}
            labelFormatter={t => new Date(Number(t)).toLocaleString()}
            formatter={(v: number, name: string) => [`${v >= 0 ? '+' : ''}${v.toFixed(2)}%`, name]}
          />
          <Legend
            verticalAlign="top" height={20} iconType="plainline"
            wrapperStyle={{ fontSize: 11, color: '#9ca3af' }}
            formatter={value => <span className="text-gray-400">{value}</span>}
          />
          <Line
            name="Autotrader" type="monotone" dataKey="auto" stroke={AUTO_COLOR} strokeWidth={2}
            dot={false} activeDot={{ r: 4, stroke: '#111827', strokeWidth: 2 }} connectNulls isAnimationActive={false}
            label={(p: { index: number; x: number; y: number; value: number }) =>
              p.index === data.length - 1 && p.value != null ? (
                <text key="auto-end" x={p.x + 6} y={p.y} dy={4} fontSize={10} fill="#e5e7eb">
                  {p.value >= 0 ? '+' : ''}{p.value.toFixed(1)}%
                </text>
              ) : <g key={`a${p.index}`} />}
          />
          <Line
            name="My wallet" type="monotone" dataKey="main" stroke={MAIN_COLOR} strokeWidth={2}
            dot={false} activeDot={{ r: 4, stroke: '#111827', strokeWidth: 2 }} connectNulls isAnimationActive={false}
            label={(p: { index: number; x: number; y: number; value: number }) =>
              p.index === data.length - 1 && p.value != null ? (
                <text key="main-end" x={p.x + 6} y={p.y} dy={4} fontSize={10} fill="#9ca3af">
                  {p.value >= 0 ? '+' : ''}{p.value.toFixed(1)}%
                </text>
              ) : <g key={`m${p.index}`} />}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Stop ── entry ── TP1 ── TP2 with the current price as a marker. */
function TradeProgress({ trade }: { trade: AutoTrade }) {
  const stop = trade.original_stop_loss ?? trade.stop_loss
  const span = trade.take_profit_2 - stop
  const pos = (v: number) => Math.min(100, Math.max(0, ((v - stop) / span) * 100))
  return (
    <div className="relative h-1.5 bg-gray-800 rounded-full mt-2" title="stop → entry → TP1 → TP2">
      <div className="absolute inset-y-0 left-0 bg-red-900/60 rounded-l-full" style={{ width: `${pos(trade.entry_price)}%` }} />
      <div
        className="absolute inset-y-0 bg-emerald-900/60 rounded-r-full"
        style={{ left: `${pos(trade.entry_price)}%`, right: 0 }}
      />
      <div className="absolute -top-0.5 h-2.5 w-px bg-gray-400" style={{ left: `${pos(trade.take_profit_1)}%` }} />
      <div
        className="absolute -top-1 h-3.5 w-1.5 -ml-[3px] rounded-sm bg-white ring-2 ring-gray-900"
        style={{ left: `${pos(trade.current_price)}%` }}
      />
    </div>
  )
}

function OpenTrade({ trade, now, onClose }: { trade: AutoTrade; now: number; onClose: (id: string) => void }) {
  const up = trade.pnl_usd >= 0
  const left = trade.expires_at ? trade.expires_at - now / 1000 : null
  return (
    <div className="bg-gray-800/40 rounded-lg p-2.5">
      <div className="flex items-center gap-2 text-xs">
        <span className="font-bold text-white">{trade.symbol.replace('USDT', '')}</span>
        <span className={clsx(
          'font-semibold px-1.5 py-0.5 rounded text-[10px] uppercase',
          trade.direction === 'long' ? 'bg-emerald-900/40 text-emerald-300' : 'bg-red-900/40 text-red-300',
        )}>
          {trade.direction}
        </span>
        <LaneTag t={trade as unknown as Record<string, unknown>} />
        {trade.leverage != null && (
          <span className="text-[10px] font-semibold text-gray-300" title={`Isolated margin $${trade.margin_usd?.toFixed(2)}`}>
            {trade.leverage}x
          </span>
        )}
        {trade.confirmed_by && (
          <span
            className="flex items-center gap-0.5 text-[10px] text-gray-300"
            title={trade.claude_thesis || 'Confirmed by Claude before entry'}
          >
            <BrainCircuit className="w-3 h-3" />
            {trade.claude_confidence != null ? `${trade.claude_confidence}%` : '✓'}
          </span>
        )}
        {trade.stop_moved && (
          <span className="flex items-center gap-0.5 text-[10px] text-gray-300" title="TP1 hit — stop moved to entry">
            <ShieldCheck className="w-3 h-3" /> risk-free
          </span>
        )}
        <span className={clsx('ml-auto font-bold tabular-nums', up ? 'text-emerald-400' : 'text-red-400')}>
          {fmtUsd(trade.pnl_usd)} <span className="font-normal">({trade.pnl_pct >= 0 ? '+' : ''}{trade.pnl_pct.toFixed(2)}%)</span>
        </span>
        <button
          onClick={() => onClose(trade.id)}
          className="text-gray-500 hover:text-white"
          title="Close this auto trade now at market"
          aria-label={`Close ${trade.symbol} auto trade`}
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      <TradeProgress trade={trade} />
      <div className="flex justify-between mt-1.5 text-[10px] text-gray-500 tabular-nums">
        <span
          title={trade.claude_entry ? `Claude suggested ${fmtPrice(trade.claude_entry)}` : undefined}
        >
          {trade.entry_type === 'limit' ? 'limit' : 'market'} {fmtPrice(trade.entry_price)} → {fmtPrice(trade.current_price)}
        </span>
        {left != null && (
          <span className="flex items-center gap-1"><Clock className="w-3 h-3" /> expires {fmtDuration(left)}</span>
        )}
      </div>
      {trade.liquidation_price != null && trade.leverage != null && trade.leverage > 1 && (
        <div className="mt-1 text-[10px] text-gray-500 tabular-nums">
          liq {fmtPrice(trade.liquidation_price)} ·{' '}
          {(Math.abs(trade.current_price - trade.liquidation_price) / trade.current_price * 100).toFixed(1)}% away · stop{' '}
          {(Math.abs(trade.current_price - trade.stop_loss) / trade.current_price * 100).toFixed(1)}% away
          {trade.roe_pct != null && ` · ROE ${trade.roe_pct >= 0 ? '+' : ''}${trade.roe_pct.toFixed(1)}%`}
        </div>
      )}
      {(trade.adjustments?.length || trade.last_review) && (
        <div className="mt-1.5 text-[10px] text-gray-500 flex items-center gap-2">
          {!!trade.adjustments?.length && (
            <span className="text-gray-400" title={trade.adjustments.map(a => `${a.action} ${a.kind ?? ''} @ ${fmtPrice(a.price)} — ${a.reason}`).join('\n')}>
              {trade.adjustments.filter(a => a.action === 'add').length} adds · {trade.adjustments.filter(a => a.action === 'reduce').length} trims
            </span>
          )}
          {trade.last_review && (
            <span className="truncate" title={trade.last_review.reason}>
              sizing: {trade.last_review.action === 'hold' ? 'hold' : trade.last_review.action} · {fmtDuration(now / 1000 - trade.last_review.ts)} ago
            </span>
          )}
        </div>
      )}
    </div>
  )
}

/** A resting limit: how far price is from filling it, and how long it has left. */
function PendingOrder({ order, now, onCancel }: { order: AutoOrder; now: number; onCancel: (id: string) => void }) {
  const price = order.current_price
  const away = price ? ((price - order.limit_price) / price) * 100 * (order.direction === 'long' ? 1 : -1) : null
  return (
    <div className="bg-gray-800/40 rounded-lg p-2.5 text-xs">
      <div className="flex items-center gap-2">
        <span className="font-bold text-white">{order.symbol.replace('USDT', '')}</span>
        <span className={clsx(
          'font-semibold px-1.5 py-0.5 rounded text-[10px] uppercase',
          order.direction === 'long' ? 'bg-emerald-900/40 text-emerald-300' : 'bg-red-900/40 text-red-300',
        )}>
          {order.direction === 'long' ? 'buy' : 'sell'} limit
        </span>
        <span className="text-white tabular-nums">{fmtPrice(order.limit_price)}</span>
        <button
          onClick={() => onCancel(order.id)}
          className="ml-auto text-gray-500 hover:text-white"
          title="Cancel this limit order"
          aria-label={`Cancel ${order.symbol} limit order`}
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      <div className="flex justify-between mt-1.5 text-[10px] text-gray-500 tabular-nums">
        <span title={order.plan?.note}>
          now {fmtPrice(price)}{away != null && ` · ${away > 0 ? `${away.toFixed(2)}% to fill` : 'at limit'}`}
          {order.claude_entry != null && ` · Claude ${fmtPrice(order.claude_entry)}`}
        </span>
        <span className="flex items-center gap-1"><Timer className="w-3 h-3" /> {fmtDuration(order.expires_at - now / 1000)}</span>
      </div>
    </div>
  )
}

const STATUS_STYLE: Record<AutoStatus, { label: string; cls: string }> = {
  in_trade: { label: 'in trade', cls: 'text-white bg-gray-700' },
  order_pending: { label: 'limit resting', cls: 'text-sky-300 bg-sky-900/30' },
  confirming: { label: 'asking Claude', cls: 'text-sky-300 bg-sky-900/30' },
  armed: { label: 'armed', cls: 'text-yellow-300 bg-yellow-900/30' },
  vetoed: { label: 'Claude declined', cls: 'text-gray-300 bg-gray-800' },
  blocked: { label: 'held back', cls: 'text-gray-300 bg-gray-800' },
  cooldown: { label: 'cooldown', cls: 'text-gray-400 bg-gray-800' },
  busy_elsewhere: { label: 'held by other system', cls: 'text-amber-300 bg-amber-900/30' },
  watching: { label: 'no bias', cls: 'text-gray-500 bg-gray-800/60' },
}

const LANE_LABEL: Record<string, string> = { ta: 'scanner', events: 'event short', unlocks: 'unlock short', weekly: 'weekly basket', cascade: 'cascade fade', factor: 'multi-factor', reversal: 'reversal score' }
const LANE_CLS: Record<string, string> = {
  ta: 'bg-sky-900/40 text-sky-300', events: 'bg-orange-900/40 text-orange-300', unlocks: 'bg-red-900/40 text-red-300', weekly: 'bg-teal-900/40 text-teal-300',
}
/** Which lane placed the trade — so every position says which strategy it belongs to. */
function LaneTag({ t }: { t: { lane?: string; event_id?: string; lane_mult?: number; lane_why?: string } & Record<string, unknown> }) {
  const lane = t.lane ?? (t.event_id ? 'events' : 'ta')
  return (
    <span className={clsx('text-[9px] font-semibold px-1 py-0.5 rounded uppercase tracking-wide', LANE_CLS[lane] ?? 'bg-gray-800 text-gray-400')}
      title={`Lane: ${LANE_LABEL[lane] ?? lane}${t.lane_mult != null ? ` · sized ${t.lane_mult}× — ${t.lane_why ?? ''}` : ''}`}>
      {LANE_LABEL[lane] ?? lane}
    </span>
  )
}

const EXIT_STYLE: Record<string, { label: string; icon: typeof CheckCircle2 }> = {
  take_profit_2: { label: 'target', icon: CheckCircle2 },
  stop_loss: { label: 'stopped', icon: XCircle },
  breakeven: { label: 'breakeven', icon: ShieldCheck },
  tightened_stop: { label: 'tightened stop', icon: XCircle },
  expired: { label: 'expired', icon: Hourglass },
  window_exit: { label: 'left near the bell', icon: ShieldCheck },
  signal_flip: { label: 'signal flip', icon: XCircle },
  manual: { label: 'manual', icon: X },
  left_watchlist: { label: 'left watchlist', icon: LogOut },
  liquidated: { label: 'LIQUIDATED', icon: XCircle },
  trailing_stop: { label: 'trailing stop', icon: CheckCircle2 },
  stale: { label: 'no progress', icon: Hourglass },
}

export function AutoTraderPanel({
  auto, now, onPause, onClose, onCancelOrder,
}: {
  auto: AutoSummary | null
  now: number
  onPause: (paused: boolean) => void
  onClose: (id: string) => void
  onCancelOrder: (id: string) => void
}) {
  if (!auto) {
    return (
      <aside className="card text-sm text-gray-500">
        <div className="flex items-center gap-2 font-bold text-white mb-2">
          <Bot className="w-4 h-4" /> Autotrader
        </div>
        Loading the experiment wallet…
      </aside>
    )
  }

  const { stats, outcomes } = auto
  const pending = auto.states.filter(s => s.status !== 'in_trade' && s.status !== 'order_pending')
  const strongest = auto.states.reduce((m, s) => Math.abs(s.conviction ?? 0) > Math.abs(m) ? (s.conviction ?? 0) : m, 0)
  const quiet = auto.open_trades.length === 0 && auto.orders.length === 0
    && !auto.states.some(s => ['armed', 'confirming', 'blocked', 'vetoed'].includes(s.status))
    && Math.abs(strongest) < auto.config.min_conviction

  return (
    <aside className="card space-y-4">
      <div className="flex items-start gap-2">
        <Bot className="w-4 h-4 text-gray-300 mt-0.5" />
        <div>
          <div className="font-bold text-white text-sm">Autotrader</div>
          <div className="text-[11px] text-gray-500">
            Experimental · own ${auto.initial_balance.toLocaleString()} wallet · picks its own entries on the watchlist
          </div>
          <div
            className={clsx(
              'flex items-center gap-1 text-[11px] mt-0.5',
              auto.config.llm_budget_used > auto.config.llm_budget_warn ? 'text-red-400 font-semibold' : 'text-gray-500',
            )}
            title={`Rules pick the moment; Claude must agree (≥${auto.config.min_claude_confidence}%) before any entry — once per asset until its trade closes`}
          >
            <BrainCircuit className="w-3 h-3" />
            Claude checks {auto.config.llm_budget_used}/{auto.config.llm_budget_total} today
          </div>
        </div>
        <button
          onClick={() => onPause(!auto.paused)}
          disabled={!auto.enabled}
          className={clsx(
            'ml-auto flex items-center gap-1 text-xs font-semibold px-2 py-1 rounded-lg transition-all disabled:opacity-40',
            auto.paused ? 'bg-emerald-900/40 text-emerald-300 hover:bg-emerald-900/60' : 'bg-gray-800 text-gray-300 hover:bg-gray-700',
          )}
          title={auto.paused ? 'Resume new entries' : 'Stop new entries — open trades still exit normally'}
        >
          {auto.paused ? <Play className="w-3 h-3" /> : <Pause className="w-3 h-3" />}
          {!auto.enabled ? 'Disabled' : auto.paused ? 'Resume' : 'Pause'}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <Stat label="Equity" value={`$${auto.total_equity.toLocaleString(undefined, { maximumFractionDigits: 2 })}`} />
        <Stat
          label="Return"
          value={`${auto.total_return_pct >= 0 ? '+' : ''}${auto.total_return_pct.toFixed(2)}%`}
          tone={auto.total_return_pct > 0 ? 'up' : auto.total_return_pct < 0 ? 'down' : undefined}
        />
        <Stat label="Win rate" value={stats.total_trades ? `${stats.win_rate_pct}%` : '—'} />
        <Stat label="Closed / open" value={`${stats.total_trades} / ${stats.open_trades}`} />
      </div>
      <div className="text-[11px] text-gray-500 tabular-nums -mt-2">
        {auto.futures?.max_leverage ?? 20}x max · margin ${(auto.margin_used ?? 0).toFixed(0)} · open risk{' '}
        <span className={clsx(auto.config.open_risk_pct >= auto.config.max_open_risk_pct * 0.9 && 'text-red-400 font-semibold')}>
          {auto.config.open_risk_pct}% of {auto.config.max_open_risk_pct}% cap
        </span>
        {(auto.fees_paid ?? 0) > 0 && ` · fees $${(auto.fees_paid ?? 0).toFixed(2)}`}
        {(auto.funding_paid ?? 0) !== 0 && ` · funding $${(auto.funding_paid ?? 0).toFixed(2)}`}
      </div>
      {auto.maker?.enabled && (
        <div className="text-[11px] text-gray-500 tabular-nums -mt-2"
          title="Entries rest as post-only limits a hair inside the market (maker fee, no slippage). Each has a market-entry twin in the shadow book: filled trades are compared with their twin; a missed order's twin shows the trade that was lost.">
          Maker entries: {auto.maker.placed ? <>
            {auto.maker.filled}/{auto.maker.placed} filled ({auto.maker.fill_rate_pct}%)
            {auto.maker.avg_price_improvement_bps != null && <> · {auto.maker.avg_price_improvement_bps > 0 ? '+' : ''}{auto.maker.avg_price_improvement_bps} bps better than market</>}
            {auto.maker.filled_vs_twin.trades > 0 && <> · closed: {auto.maker.filled_vs_twin.maker_r}R vs market twin {auto.maker.filled_vs_twin.market_twin_r}R</>}
            {auto.maker.missed_twin_r.trades > 0 && <> · missed trades would have made {auto.maker.missed_twin_r.avg_r}R</>}
            {auto.maker.net_r_vs_market != null && <> · net vs market entries: <span className={auto.maker.net_r_vs_market >= 0 ? 'text-emerald-400' : 'text-red-400'}>{auto.maker.net_r_vs_market > 0 ? '+' : ''}{auto.maker.net_r_vs_market}R</span></>}
          </> : 'none placed yet'}
        </div>
      )}

      {auto.pullback && auto.maker?.pullback_r != null && (
        <div className="text-[11px] text-gray-500 tabular-nums -mt-2"
          title="Since 2026-10-19 entries rest as a post-only limit this far back toward the stop (stop unchanged), up to 6h — +0.075R a signal on the year's replay. Measured here against each order's market-entry twin, across the autotrader and signal books.">
          Pullback entries ({auto.maker.pullback_r}R back): {auto.pullback.placed ? <>
            {auto.pullback.filled}/{auto.pullback.placed} filled
            {auto.pullback.filled_vs_twin.trades > 0 && <> · closed {auto.pullback.filled_vs_twin.maker_r}R vs market {auto.pullback.filled_vs_twin.market_twin_r}R</>}
            {auto.pullback.missed_twin_r.trades > 0 && <> · missed would have made {auto.pullback.missed_twin_r.avg_r}R</>}
            {auto.pullback.net_r_vs_market != null && <> · net <span className={auto.pullback.net_r_vs_market >= 0 ? 'text-emerald-400' : 'text-red-400'}>{auto.pullback.net_r_vs_market > 0 ? '+' : ''}{auto.pullback.net_r_vs_market}R</span></>}
          </> : 'none placed yet'}
        </div>
      )}

      {auto.events?.enabled && (
        <div className="text-[11px] text-gray-500 tabular-nums -mt-2"
          title="Binance listing/delisting shorts from the event book, mirrored here at half size (coin short + BTC hedge) and closed when the book closes them. They skip Claude, the direction gate and the timing rules.">
          Event shorts ({auto.events.leg_pct}% a leg): {auto.events.open + auto.events.closed === 0 ? 'none yet' : <>
            {auto.events.open > 0 && `${auto.events.open} open · `}{auto.events.closed} closed, {auto.events.wins} won ·{' '}
            <span className={auto.events.net_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}>{auto.events.net_usd >= 0 ? '+' : ''}${auto.events.net_usd.toFixed(2)}</span></>}
        </div>
      )}
      {auto.lanes && (
        <section className="rounded-lg border border-gray-800 bg-gray-800/30 px-2.5 py-2 space-y-1.5" title={auto.lanes.rule}>
          <div className="flex items-baseline gap-2 text-[11px]">
            <span className="font-semibold text-gray-200">Lanes</span>
            <span className="text-gray-500">regime now <span className="text-gray-300">{auto.lanes.regime_label ?? auto.lanes.regime}</span> · sized by each lane's last {auto.lanes.cell_n} trades here{auto.lanes.net_exposure_usd != null && <> · net <span className={clsx(Math.abs(auto.lanes.net_exposure_usd) > (auto.total_equity || 1) * ((auto.lanes.net_cap_pct ?? 25) / 100) ? 'text-amber-300' : 'text-gray-300')}>{auto.lanes.net_exposure_usd >= 0 ? 'long' : 'short'} ${Math.abs(auto.lanes.net_exposure_usd).toLocaleString(undefined, { maximumFractionDigits: 0 })}</span> (cap {auto.lanes.net_cap_pct ?? 25}%)</>}</span>
          </div>
          {auto.lanes.lanes.map(l => (
            <div key={l.lane} className="grid grid-cols-[minmax(0,1fr)_3.5rem_4.5rem] items-center gap-2 text-[11px]">
              <div className="min-w-0">
                <div className="text-gray-200 truncate">{l.label}{l.open > 0 && <span className="ml-1 text-gray-500">· {l.open} open</span>}</div>
                <div className="text-[10px] text-gray-500 truncate" title={l.why}>{l.why}</div>
                {l.sides && (l.sides.long.all.n > 0 || l.sides.short.all.n > 0) && (
                  <div className="text-[10px] text-gray-600 truncate">
                    {(['long', 'short'] as const).map(d => {
                      const c = l.sides![d]
                      return <span key={d} className="mr-2" title={`${c.all.n} ${d}s in all · ${c.regime.n} in this regime`}>{d}s <span className={clsx(c.mult >= 1 ? 'text-gray-400' : 'text-amber-300')}>{c.mult}×</span>{c.all.n ? ` ${c.all.avg_r != null && c.all.avg_r > 0 ? '+' : ''}${c.all.avg_r}R · ${c.all.n}` : ''}</span>
                    })}
                  </div>
                )}
              </div>
              <span className={clsx('text-right font-semibold tabular-nums', l.enabled === false ? 'text-gray-600' : l.mult >= 1 ? 'text-emerald-300' : 'text-amber-300')}
                title={l.enabled === false ? 'This lane is not mirrored here: its book holds the only position for each signal' : undefined}>
                {l.enabled === false ? 'off' : `${l.mult}×`}
              </span>
              <span className="text-right text-gray-400 tabular-nums" title={`${l.all.n} trades in all · ${l.regime_cell.n} in this regime`}>
                {l.all.n ? `${l.all.avg_r != null && l.all.avg_r > 0 ? '+' : ''}${l.all.avg_r}R · ${l.all.n}` : 'no trades'}
              </span>
            </div>
          ))}
          <div className="text-[10px] text-gray-600">{auto.lanes.rule} Lanes marked off are traded only in their own book, so no signal is ever held twice.</div>
        </section>
      )}
      {auto.dry && (
        <div className={auto.dry.active ? 'text-xs text-amber-300' : 'text-xs text-gray-500'}>
          {auto.dry.active
            ? <>Dry market: nothing entered for {auto.dry.idle_h}h — the scanner is widened to {auto.dry.universe} coins and every scanned coin is a candidate, not only the watchlist (normal rules and guards still apply).</>
            : <>Dry-market widening arms after {auto.dry.hours}h without an entry (idle {auto.dry.idle_h}h now).</>}
        </div>
      )}
      {auto.daily_min?.enabled && (
        <div className="text-[11px] text-gray-500 tabular-nums -mt-2"
          title="If nothing has been entered for this long, the strongest setup is taken at reduced risk, skipping Claude and the timing filters. Measured separately: those filters had mostly been right.">
          Daily minimum ({auto.daily_min.hours}h, {auto.daily_min.risk_x}× risk): {auto.daily_min.closed + auto.daily_min.open === 0 ? 'none forced yet' : <>
            {auto.daily_min.open > 0 && `${auto.daily_min.open} open · `}{auto.daily_min.closed} closed, {auto.daily_min.wins} won
            {auto.daily_min.avg_r != null && <> · avg {auto.daily_min.avg_r > 0 ? '+' : ''}{auto.daily_min.avg_r}R</>} ·{' '}
            <span className={auto.daily_min.net_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}>{auto.daily_min.net_usd >= 0 ? '+' : ''}${auto.daily_min.net_usd.toFixed(2)}</span></>}
        </div>
      )}

      {quiet && (
        <div className="text-[11px] text-gray-400 bg-gray-800/40 rounded px-2 py-1.5">
          Quiet market — the strongest conviction on the watchlist is {strongest >= 0 ? '+' : ''}{strongest.toFixed(2)}; a coin needs ±{auto.config.min_conviction.toFixed(2)} before
          the rules even start timing an entry. Nothing is wrong: it waits rather than force trades.
        </div>
      )}

      <div>
        <div className="text-xs font-semibold text-gray-300 mb-1">Return vs my wallet</div>
        <EquityChart auto={auto} />
      </div>

      {stats.total_trades > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-400">
          <span className="flex items-center gap-1"><CheckCircle2 className="w-3 h-3 text-emerald-400" />{outcomes.win} won</span>
          <span className="flex items-center gap-1"><XCircle className="w-3 h-3 text-red-400" />{outcomes.loss} lost</span>
          <span className="flex items-center gap-1"><Hourglass className="w-3 h-3" />{outcomes.expired} expired</span>
          <span className="flex items-center gap-1"><ShieldCheck className="w-3 h-3" />{outcomes.breakeven} breakeven</span>
          {outcomes.flip > 0 && <span>{outcomes.flip} flipped</span>}
          {outcomes.left_watchlist > 0 && (
            <span className="flex items-center gap-1"><LogOut className="w-3 h-3" />{outcomes.left_watchlist} left watchlist</span>
          )}
        </div>
      )}

      <section>
        <div className="text-xs font-semibold text-gray-300 mb-1.5">Open positions</div>
        {auto.open_trades.length === 0 ? (
          <div className="text-xs text-gray-500">None — waiting for a good entry.</div>
        ) : (
          <div className="space-y-2">
            {auto.open_trades.map(t => <OpenTrade key={t.id} trade={t} now={now} onClose={onClose} />)}
          </div>
        )}
      </section>

      {auto.orders.length > 0 && (
        <section>
          <div className="flex items-baseline justify-between mb-1.5">
            <span className="text-xs font-semibold text-gray-300">Limit orders</span>
            {(auto.order_stats.filled + auto.order_stats.cancelled) > 0 && (
              <span className="text-[10px] text-gray-500">
                {auto.order_stats.filled} filled · {auto.order_stats.cancelled} cancelled
              </span>
            )}
          </div>
          <div className="space-y-2">
            {auto.orders.map(o => <PendingOrder key={o.id} order={o} now={now} onCancel={onCancelOrder} />)}
          </div>
        </section>
      )}

      {pending.length > 0 && (
        <section>
          <div className="text-xs font-semibold text-gray-300 mb-1.5">Waiting for entry</div>
          <ul className="space-y-1.5">
            {pending.map(s => (
              <li key={s.symbol} className="text-xs">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-white w-12">{s.base}</span>
                  <span className={clsx('text-[10px] font-semibold px-1.5 py-0.5 rounded', STATUS_STYLE[s.status].cls)}>
                    {STATUS_STYLE[s.status].label}
                    {s.direction && s.status !== 'watching' && ` ${s.direction}`}
                  </span>
                  {s.score != null && ['armed', 'blocked', 'confirming'].includes(s.status) && (
                    <span className="ml-auto text-[10px] text-gray-500 tabular-nums" title={`Enters at ${auto.config.entry_score}`}>
                      timing {s.score}/{auto.config.entry_score}
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-gray-500 pl-14 truncate" title={s.reasons.join(' · ')}>
                  {s.reasons[s.reasons.length - 1]}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {auto.closed_trades.length > 0 && (
        <section>
          <div className="text-xs font-semibold text-gray-300 mb-1.5">Recent exits</div>
          <ul className="space-y-1">
            {auto.closed_trades.slice(0, 8).map(t => {
              const exit = EXIT_STYLE[t.close_reason ?? ''] ?? { label: t.close_reason ?? '—', icon: X }
              const Icon = exit.icon
              return (
                <li
                  key={t.id}
                  className="flex items-center gap-2 text-xs"
                  title={(t.entry_reasons ?? []).join(' · ') + (t.exit_note ? ` — exit: ${t.exit_note}` : '')}
                >
                  <span className="font-semibold text-white w-12">{t.symbol.replace('USDT', '')}</span>
                  <span className="text-gray-500 w-10">{t.direction}</span>
                  <LaneTag t={t as unknown as Record<string, unknown>} />
                  <span className="flex items-center gap-1 text-gray-400">
                    <Icon className="w-3 h-3" /> {exit.label}
                  </span>
                  <span className={clsx('ml-auto tabular-nums font-semibold', t.pnl_usd >= 0 ? 'text-emerald-400' : 'text-red-400')}>
                    {fmtUsd(t.pnl_usd)}
                  </span>
                </li>
              )
            })}
          </ul>
        </section>
      )}
    </aside>
  )
}
