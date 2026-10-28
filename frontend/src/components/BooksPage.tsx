import { useCallback, useEffect, useState } from 'react'
import { Archive, Layers } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import { LedgerSwitch } from './ui/LedgerSwitch'
import { useLedger } from '../hooks/useLedger'
import { GuardsPanel } from './GuardsPanel'
import type { Book, BookVerdict, BooksResponse } from '../types/books'

const KIND: Record<Book['kind'], { label: string; cls: string }> = {
  manual: { label: 'Manual', cls: 'bg-blue-900/50 text-blue-300' },
  automated: { label: 'Automated', cls: 'bg-violet-900/50 text-violet-300' },
  experiment: { label: 'Experiment', cls: 'bg-emerald-900/50 text-emerald-300' },
  scalper: { label: 'Scalper', cls: 'bg-amber-900/50 text-amber-300' },
}
const ago = (ts: number | null) => {
  if (!ts) return 'never'
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60))
  return m < 60 ? `${m}m ago` : m < 48 * 60 ? `${Math.floor(m / 60)}h ago` : `${Math.floor(m / 1440)}d ago`
}
let memo: BooksResponse | null = null          // instant render when the tab is reopened

function Switch({ on, disabled, busy, onChange, label }: { on: boolean; disabled?: boolean; busy?: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button role="switch" aria-checked={on} aria-label={label} disabled={disabled || busy} onClick={() => onChange(!on)}
      className={clsx('relative w-11 h-6 rounded-full transition-colors shrink-0 disabled:opacity-40 disabled:cursor-not-allowed',
        on ? 'bg-emerald-500' : 'bg-gray-600', busy && 'animate-pulse')}>
      <span className={clsx('absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform', on && 'translate-x-5')} />
    </button>
  )
}

const VERDICT: Record<BookVerdict['status'], [string, string]> = {
  collecting: ['Collecting', 'bg-gray-800 text-gray-300'],
  pass: ['Pass', 'bg-emerald-900/60 text-emerald-300'],
  unclear: ['Unclear', 'bg-amber-900/50 text-amber-300'],
  fail: ['Fail · retire', 'bg-red-900/60 text-red-300'],
  no_edge: ['No edge · retire', 'bg-red-900/60 text-red-300'],
}
const signed = (x: number, d = 2) => `${x > 0 ? '+' : ''}${x.toFixed(d)}`

function VerdictLine({ v }: { v: BookVerdict }) {
  const [label, cls] = VERDICT[v.status]
  const pct = v.rule === 'trades' ? Math.min(100, ((v.trades ?? 0) / (v.target ?? 40)) * 100)
    : Math.min(100, ((v.days ?? 0) / (v.target_days ?? 60)) * 100)
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]"
      title={`Pass/fail rule registered ${v.registered}, before the results it judges. Advice only — nothing is switched off automatically.`}>
      <span className={clsx('px-1.5 py-0.5 rounded font-semibold', cls)}>{label}</span>
      {v.status === 'collecting' && (
        <span className="w-24 h-1.5 rounded bg-gray-800 overflow-hidden" aria-label={`${Math.round(pct)}% of the way to a verdict`}>
          <span className="block h-full bg-blue-500" style={{ width: `${pct}%` }} />
        </span>
      )}
      {v.rule === 'trades' ? (
        <span className="text-gray-400 tabular-nums">
          {v.avg_r != null && <>avg {signed(v.avg_r)}R</>}
          {v.ci && <> · {Math.round((v.level ?? 0.9) * 100)}% range {signed(v.ci[0])} to {signed(v.ci[1])}R</>}
        </span>
      ) : (
        <span className="text-gray-400 tabular-nums">
          funding {v.funding_bps_day != null ? signed(v.funding_bps_day, 2) : '—'} bps/day vs {v.expected_bps_day ?? '—'} expected
        </span>
      )}
      <span className="text-gray-500">{v.note}</span>
    </div>
  )
}

