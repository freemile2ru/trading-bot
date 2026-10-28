import { useEffect, useState } from 'react'
import {
  AlertTriangle, ChevronRight, Eye, Pin, RefreshCw,
  TrendingDown, TrendingUp, Wifi, WifiOff, X, Zap,
} from 'lucide-react'
import clsx from 'clsx'
import type { CoinStatus, FiredSignal, ScanRow, Setup, WatchlistEntry } from '../types/signals'
import { useSignalStream } from '../hooks/useSignalStream'
import { useCoinStatus } from '../hooks/useCoinStatus'
import { AutoTraderPanel } from './AutoTraderPanel'
import { SortHeader, useSort } from './ui/Sortable'

function fmtPrice(n: number | null | undefined): string {
  if (n == null) return '—'
  const digits = n >= 1000 ? 2 : n >= 1 ? 3 : 6
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: digits })}`
}

function fmtPct(n: number | null | undefined): string {
  return n == null ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`
}

function ago(ts: number | null | undefined, now: number): string {
  if (!ts) return '—'
  const s = Math.max(0, Math.floor(now / 1000 - ts))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
}

function ConvictionBar({ value }: { value: number }) {
  const pct = Math.min(Math.abs(value) * 100, 100)
  const positive = value > 0
  return (
    <div className="flex items-center gap-2 min-w-[7rem]">
      <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div
          className={clsx('h-full rounded-full transition-all', positive ? 'bg-emerald-500' : 'bg-red-500')}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={clsx(
        'text-xs font-bold tabular-nums w-12 text-right',
        Math.abs(value) >= 0.45 ? (positive ? 'text-emerald-400' : 'text-red-400') : 'text-gray-400',
      )}>
        {value >= 0 ? '+' : ''}{value.toFixed(2)}
      </span>
    </div>
  )
}

function SetupChips({ setups, limit = 3 }: { setups: Setup[]; limit?: number }) {
  if (!setups.length) return <span className="text-xs text-gray-600">—</span>
  const shown = setups.slice(0, limit)
  return (
    <div className="flex flex-wrap gap-1">
      {shown.map(s => (
        <span
          key={s.name}
          title={`${s.label} (${s.timeframe}) — ${s.reasons.join(' · ')}`}
          className={clsx(
            'text-[10px] font-semibold px-1.5 py-0.5 rounded border whitespace-nowrap',
            s.direction === 'long'
              ? 'bg-emerald-900/30 text-emerald-400 border-emerald-800/50'
              : 'bg-red-900/30 text-red-400 border-red-800/50',
          )}
        >
          {s.label}
        </span>
      ))}
      {setups.length > limit && (
        <span
          className="text-[10px] text-gray-400 px-1 py-0.5 cursor-help"
          title={setups.slice(limit).map(s => `${s.label} (${s.timeframe})`).join('\n')}
        >
          +{setups.length - limit}
        </span>
      )}
    </div>
  )
}

// A signal is only actionable for as long as its setup lasts. Past this the entry and
// stop are priced off stale levels, so the card is removed — acting on an expired
// signal costs real money. Mirrors SIGNAL_LIVE_SECONDS in the scheduler.
const STALE_AFTER_SECONDS = 4 * 3600

const SYSTEM_NAME = { main: 'Main', auto: 'Auto' } as const

/** How many signals this coin has fired (24h / 7d / all time), from the permanent log. */
function SignalTally({ status, now }: { status?: CoinStatus; now: number }) {
  const t = status?.signals
  if (!t) return null
  return (
    <span
      className="badge-neutral tabular-nums whitespace-nowrap"
      title={`Signals fired: ${t.d1} in 24h · ${t.d7} in 7d · ${t.total} all time\n${t.buys} BUY / ${t.sells} SELL · last ${ago(t.last, now)} ago`}
    >
      {t.total} signal{t.total === 1 ? '' : 's'}{t.d1 > 0 && ` · ${t.d1} today`}
    </span>
  )
}

