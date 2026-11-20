import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, RefreshCw, Server, ShieldCheck } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd } from '../utils/format'
import { TIER_ORDER } from '../utils/pricing'
import { TierBadge } from './account/TierBadge'
import type { AdminTenant, RunnerStatus, TierId } from '../types/auth'

const ago = (ts: number | null) => {
  if (!ts) return 'never'
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60))
  return m < 1 ? 'just now' : m < 60 ? `${m}m ago` : m < 48 * 60 ? `${Math.floor(m / 60)}h ago` : `${Math.floor(m / 1440)}d ago`
}
const fmtDate = (ts: number) => new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })

const STATUS_CLS: Record<AdminTenant['status'], string> = {
  active: 'bg-emerald-900/50 text-emerald-300',
  trialing: 'bg-blue-900/50 text-blue-300',
  past_due: 'bg-amber-900/50 text-amber-300',
  canceled: 'bg-gray-800 text-gray-400',
}

/** Admin only: every tenant with a tier select, and the tenant runner's health. */
export function AdminPage() {
  const [tenants, setTenants] = useState<AdminTenant[] | null>(null)
  const [runner, setRunner] = useState<RunnerStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [t, r] = await Promise.all([
        api.admin.tenants(),
        api.admin.runner().catch(() => null),
      ])
      setTenants(t.tenants)
      setRunner(r)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load the tenants')
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => {
    load()
    const id = setInterval(load, 60_000)
    return () => clearInterval(id)
  }, [load])

  async function setTier(t: AdminTenant, tier: TierId) {
    if (tier === t.tier) return
    setBusy(t.id); setError(null)
    try {
      const updated = await api.admin.setTier(t.id, tier)
      setTenants(list => list && list.map(x => (x.id === t.id ? { ...x, ...updated } : x)))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not change the tier')
    } finally {
      setBusy(null)
    }
  }

  const lastTickStale = runner?.last_tick != null && Date.now() / 1000 - runner.last_tick > 300

  return (
    <div className="space-y-4">
      <section className="card flex flex-wrap items-center gap-3">
        <ShieldCheck className="w-5 h-5 text-blue-400" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold text-white">Admin</div>
          <p className="text-[11px] text-gray-500">
            Every tenant on this instance. The tier select assigns a plan by hand — the way plans are set when Stripe is not
            configured, or to comp an account; Stripe's webhook overwrites it on the next billing event for tenants with a Stripe customer.
          </p>
        </div>
        <button onClick={load} disabled={loading}
          className="flex items-center gap-2 bg-gray-800 hover:bg-gray-700 text-white px-3 py-1.5 rounded-lg text-xs font-semibold transition-all disabled:opacity-50">
          <RefreshCw className={clsx('w-3.5 h-3.5', loading && 'animate-spin')} /> Refresh
        </button>
      </section>

      {error && (
        <div className="flex items-center gap-2 bg-red-900/30 border border-red-800 rounded-lg px-4 py-3 text-red-400 text-sm">
          <AlertCircle className="w-4 h-4 shrink-0" /> {error}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        {/* Runner */}
        <section className="card space-y-3 lg:col-span-1">
          <div className="flex items-center gap-2">
            <Server className="w-4 h-4 text-gray-400" />
            <h3 className="text-sm font-semibold text-white">Tenant runner</h3>
          </div>
          {runner ? (
            <>
              <div className="grid grid-cols-3 lg:grid-cols-1 gap-2 text-xs">
                {([
                  ['Tenants mirrored', runner.tenants_active],
                  ['Legs mirrored 24h', runner.mirrored_24h],
                  ['Last tick', <span className={clsx(lastTickStale && 'text-amber-300')}>{ago(runner.last_tick)}</span>],
                ] as const).map(([k, v]) => (
                  <div key={k} className="bg-gray-800/40 rounded px-2 py-1.5">
                    <div className="text-[10px] text-gray-500">{k}</div>
                    <div className="tabular-nums text-gray-100">{v}</div>
                  </div>
                ))}
              </div>
              {runner.errors.length > 0 ? (
                <div className="space-y-1">
                  <div className="text-[10px] uppercase tracking-wider text-red-400 font-semibold">Errors ({runner.errors.length})</div>
                  <ul className="space-y-1 max-h-48 overflow-y-auto">
                    {runner.errors.slice(0, 20).map((e, i) => (
                      <li key={i} className="text-[11px] text-red-300 bg-red-950/40 border border-red-900/40 rounded px-2 py-1 break-words">
                        {typeof e === 'string' ? e : JSON.stringify(e)}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                <p className="text-[11px] text-emerald-400">No errors.</p>
              )}
            </>
          ) : (
            <p className="text-xs text-gray-500">Runner status unavailable.</p>
          )}
        </section>

        {/* Tenants */}
        <section className="card p-0 overflow-x-auto lg:col-span-3">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-gray-500 border-b border-gray-800">
                <th className="text-left font-semibold px-4 py-2.5">Tenant</th>
                <th className="text-left font-semibold px-3 py-2.5">Owner</th>
                <th className="text-left font-semibold px-3 py-2.5">Tier</th>
                <th className="text-left font-semibold px-3 py-2.5">Status</th>
                <th className="text-right font-semibold px-3 py-2.5">Users</th>
                <th className="text-right font-semibold px-3 py-2.5">Equity</th>
                <th className="text-right font-semibold px-4 py-2.5">Created</th>
              </tr>
            </thead>
            <tbody>
              {tenants === null ? (
                <tr><td colSpan={7} className="px-4 py-6 text-center text-xs text-gray-500">Loading…</td></tr>
              ) : tenants.length === 0 ? (
                <tr><td colSpan={7} className="px-4 py-6 text-center text-xs text-gray-500">No tenants yet.</td></tr>
              ) : tenants.map(t => (
                <tr key={t.id} className="border-t border-gray-800/60 hover:bg-gray-800/30">
                  <td className="px-4 py-2">
                    <div className="text-gray-100 text-xs font-semibold">{t.name}</div>
                    <div className="text-[10px] text-gray-500 font-mono truncate max-w-[14rem]" title={t.id}>{t.id}</div>
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-300">{t.owner_email}</td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <select value={t.tier} disabled={busy === t.id} onChange={e => setTier(t, e.target.value as TierId)}
                        aria-label={`Tier for ${t.name}`}
                        className="bg-gray-800 border border-gray-700 text-gray-100 text-xs rounded px-2 py-1 disabled:opacity-50">
                        {TIER_ORDER.map(x => <option key={x} value={x}>{x}</option>)}
                      </select>
                      <TierBadge tier={t.tier} />
                      {t.stripe_customer && <span className="text-[10px] text-gray-500" title="Has a Stripe customer — the webhook sets this tier">stripe</span>}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <span className={clsx('px-1.5 py-0.5 rounded text-[10px] font-semibold', STATUS_CLS[t.status] ?? 'bg-gray-800 text-gray-400')}>{t.status.replace('_', ' ')}</span>
                  </td>
                  <td className="px-3 py-2 text-right text-xs text-gray-300 tabular-nums">{t.users}</td>
                  <td className="px-3 py-2 text-right text-xs text-gray-300 tabular-nums">{t.equity_usd == null ? '—' : fmtUsd(t.equity_usd, 0)}</td>
                  <td className="px-4 py-2 text-right text-xs text-gray-400 tabular-nums" title={new Date(t.created_ts * 1000).toLocaleString()}>{fmtDate(t.created_ts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  )
}