export function BookCard({ b, busy, onToggle }: { b: Book; busy: boolean; onToggle: (on: boolean) => void }) {
  const kind = KIND[b.kind]
  return (
    <section className={clsx('card space-y-3 transition-opacity', !b.enabled && 'opacity-70')}>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-white">{b.label}</h3>
            <span className={clsx('px-1.5 py-0.5 rounded text-[10px] font-semibold', kind.cls)}>{kind.label}</span>
            {b.mode && (
              <a href="#live" title="Live mode is set on the Live tab"
                className={clsx('px-1.5 py-0.5 rounded text-[10px] font-bold', b.mode === 'live' ? 'bg-rose-600 text-white' : 'bg-gray-800 text-gray-400')}>
                {b.mode === 'live' ? 'LIVE' : 'PAPER'}
              </a>
            )}
          </div>
          <p className="text-xs text-gray-400">{b.description}</p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Switch on={b.enabled} disabled={!!b.unavailable} busy={busy} onChange={onToggle} label={`${b.label} on or off`} />
          <span className={clsx('text-[10px] font-semibold', b.enabled ? 'text-emerald-400' : 'text-gray-500')}>
            {b.unavailable ? 'unavailable' : b.enabled ? 'ON' : 'OFF'}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 text-xs">
        {([
          ['Open', b.open],
          ['Closed', b.closed],
          ['Win rate', b.win_rate == null ? '—' : `${b.win_rate}%`],
          ['Net P&L', <span className={b.net_pnl > 0 ? 'text-emerald-400' : b.net_pnl < 0 ? 'text-red-400' : 'text-gray-300'}>{fmtUsdSigned(b.net_pnl)}</span>],
          ['Trades 24h · 7d', `${b.trades_24h} · ${b.trades_7d}`],
          ['Last entry', ago(b.last_opened)],
        ] as const).map(([k, v]) => (
          <div key={k} className="bg-gray-800/40 rounded px-2 py-1.5">
            <div className="text-[10px] text-gray-500">{k}</div>
            <div className="tabular-nums text-gray-100">{v}</div>
          </div>
        ))}
      </div>

      {b.verdict && <VerdictLine v={b.verdict} />}

      <div className="space-y-1">
        {b.wallets.map(w => (
          <div key={w.system} className={clsx('flex flex-wrap items-center gap-x-3 gap-y-0.5 rounded px-2 py-1 text-[11px]',
            w.ledger === 'live' ? 'bg-rose-950/40 border border-rose-900/40' : 'bg-gray-800/30')}>
            <span className={clsx('font-semibold', w.ledger === 'live' ? 'text-rose-300' : 'text-gray-300')}>{w.label}</span>
            <span className="text-gray-500">balance <span className="text-gray-200 tabular-nums">{fmtUsd(w.balance)}</span></span>
            <span className="text-gray-500">equity <span className="text-gray-200 tabular-nums">{fmtUsd(w.equity)}</span></span>
            {w.return_pct != null ? (
              <span className="text-gray-500">since start <span className={clsx('tabular-nums', w.return_pct > 0 ? 'text-emerald-400' : w.return_pct < 0 ? 'text-red-400' : 'text-gray-300')}>
                {w.return_pct > 0 ? '+' : ''}{w.return_pct.toFixed(2)}%</span> of {fmtUsd(w.initial, 0)}</span>
            ) : w.ledger === 'live' && <span className="text-gray-500">mirrors the Binance account — its trades' P&L is above</span>}
          </div>
        ))}
      </div>

      <p className="text-[11px] text-gray-500">
        {b.unavailable ? <>Can't be switched here — {b.unavailable}.</>
          : b.enabled ? <>Off means: {b.off_means}</>
          : <>Off{b.switched_at ? ` since ${ago(b.switched_at)}` : ''}: {b.off_means}{b.open > 0 && <span className="text-amber-300"> {b.open} open position{b.open > 1 ? 's are' : ' is'} still managed until closed.</span>}</>}
        {b.live_trades > 0 && <> · the record includes {b.live_trades} trade{b.live_trades > 1 ? 's' : ''} on the live ledger</>}
      </p>
    </section>
  )
}

export function BooksPage() {
  const [data, setData] = useState<BooksResponse | null>(memo)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [ledger] = useLedger()
  const load = useCallback(() => api.books.list(ledger).then(d => { memo = d; setData(d); setError(null) })
    .catch(e => setError(e instanceof Error ? e.message : 'Could not load the books')), [ledger])
  useEffect(() => {
    load()
    const id = setInterval(load, 30_000)
    return () => clearInterval(id)
  }, [load])

  async function toggle(b: Book, on: boolean) {
    if (!on && b.open > 0 && !confirm(`Switch ${b.label} off?\n\nIt stops opening new trades. Its ${b.open} open position(s) keep their stops and targets until they close — close them from the Trades tab if you want out now.`)) return
    setBusy(b.name); setError(null)
    try {
      const updated = await api.books.set(b.name, on)
      setData(d => d && { ...d, books: d.books.map(x => (x.name === b.name ? updated : x)) })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Switch failed')
    } finally {
      setBusy(null)
    }
  }

  const on = data?.books.filter(b => b.enabled).length ?? 0
  return (
    <div className="space-y-4">
      <section className="card flex flex-wrap items-center gap-3">
        <Layers className="w-5 h-5 text-blue-400" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold text-white">Books</div>
          <p className="text-[11px] text-gray-500">
            Every system that trades, and a switch for each. Off stops <b>new entries</b> only — open positions keep being managed.
            Every paper book trades its own wallet; Main and Autotrader also have a live ledger. The record counts the ledger chosen on the right.
            Live mode (real orders on Binance) is separate, on the <a href="#live" className="text-blue-300 hover:underline">Live tab</a>.
          </p>
        </div>
        {data && <span className="text-xs text-gray-400">{on} of {data.books.length} on</span>}
        <LedgerSwitch />
      </section>
      {error && <p className="text-xs text-red-400">{error}</p>}

      <GuardsPanel />

      {!data ? (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">{[0, 1, 2, 3].map(i => <div key={i} className="card h-44 animate-pulse" />)}</div>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          {data.books.map(b => <BookCard key={b.name} b={b} busy={busy === b.name} onToggle={v => toggle(b, v)} />)}
        </div>
      )}

      {data && data.retired.length > 0 && (
        <section className="card space-y-2">
          <div className="flex items-center gap-2 text-sm font-semibold text-gray-300"><Archive className="w-4 h-4" /> Retired</div>
          {data.retired.map(r => (
            <div key={r.name} className="flex flex-wrap items-baseline gap-x-3 text-xs">
              <span className="font-semibold text-gray-200">{r.label}</span>
              <span className="text-gray-500">{r.description}</span>
              <span className="ml-auto text-gray-400">{r.closed} trades · <span className={r.net_pnl < 0 ? 'text-red-400' : 'text-emerald-400'}>{fmtUsdSigned(r.net_pnl)}</span></span>
            </div>
          ))}
          <p className="text-[11px] text-gray-500">Retired books can't be switched back on — their code was removed. Details on the <a href="#retired" className="text-blue-300 hover:underline">Retired tab</a>.</p>
        </section>
      )}
    </div>
  )
}