/** One chip per open position (or resting entry order) in any system, paper or live. */
function InTrade({ status }: { status?: CoinStatus }) {
  if (!status?.trades.length) return null
  return (
    <>
      {status.trades.map((t, i) => {
        const long = t.direction === 'long'
        return (
          <span
            key={i}
            className={clsx(
              'inline-flex items-center gap-1 whitespace-nowrap px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide border',
              t.pending ? 'border-gray-600 text-gray-400'
                : long ? 'border-emerald-700 text-emerald-400 bg-emerald-900/20' : 'border-red-700 text-red-400 bg-red-900/20',
            )}
            title={`${t.pending ? 'Entry order resting' : 'Open position'} in ${SYSTEM_NAME[t.system]} (${t.mode}) — ${t.direction} @ ${fmtPrice(t.entry)}${t.pnl_usd != null ? ` · P&L ${t.pnl_usd >= 0 ? '+' : ''}$${t.pnl_usd.toFixed(2)}` : ''}`}
          >
            {t.mode === 'live' && <span className="w-1.5 h-1.5 rounded-full bg-yellow-400" />}
            {t.pending ? 'order' : 'in trade'} · {SYSTEM_NAME[t.system]} {t.mode === 'live' ? 'LIVE' : 'paper'} {long ? '▲' : '▼'}
            {t.pnl_usd != null && !t.pending && (
              <span className={clsx('normal-case tabular-nums', t.pnl_usd >= 0 ? 'text-emerald-300' : 'text-red-300')}>
                {t.pnl_usd >= 0 ? '+' : ''}{t.pnl_usd.toFixed(2)}
              </span>
            )}
          </span>
        )
      })}
    </>
  )
}

function FiredCard({
  signal, onTrade, trading, now, confirmations = 1, firstFiredAt, status,
}: {
  signal: FiredSignal
  status?: CoinStatus
  onTrade: (symbol: string) => void
  trading: string | null
  now: number
  confirmations?: number
  firstFiredAt?: number
}) {
  const buy = signal.action === 'BUY'
  const risks = Array.isArray(signal.risk_factors) ? signal.risk_factors : [signal.risk_factors]

  return (
    <div className={clsx(
      'card border-l-4 transition-opacity',
      buy ? 'border-l-emerald-500' : 'border-l-red-500',
    )}>
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex items-center gap-2.5">
          <div className={clsx(
            'w-9 h-9 rounded-lg flex items-center justify-center shrink-0',
            buy ? 'bg-emerald-600' : 'bg-red-600',
          )}>
            {buy ? <TrendingUp className="w-5 h-5 text-white" /> : <TrendingDown className="w-5 h-5 text-white" />}
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="font-black text-white">{signal.action} {signal.base}</span>
              <span className={buy ? 'badge-buy' : 'badge-sell'}>{signal.confidence}%</span>
              {confirmations > 1 && (
                <span
                  className="badge-neutral"
                  title={`Claude confirmed this ${confirmations} times in the live window — first ${ago(firstFiredAt, now)} ago. Levels shown are from the latest.`}
                >
                  confirmed ×{confirmations}
                </span>
              )}
              <SignalTally status={status} now={now} />
            </div>
            {status?.trades.length ? <div className="flex flex-wrap gap-1 mt-1"><InTrade status={status} /></div> : null}
            <div className="text-xs text-gray-500">
              fired {ago(signal.fired_at, now)} ago · {signal.timeframe} · hold {signal.hold_duration}
            </div>
          </div>
        </div>
        <button
          onClick={() => onTrade(signal.symbol)}
          disabled={trading === signal.symbol}
          className={clsx(
            'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-bold text-white transition-all shrink-0',
            'disabled:opacity-40 disabled:cursor-not-allowed',
            buy ? 'bg-emerald-600 hover:bg-emerald-500' : 'bg-red-600 hover:bg-red-500',
          )}
        >
          <Zap className="w-3.5 h-3.5" />
          {trading === signal.symbol ? 'Opening…' : 'Paper trade'}
        </button>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">
        {([
          ['Entry', signal.entry_price, 'text-white'],
          ['Stop', signal.stop_loss, 'text-red-400'],
          ['TP1', signal.take_profit_1, 'text-emerald-400'],
          ['TP2', signal.take_profit_2, 'text-emerald-400'],
        ] as const).map(([label, value, color]) => (
          <div key={label} className="bg-gray-800/50 rounded-lg px-2.5 py-1.5">
            <div className="text-[10px] text-gray-500 uppercase tracking-wide">{label}</div>
            <div className={clsx('text-sm font-bold tabular-nums', color)}>{fmtPrice(value)}</div>
          </div>
        ))}
      </div>

      <SetupChips setups={signal.setups} limit={5} />

      {signal.thesis && (
        <p className="text-sm text-gray-300 leading-relaxed mt-3">{signal.thesis}</p>
      )}

      {risks.filter(Boolean).length > 0 && (
        <div className="flex items-start gap-1.5 mt-2 text-xs text-yellow-500/90">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <span>{risks.filter(Boolean).join(' · ')}</span>
        </div>
      )}
    </div>
  )
}

