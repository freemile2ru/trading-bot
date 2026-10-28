import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, KeyRound, XOctagon, RefreshCw, ShieldCheck } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd } from '../utils/format'
import { DataTable, type Column } from './ui/DataTable'
import type { LiveActivity, LiveStatus, LiveSystem, LiveSystemStatus } from '../types/live'

const LABEL: Record<LiveSystem, string> = { main: 'Main wallet', auto: 'Autotrader' }
const WHAT: Record<LiveSystem, string> = {
  main: 'Your own trades from the Signal tab and All Signals "Trade" buttons.',
  auto: 'Autonomous entries, exits, adds and trims (Claude confirms each entry).',
}

const when = (ts: number) => new Date(ts * 1000).toLocaleString('en-US', {
  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})

/** Live trading control: per-system paper/live switch, account balances, kill switch, activity. */
export function LivePage({ onChange }: { onChange?: () => void }) {
  const [status, setStatus] = useState<LiveStatus | null>(null)
  const [activity, setActivity] = useState<LiveActivity | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [killing, setKilling] = useState(false)

  const load = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([api.live.status(), api.live.activity(40)])
      setStatus(s)
      setActivity(a)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load live status')
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 15_000)
    return () => clearInterval(id)
  }, [load])

  async function kill() {
    if (!confirm('KILL SWITCH: every system goes back to paper, and all live orders are cancelled and live positions closed at market on Binance. Continue?')) return
    setKilling(true)
    try {
      await api.live.kill()
      await load()
      onChange?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Kill switch failed')
    } finally {
      setKilling(false)
    }
  }

  if (!status) return error ? <Banner tone="red">{error}</Banner> : <div className="card animate-pulse h-48" />

  const anyLive = Object.values(status.systems).some(s => s.live)
  return (
    <div className="space-y-4">
      {!status.allowed ? (
        <Banner tone="gray" icon={<ShieldCheck className="w-4 h-4 shrink-0" />}>
          Live trading is <b>off</b>. Nothing can be sent to Binance until <code>LIVE_TRADING_ALLOWED=true</code> is
          set in <code>backend/.env</code> (and the backend restarted). Removing it later blocks every live order at once.
        </Banner>
      ) : (
        <Banner tone={status.env === 'mainnet' ? 'red' : 'amber'} icon={<AlertTriangle className="w-4 h-4 shrink-0" />}>
          Live trading is <b>allowed</b> on Binance <b>{status.env.toUpperCase()}</b>
          {status.env === 'mainnet' ? ' — REAL MONEY.' : ' (test funds).'} Daily loss stop {status.daily_loss_pct}% per
          system · leverage cap {status.max_leverage}x · every live position gets an exchange-side stop.
        </Banner>
      )}
      {status.shared_account && (
        <Banner tone="amber">
          Two or more systems use the same Binance API key, so they share one account: they can't hold the same coin at
          the same time, and each one sizes off the whole account balance. Give each system its own sub-account key
          (<code>BINANCE_TESTNET_MAIN_API_KEY</code>, <code>…_AUTO_…</code>, <code>…_SCALPER_…</code>) to keep them apart.
        </Banner>
      )}
      {error && <Banner tone="red">{error}</Banner>}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        {(Object.keys(LABEL) as LiveSystem[]).map(s => (
          <SystemCard key={s} system={s} st={status.systems[s]} allowed={status.allowed} env={status.env}
            onChanged={() => { load(); onChange?.() }} />
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button onClick={kill} disabled={killing}
          className={clsx('inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-bold transition-colors disabled:opacity-50',
            anyLive ? 'bg-red-600 hover:bg-red-500 text-white' : 'bg-gray-800 hover:bg-red-900/60 text-red-300')}>
          <XOctagon className="w-4 h-4" /> {killing ? 'Closing everything…' : 'Kill switch — close all live positions'}
        </button>
        <span className="text-xs text-gray-500">Works even when live trading is disabled: closing risk is never blocked.</span>
        <button onClick={load} className="ml-auto inline-flex items-center gap-1.5 text-xs text-gray-400 hover:text-white">
          <RefreshCw className="w-3.5 h-3.5" /> Refresh
        </button>
      </div>

      <ActivityTables activity={activity} />
    </div>
  )
}

function SystemCard({ system, st, allowed, env, onChanged }: {
  system: LiveSystem; st: LiveSystemStatus; allowed: boolean; env: string; onChanged: () => void
}) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const live = st.mode === 'live'

  async function change(mode: 'paper' | 'live') {
    setBusy(true)
    setMsg(null)
    try {
      await api.live.setMode(system, mode, typed.trim().toUpperCase())
      setTyped('')
      onChanged()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Switch failed')
    } finally {
      setBusy(false)
    }
  }

  const blocker = !allowed ? 'Master switch off (.env)' : !st.has_keys ? `No ${env} API key in .env` :
    null
  return (
    <section className={clsx('card border space-y-3', live ? 'border-rose-500/70 bg-rose-950/20' : 'border-transparent')}>
      <div className="flex items-center justify-between">
        <h3 className="font-bold text-white">{LABEL[system]}</h3>
        <span className={clsx('text-[10px] font-black tracking-wider px-2 py-0.5 rounded',
          live ? 'bg-rose-600 text-white' : 'bg-gray-700 text-gray-300')}>{live ? 'LIVE' : 'PAPER'}</span>
      </div>
      <p className="text-xs text-gray-400">{WHAT[system]}</p>
      <dl className="text-xs space-y-1">
        <Row k="API key" v={st.has_keys ? <span className="text-emerald-400">set · account {st.account}</span>
          : <span className="text-gray-500 inline-flex items-center gap-1"><KeyRound className="w-3 h-3" /> not set</span>} />
        {st.account_balance && <Row k="Binance equity" v={`${fmtUsd(st.account_balance.equity)} · available ${fmtUsd(st.account_balance.available, 0)}`} />}
        {st.account_error && <Row k="Account" v={<span className="text-red-400">{st.account_error}</span>} />}
        {st.halted && <Row k="Halted" v={<span className="text-red-400">{st.halted}</span>} />}
      </dl>
      {live ? (
        <button onClick={() => change('paper')} disabled={busy}
          className="w-full px-3 py-2 rounded-lg text-sm font-semibold bg-gray-800 hover:bg-gray-700 text-white disabled:opacity-50">
          Switch back to paper
        </button>
      ) : (
        <div className="space-y-2">
          <label className="block text-[11px] text-gray-500" htmlFor={`confirm-${system}`}>
            Type <code className="text-gray-300">{st.confirm}</code> to go live
          </label>
          <div className="flex gap-2">
            <input id={`confirm-${system}`} value={typed} onChange={e => setTyped(e.target.value)} disabled={!!blocker}
              placeholder={blocker ?? 'type the phrase above'} autoComplete="off" spellCheck={false}
              className="flex-1 min-w-0 bg-gray-900 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-white placeholder:text-gray-600 focus:outline-none focus:border-rose-500 disabled:opacity-50" />
            <button onClick={() => change('live')} disabled={busy || !!blocker || typed.trim().toUpperCase() !== st.confirm}
              className="px-3 py-1.5 rounded-lg text-xs font-bold bg-rose-600 hover:bg-rose-500 text-white disabled:opacity-40 disabled:hover:bg-rose-600">
              Go live
            </button>
          </div>
          <p className="text-[11px] text-gray-500">
            {blocker ? <span className="text-amber-400">{blocker}</span>
              : typed.trim().toUpperCase() === st.confirm ? <span className="text-rose-300">Ready — Go live sends real orders to Binance {env}.</span>
              : 'Go live unlocks once the phrase matches exactly.'}
          </p>
        </div>
      )}
      {msg && <p className="text-xs text-red-400">{msg}</p>}
      <p className="text-[11px] text-gray-600">
        Switching changes where new entries go; open positions finish in the ledger they started in.
      </p>
    </section>
  )
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return <div className="flex justify-between gap-2"><dt className="text-gray-500">{k}</dt><dd className="text-right text-gray-200">{v}</dd></div>
}

function Banner({ tone, icon, children }: { tone: 'red' | 'amber' | 'gray'; icon?: React.ReactNode; children: React.ReactNode }) {
  const cls = { red: 'bg-red-950/50 border-red-800 text-red-200', amber: 'bg-amber-950/40 border-amber-800 text-amber-100',
                gray: 'bg-gray-900 border-gray-700 text-gray-300' }[tone]
  return <div className={clsx('flex gap-2 items-start text-sm px-4 py-3 rounded-lg border [&_code]:text-xs [&_code]:bg-black/30 [&_code]:px-1 [&_code]:rounded', cls)}>{icon}<div>{children}</div></div>
}

function ActivityTables({ activity }: { activity: LiveActivity | null }) {
  type Ev = LiveActivity['events'][number]
  type Ord = LiveActivity['orders'][number]
  type Aud = LiveActivity['audit'][number]
  const evCols: Column<Ev>[] = [
    { key: 't', header: 'Time', render: e => <span className="text-gray-400 whitespace-nowrap">{when(e.ts)}</span> },
    { key: 's', header: 'System', render: e => e.system },
    { key: 'k', header: 'Event', render: e => <span className="font-semibold">{e.kind.replace(/_/g, ' ')}</span> },
    { key: 'd', header: 'Detail', render: e => <span className="text-gray-400 break-all">{JSON.stringify(e.data).slice(0, 160)}</span> },
  ]
  const ordCols: Column<Ord>[] = [
    { key: 't', header: 'Updated', render: o => <span className="text-gray-400 whitespace-nowrap">{when(o.updated)}</span> },
    { key: 's', header: 'System', render: o => o.system },
    { key: 'sym', header: 'Symbol', render: o => <span className="font-mono">{o.symbol}</span> },
    { key: 'p', header: 'Purpose', render: o => o.purpose },
    { key: 'ty', header: 'Type', render: o => `${o.side} ${o.type}` },
    { key: 'q', header: 'Qty', align: 'right', render: o => o.qty },
    { key: 'px', header: 'Price', align: 'right', render: o => o.price ?? '—' },
    { key: 'st', header: 'Status', render: o => o.status },
  ]
  const audCols: Column<Aud>[] = [
    { key: 't', header: 'Time', render: a => <span className="text-gray-400 whitespace-nowrap">{when(a.ts)}</span> },
    { key: 's', header: 'System', render: a => a.system },
    { key: 'r', header: 'Request', render: a => <span className="font-mono">{a.method} {a.path}</span> },
    { key: 'c', header: 'Status', align: 'right', render: a => <span className={clsx((a.status ?? 500) >= 400 ? 'text-red-400' : 'text-emerald-400')}>{a.status ?? 'net err'}</span> },
    { key: 'ms', header: 'ms', align: 'right', render: a => a.ms },
    { key: 'b', header: 'Response', render: a => <span className="text-gray-500 break-all">{a.response.slice(0, 120)}</span> },
  ]
  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
      <section className="card p-0 overflow-hidden">
        <h3 className="px-4 pt-4 pb-2 font-bold text-white text-sm">Live events</h3>
        <DataTable columns={evCols} rows={activity?.events ?? []} rowKey={(e, i) => `${e.ts}-${i}`} dense empty="No live activity yet." />
      </section>
      <section className="card p-0 overflow-hidden">
        <h3 className="px-4 pt-4 pb-2 font-bold text-white text-sm">Orders sent to Binance</h3>
        <DataTable columns={ordCols} rows={activity?.orders ?? []} rowKey={o => o.cid} dense empty="No orders sent." />
      </section>
      <section className="card p-0 overflow-hidden xl:col-span-2">
        <h3 className="px-4 pt-4 pb-2 font-bold text-white text-sm">Exchange requests (audit log — keys are never stored)</h3>
        <DataTable columns={audCols} rows={activity?.audit ?? []} rowKey={(a, i) => `${a.ts}-${i}`} dense empty="No requests yet." />
      </section>
    </div>
  )
}