export function AllSignals({
  onSelectSymbol, onTrade, trading,
}: {
  onSelectSymbol: (symbol: string) => void
  onTrade: (symbol: string) => void
  trading: string | null
}) {
  const {
    rows, fired, watchlist, meta, connection, scanning, error, rescan, unpin,
    auto, setAutoPaused, closeAutoTrade, cancelAutoOrder,
  } = useSignalStream()
  const coins = useCoinStatus()
  const inTrade = Object.entries(coins).filter(([, c]) => c.trades.length > 0)

  // Drives the relative timestamps without re-rendering anything else.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const watched = new Set(watchlist.map(w => w.symbol))

  // Only watchlist symbols are shown. The scanner still scores all ten every sweep —
  // that is how it decides what belongs on the list — but the other coins have nothing
  // actionable about them, so surfacing them here is noise. A fired signal keeps its
  // symbol on the watchlist for as long as it is tradeable, so nothing live is hidden.
  const watchedRows = rows.filter(r => watched.has(r.symbol))
  // Column sorting; the default order (no sort chosen) is by |conviction| from the stream.
  const table = useSort(watchedRows, {
    coin: r => r.base, price: r => r.price, change: r => r.change_24h, vsbtc: r => (r.base === 'BTC' ? null : r.fa?.rs_vs_btc_24h),
    conviction: r => r.conviction, news: r => r.news?.score, conf: r => r.confidence, setups: r => r.setups?.length ?? 0,
    why: r => watchlist.find(w => w.symbol === r.symbol)?.added_at, age: r => r.scanned_at,
  })
  const TH = ({ k, children, right, title }: { k: string; children: React.ReactNode; right?: boolean; title?: string }) => (
    <th className={clsx('font-semibold px-3 py-2.5', right ? 'text-right' : 'text-left')}>
      <SortHeader k={k} sort={table.sort} onSort={table.toggle} align={right ? 'right' : 'left'} title={title}>{children}</SortHeader>
    </th>
  )
  // When nothing is live: how close the strongest coins are to the firing bar, and what each still lacks.
  const minConv = meta?.scanner?.min_conviction ?? 0.45
  const minConf = meta?.scanner?.min_confidence ?? 65
  const nearMisses = [...rows]
    .sort((a, b) => Math.abs(b.conviction) - Math.abs(a.conviction))
    .slice(0, 5)
    .map(row => {
      const missing: string[] = []
      if (Math.abs(row.conviction) < minConv) missing.push(`conviction ±${minConv.toFixed(2)} (${Math.abs(row.conviction).toFixed(2)})`)
      if (row.confidence < minConf) missing.push(`confidence ${minConf}%`)
      if (!row.conviction_detail?.aligned_setups?.length) missing.push('a setup in its direction')
      if ((row.tf_alignment?.multiplier ?? 1) < 1) missing.push('timeframes agreeing')
      return { row, missing }
    })
  const firedToday = fired.filter(f => now / 1000 - f.fired_at <= 86400).length
  const watchedFired = fired.filter(f => watched.has(f.symbol))

  // Expired signals are dropped entirely — only ones still inside their window show.
  const liveAll = watchedFired
    .filter(f => now / 1000 - f.fired_at <= STALE_AFTER_SECONDS)
    .sort((a, b) => b.fired_at - a.fired_at)
  // One card per coin and direction: the newest signal, with how many times it was confirmed.
  const grouped = new Map<string, { signal: FiredSignal; count: number; first: number }>()
  for (const f of liveAll) {
    const key = `${f.symbol}-${f.action}`
    const g = grouped.get(key)
    if (g) {
      g.count += 1
      g.first = Math.min(g.first, f.fired_at)
    } else {
      grouped.set(key, { signal: f, count: 1, first: f.fired_at })
    }
  }
  const liveCards = [...grouped.values()]
  const liveFired = liveCards.map(g => g.signal)
  const selectionTitle = (meta?.selection ?? [])
    .filter(c => c.selected)
    .map(c => `#${c.rank} ${c.base} — score ${c.tradeability.toFixed(2)} · range ${c.range_pct}% · vol $${Math.round(c.quote_volume / 1e6)}M`)
    .join('\n') || 'Ranked by live liquidity, range, momentum and trend coherence'
  const nextIn = meta?.next_sweep_at ? Math.max(0, Math.round(meta.next_sweep_at - now / 1000)) : null

  return (
    <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px] gap-5 items-start">
    <div className="space-y-5 min-w-0">
      {/* Status bar */}
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <div className={clsx(
          'flex items-center gap-1.5 px-2 py-1 rounded-lg font-semibold',
          connection === 'live' ? 'bg-emerald-900/30 text-emerald-400'
            : connection === 'connecting' ? 'bg-gray-800 text-gray-400'
            : 'bg-red-900/30 text-red-400',
        )}>
          {connection === 'offline' ? <WifiOff className="w-3.5 h-3.5" /> : <Wifi className="w-3.5 h-3.5" />}
          {connection === 'live' ? 'Live' : connection === 'connecting' ? 'Connecting…' : 'Reconnecting…'}
        </div>

        <span className="text-gray-500">
          <span
            className="underline decoration-dotted decoration-gray-700 underline-offset-2 cursor-help"
            title={selectionTitle}
          >
            {meta?.pool_size
              ? `best ${meta.universe_size} tradeable of top ${meta.pool_size}`
              : `${rows.length} scanned`}
          </span>
          {' '}· watching {watchedRows.length} · updated {ago(meta?.updated_at, now)} ago
          {nextIn != null && ` · next sweep ${nextIn}s`}
        </span>

        {meta?.llm_budget_total != null && (
          <span className="text-gray-600" title="Claude Opus calls used today">
            AI budget {meta.llm_budget_used}/{meta.llm_budget_total}
          </span>
        )}

        <button
          onClick={rescan}
          disabled={scanning}
          className="ml-auto flex items-center gap-1.5 bg-gray-800 hover:bg-gray-700 text-white px-2.5 py-1.5 rounded-lg font-semibold transition-all disabled:opacity-50"
        >
          <RefreshCw className={clsx('w-3.5 h-3.5', scanning && 'animate-spin')} />
          {scanning ? 'Scanning…' : 'Scan now'}
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 bg-red-900/30 border border-red-800 rounded-lg px-4 py-3 text-red-400 text-sm">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          {error}
        </div>
      )}

      {/* Fired signals — live ones only */}
      <section className="space-y-2">
        <h2 className="flex items-center gap-2 text-sm font-bold text-white">
          <Zap className="w-4 h-4 text-yellow-500" />
          Trade signals
          {liveFired.length > 0 && (
            <span className="text-xs font-normal text-gray-500">{liveFired.length} active</span>
          )}
        </h2>
        {liveFired.length === 0 ? (
          <div className="card text-sm text-gray-500 space-y-3">
            <p>
              No confirmed signals live right now. A signal fires only when conviction reaches ±{(meta?.scanner?.min_conviction ?? 0.45).toFixed(2)},
              confidence {meta?.scanner?.min_confidence ?? 65}% and a named setup lines up — then Claude confirms it.
              {firedToday > 0 && <> {firedToday} fired in the last 24h; each stays here for {STALE_AFTER_SECONDS / 3600}h.</>}
            </p>
            {nearMisses.length > 0 && (
              <div>
                <div className="text-[11px] uppercase tracking-wider text-gray-500 mb-1">Closest to firing</div>
                <div className="space-y-1">
                  {nearMisses.map(n => (
                    <div key={n.row.symbol} className="flex flex-wrap items-center gap-x-3 text-xs">
                      <span className="font-semibold text-gray-200 w-12">{n.row.base}</span>
                      <span className={n.row.conviction > 0 ? 'text-emerald-400' : n.row.conviction < 0 ? 'text-red-400' : 'text-gray-400'}>
                        conviction {n.row.conviction > 0 ? '+' : ''}{n.row.conviction.toFixed(2)}
                      </span>
                      <span className="text-gray-400">confidence {n.row.confidence}%</span>
                      <span className="text-gray-500">{n.missing.length ? `needs ${n.missing.join(', ')}` : 'at the bar — waiting for Claude'}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {liveCards.map(({ signal: s, count, first }) => (
              <FiredCard
                key={`${s.symbol}-${s.action}`}
                signal={s}
                onTrade={onTrade}
                trading={trading}
                now={now}
                confirmations={count}
                firstFiredAt={first}
                status={coins[s.symbol]}
              />
            ))}
          </div>
        )}
      </section>

      {/* Every coin with an open position anywhere — watched or not */}
      {inTrade.length > 0 && (
        <div className="card py-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
          <span className="font-bold text-white">In trade</span>
          {inTrade.map(([sym, c]) => (
            <button key={sym} onClick={() => onSelectSymbol(sym)} className="flex items-center gap-1.5 hover:opacity-80">
              <span className="font-bold text-gray-200">{sym.replace(/USDT$/, '')}</span>
              <InTrade status={c} />
            </button>
          ))}
        </div>
      )}

      {/* Watchlist scan */}
      <section className="space-y-2">
        <h2 className="flex items-center gap-2 text-sm font-bold text-white">
          <Eye className="w-4 h-4 text-blue-400" />
          Watchlist
          <span className="text-xs font-normal text-gray-500">
            ranked by conviction · re-checked every {meta?.scanner?.watchlist_interval ?? 60}s
          </span>
        </h2>

        <div className="card p-0 overflow-x-auto">
          <table className="w-full text-sm min-w-[56rem]">
            <thead>
              <tr className="text-[11px] uppercase tracking-wide text-gray-500 border-b border-gray-800">
                <TH k="coin">Coin</TH>
                <TH k="price" right>Price</TH>
                <TH k="change" right>24h</TH>
                <TH k="vsbtc" right title="24h move minus BTC's">vs BTC</TH>
                <TH k="conviction">Conviction</TH>
                <TH k="news" right title="News score, −1…+1: Claude-classified headlines weighted by importance and age (News tab)">News</TH>
                <TH k="conf" right>Conf</TH>
                <th className="text-left font-semibold px-3 py-2.5 min-w-[12rem]"><SortHeader k="setups" sort={table.sort} onSort={table.toggle}>Setups</SortHeader></th>
                <TH k="why" title="Sorted by how long the coin has been watched">Why watched</TH>
                <TH k="age" right>Age</TH>
                <th className="w-16" />
              </tr>
            </thead>
            <tbody>
              {watchedRows.length === 0 && (
                <tr>
                  <td colSpan={11} className="px-3 py-8 text-center text-gray-500">
                    {scanning
                      ? 'Scanning the market…'
                      : rows.length === 0
                        ? 'Waiting for the first sweep…'
                        : `Nothing on the watchlist — all ${rows.length} coins scanned, none meeting the criteria yet.`}
                  </td>
                </tr>
              )}
              {table.rows.map(row => (
                <ScanTableRow
                  key={row.symbol}
                  row={row}
                  now={now}
                  entry={watchlist.find(w => w.symbol === row.symbol)}
                  status={coins[row.symbol]}
                  onSelect={onSelectSymbol}
                  onRemove={unpin}
                />
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>

    {/* Autonomous experiment — its own wallet, its own entry timing */}
    <div className="xl:sticky xl:top-4">
      <AutoTraderPanel
        auto={auto} now={now} onPause={setAutoPaused} onClose={closeAutoTrade} onCancelOrder={cancelAutoOrder}
      />
    </div>
    </div>
  )
}

function ScanTableRow({
  row, now, entry, onSelect, onRemove, status,
}: {
  row: ScanRow
  status?: CoinStatus
  now: number
  entry?: WatchlistEntry
  onSelect: (s: string) => void
  onRemove: (s: string) => void
}) {
  const signalGrade = Math.abs(row.conviction) >= 0.45

  return (
    <tr
      onClick={() => onSelect(row.symbol)}
      className={clsx(
        'border-b border-gray-800/60 last:border-0 cursor-pointer transition-colors hover:bg-gray-800/40',
        signalGrade && 'bg-gray-800/20',
      )}
    >
      <td className="px-3 py-2.5">
        <div className="flex items-center gap-2">
          <span className="relative flex h-1.5 w-1.5 shrink-0" title="re-checked every minute">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-500 opacity-70" />
            <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-blue-500" />
          </span>
          <span className="font-bold text-white">{row.base}</span>
          <span className="text-[10px] text-gray-600">#{row.rank}</span>
          {entry?.pinned && <Pin className="w-3 h-3 text-blue-400" />}
          <SignalTally status={status} now={now} />
        </div>
        {status?.trades.length ? <div className="flex flex-wrap gap-1 mt-1"><InTrade status={status} /></div> : null}
      </td>

      <td className="px-3 py-2.5 text-right tabular-nums text-gray-200">{fmtPrice(row.price)}</td>

      <td className={clsx(
        'px-3 py-2.5 text-right tabular-nums font-semibold',
        (row.change_24h ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400',
      )}>
        {fmtPct(row.change_24h)}
      </td>

      <td className={clsx(
        'px-3 py-2.5 text-right tabular-nums text-xs',
        row.fa.rs_vs_btc_24h > 0 ? 'text-emerald-400/80'
          : row.fa.rs_vs_btc_24h < 0 ? 'text-red-400/80' : 'text-gray-600',
      )}>
        {row.base === 'BTC' ? '—' : fmtPct(row.fa.rs_vs_btc_24h)}
      </td>

      <td className="px-3 py-2.5"><ConvictionBar value={row.conviction} /></td>

      <td className="px-3 py-2.5 text-right tabular-nums text-xs whitespace-nowrap"
          title={row.news?.stories_6h != null ? `${row.news.stories_6h} stories in 6 h · ${row.news.burst ?? 1}× usual rate` : undefined}>
        {row.news?.score == null ? <span className="text-gray-600">—</span> : (
          <span className={Math.abs(row.news.score) < 0.05 ? 'text-gray-500' : row.news.score > 0 ? 'text-emerald-400' : 'text-red-400'}>
            {row.news.score > 0 ? '+' : ''}{row.news.score.toFixed(2)}
            {(row.news.burst ?? 0) >= 2 && (row.news.stories_6h ?? 0) >= 3 && <span className="ml-1 text-amber-300">●</span>}
          </span>
        )}
      </td>

      <td className={clsx(
        'px-3 py-2.5 text-right tabular-nums font-semibold',
        row.confidence >= 65 ? 'text-white' : 'text-gray-500',
      )}>
        {row.confidence}%
      </td>

      <td className="px-3 py-2.5 min-w-[12rem]"><SetupChips setups={row.setups} limit={1} /></td>

      <td className="px-3 py-2.5 max-w-[16rem]">
        <div className="text-xs text-gray-400 truncate" title={entry?.reasons?.join(' · ')}>
          {entry?.reasons?.[0] ?? '—'}
        </div>
        <div className="text-[10px] text-gray-600 capitalize">
          {row.regime}{row.best_timeframe ? ` · ${row.best_timeframe}` : ''}
          {entry && ` · watching ${ago(entry.added_at, now)}`}
        </div>
      </td>

      <td className="px-3 py-2.5 text-right text-xs text-gray-600 tabular-nums">
        {ago(row.scanned_at, now)}
      </td>

      <td className="pr-2">
        <div className="flex items-center gap-1 justify-end text-gray-700">
          <button
            onClick={e => { e.stopPropagation(); onRemove(row.symbol) }}
            title="Remove from watchlist"
            className="p-1 rounded hover:bg-gray-700 hover:text-gray-300 transition-colors"
          >
            <X className="w-3.5 h-3.5" />
          </button>
          <ChevronRight className="w-4 h-4" />
        </div>
      </td>
    </tr>
  )
}
