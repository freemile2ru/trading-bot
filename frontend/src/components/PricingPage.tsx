import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import { Check, CreditCard, Minus, Sparkles } from 'lucide-react'
import clsx from 'clsx'
import { api, isApiError } from '../utils/api'
import { FALLBACK_PRICING, GROUP_ORDER, fmtPrice, tierRank } from '../utils/pricing'
import { useAuth } from '../auth/AuthProvider'
import { TierBadge } from './account/TierBadge'
import type { BillingStatus, FeatureRow, Pricing, PricingTier, TierId } from '../types/auth'

let memo: Pricing | null = null          // instant render when the tab is reopened

const ACCENT: Record<TierId, string> = { free: 'text-gray-300', pro: 'text-blue-300', elite: 'text-amber-300' }

const fmtDate = (ts: number | null | undefined) =>
  ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—'

function limitsLine(t: PricingTier): string[] {
  const l = t.limits
  const out: string[] = []
  if (l.signal_delay_min > 0) out.push(`signals ${l.signal_delay_min} min delayed`)
  else if (t.features.includes('signals.live')) out.push('live signals')
  if (l.paper_books === null) out.push('every book')
  else if (l.paper_books > 0) out.push(`up to ${l.paper_books} paper books`)
  if (l.paper_capital_usd > 0) out.push(`$${(l.paper_capital_usd / 1000).toFixed(0)}k paper capital`)
  if (l.alert_channels > 0) out.push(`${l.alert_channels} alert channel${l.alert_channels > 1 ? 's' : ''}`)
  if (l.api_rpm > 0) out.push(`API ${l.api_rpm} req/min`)
  return out
}

function Mark({ on, strong }: { on: boolean; strong?: boolean }) {
  return on
    ? <Check className={clsx('w-4 h-4 shrink-0', strong ? 'text-emerald-400' : 'text-emerald-500/80')} aria-label="included" />
    : <Minus className="w-4 h-4 shrink-0 text-gray-700" aria-label="not included" />
}

/**
 * The three plans and what each includes, from GET /api/pricing (the contract's numbers only when that
 * fails). `highlight` is the feature key the user bumped into (#pricing/<feature>): its row is lit and scrolled to.
 */
export function PricingPage({ highlight = null, onSignIn }: { highlight?: string | null; onSignIn?: () => void }) {
  const { session, config } = useAuth()
  const signedIn = !!session
  const [data, setData] = useState<Pricing | null>(memo)
  const [fallback, setFallback] = useState(false)
  const [billing, setBilling] = useState<BillingStatus | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ tier: string; text: string; kind: 'warn' | 'error' } | null>(null)

  useEffect(() => {
    let alive = true
    api.pricing.get()
      .then(d => { if (alive) { memo = d; setData(d); setFallback(false) } })
      .catch(() => { if (alive && !memo) { setData(FALLBACK_PRICING); setFallback(true) } })
    return () => { alive = false }
  }, [session?.tenant.tier])

  useEffect(() => {
    if (!signedIn) { setBilling(null); return }
    let alive = true
    api.billing.status().then(b => { if (alive) setBilling(b) }).catch(() => { if (alive) setBilling(null) })
    return () => { alive = false }
  }, [signedIn, session?.tenant.tier])

  const current: string | null = data?.current_tier ?? session?.tenant.tier ?? null
  const tiers = useMemo(() => {
    const list = data?.tiers ?? []
    return [...list].sort((a, b) => tierRank(a.id) - tierRank(b.id))
  }, [data])
  const rows = data?.features ?? []
  const groups = useMemo(() => {
    const byGroup = new Map<string, FeatureRow[]>()
    for (const r of rows) byGroup.set(r.group, [...(byGroup.get(r.group) ?? []), r])
    const order = [...GROUP_ORDER.filter(g => byGroup.has(g)), ...[...byGroup.keys()].filter(g => !(GROUP_ORDER as string[]).includes(g))]
    return order.map(g => [g, byGroup.get(g) ?? []] as const)
  }, [rows])

  // The wanted feature's row scrolls into view once the table has rendered.
  const litRow = useRef<HTMLTableRowElement | null>(null)
  useEffect(() => {
    if (highlight && litRow.current) litRow.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [highlight, rows.length])

  const checkout = useCallback(async (tier: TierId) => {
    if (tier === 'free') return
    if (!signedIn) { onSignIn?.(); return }
    setBusy(tier); setNotice(null)
    try {
      const { url } = await api.billing.checkout(tier)
      window.location.assign(url)
    } catch (e) {
      if (isApiError(e) && e.status === 503) setNotice({ tier, kind: 'warn', text: 'Billing is not set up yet — contact us to switch plans.' })
      else setNotice({ tier, kind: 'error', text: e instanceof Error ? e.message : 'Checkout failed' })
    } finally {
      setBusy(null)
    }
  }, [signedIn, onSignIn])

  const portal = useCallback(async (tier: string) => {
    setBusy(tier); setNotice(null)
    try {
      const { url } = await api.billing.portal()
      window.location.assign(url)
    } catch (e) {
      if (isApiError(e) && e.status === 503) setNotice({ tier, kind: 'warn', text: 'Billing is not set up yet — contact us to manage your plan.' })
      else setNotice({ tier, kind: 'error', text: e instanceof Error ? e.message : 'Could not open the billing portal' })
    } finally {
      setBusy(null)
    }
  }, [])

  const wanted = highlight ? rows.find(r => r.key === highlight) : null
  const wantedTier = wanted ? tiers.find(t => t.features.includes(wanted.key)) : null

  if (!data) return <div className="card text-sm text-gray-500">Loading plans…</div>

  return (
    <div className="space-y-5">
      <section className="card flex flex-wrap items-center gap-3">
        <CreditCard className="w-5 h-5 text-blue-400" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold text-white">Plans</div>
          <p className="text-[11px] text-gray-500">
            One market, read once by the scanner, the oracle and the lanes; what a plan buys is how much of it you see and
            how much of it trades in your own paper books. Prices are per month; change or cancel any time from the billing portal.
            {fallback && <span className="text-amber-300"> Showing the published prices — the pricing service did not answer.</span>}
          </p>
        </div>
        {current && (
          <div className="flex items-center gap-2 text-xs text-gray-400">
            Your plan <TierBadge tier={current} />
          </div>
        )}
      </section>

      {wanted && (
        <div className="flex flex-wrap items-center gap-2 bg-blue-900/30 border border-blue-800 rounded-lg px-4 py-2.5 text-sm text-blue-200">
          <Sparkles className="w-4 h-4 shrink-0" />
          <span><b>{wanted.label}</b> is {wantedTier ? <>part of the <b>{wantedTier.name}</b> plan</> : 'not part of your plan'}{wanted.note ? ` — ${wanted.note}` : ''}.</span>
        </div>
      )}

      {billing && (billing.current_period_end || billing.cancel_at_period_end || billing.status !== 'active') && (
        <div className="card flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-400">
          <span className="font-semibold text-gray-200">Billing</span>
          <span>status <span className={clsx('font-semibold', billing.status === 'active' ? 'text-emerald-400' : billing.status === 'past_due' ? 'text-amber-300' : 'text-gray-200')}>{billing.status.replace('_', ' ')}</span></span>
          {billing.current_period_end && (
            <span>{billing.cancel_at_period_end ? 'ends' : 'renews'} <span className="text-gray-200">{fmtDate(billing.current_period_end)}</span></span>
          )}
          {billing.cancel_at_period_end && <span className="text-amber-300">cancels at the period end</span>}
          {!billing.configured && <span className="text-gray-500">Stripe is not configured on this instance — an admin assigns plans.</span>}
        </div>
      )}

      {/* Cards — stacked on a phone, three across from md */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {tiers.map(t => {
          const isCurrent = current === t.id
          const paid = t.id !== 'free'
          const onPaid = tierRank(current) > 0
          const n = notice?.tier === t.id ? notice : null
          return (
            <section key={t.id} className={clsx('card flex flex-col gap-4 relative', t.highlight && 'border-blue-600/70 ring-1 ring-blue-600/40', isCurrent && !t.highlight && 'border-gray-600')}>
              {t.highlight && (
                <span className="absolute -top-2.5 left-4 px-2 py-0.5 rounded-full bg-blue-600 text-white text-[10px] font-bold uppercase tracking-wide">Most popular</span>
              )}
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className={clsx('text-lg font-black', ACCENT[t.id])}>{t.name}</div>
                  <div className="flex items-baseline gap-1 mt-1">
                    <span className="text-3xl font-black text-white tabular-nums">{fmtPrice(t.price_usd_month)}</span>
                    <span className="text-xs text-gray-500">/ month</span>
                  </div>
                </div>
                {isCurrent && <span className="badge-neutral">Current plan</span>}
              </div>
              <p className="text-xs text-gray-400 min-h-[2.5rem]">{t.tagline}</p>
              {limitsLine(t).length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {limitsLine(t).map(x => <span key={x} className="px-1.5 py-0.5 rounded bg-gray-800/60 text-[10px] text-gray-300">{x}</span>)}
                </div>
              )}

              <div>
                {paid ? (
                  isCurrent ? (
                    <button onClick={() => portal(t.id)} disabled={busy !== null}
                      className="w-full bg-gray-800 hover:bg-gray-700 text-white px-3 py-2 rounded-lg text-sm font-semibold transition-all disabled:opacity-50">
                      {busy === t.id ? 'Opening…' : 'Manage billing'}
                    </button>
                  ) : onPaid ? (
                    <button onClick={() => portal(t.id)} disabled={busy !== null}
                      className="w-full bg-gray-800 hover:bg-gray-700 text-white px-3 py-2 rounded-lg text-sm font-semibold transition-all disabled:opacity-50"
                      title="You already have a paid plan — plan changes go through the billing portal">
                      {busy === t.id ? 'Opening…' : tierRank(t.id) > tierRank(current) ? `Upgrade to ${t.name}` : `Switch to ${t.name}`}
                    </button>
                  ) : (
                    <button onClick={() => checkout(t.id)} disabled={busy !== null}
                      className={clsx('w-full px-3 py-2 rounded-lg text-sm font-bold text-white transition-all disabled:opacity-50',
                        t.id === 'elite' ? 'bg-amber-600 hover:bg-amber-500' : 'bg-blue-600 hover:bg-blue-500')}>
                      {busy === t.id ? 'Redirecting…' : signedIn ? 'Upgrade' : 'Sign in to upgrade'}
                    </button>
                  )
                ) : (
                  <button disabled className="w-full bg-gray-800/60 text-gray-400 px-3 py-2 rounded-lg text-sm font-semibold cursor-default">
                    {isCurrent ? 'Current plan' : 'Included'}
                  </button>
                )}
                {n && (
                  <p className={clsx('mt-2 text-[11px]', n.kind === 'warn' ? 'text-amber-300' : 'text-red-400')}>{n.text}</p>
                )}
              </div>

              <ul className="space-y-1.5 text-xs border-t border-gray-800 pt-3">
                {rows.map(r => {
                  const on = t.features.includes(r.key)
                  return (
                    <li key={r.key} className={clsx('flex items-start gap-2', on ? 'text-gray-200' : 'text-gray-600', highlight === r.key && 'rounded bg-blue-900/30 -mx-1 px-1')}>
                      <Mark on={on} strong />
                      <span>{r.label}</span>
                    </li>
                  )
                })}
              </ul>
            </section>
          )
        })}
      </div>

      {/* Full comparison */}
      <section className="card p-0 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[10px] uppercase tracking-wider text-gray-500 border-b border-gray-800">
              <th className="text-left font-semibold px-4 py-2.5">Feature</th>
              {tiers.map(t => (
                <th key={t.id} className={clsx('text-center font-semibold px-3 py-2.5 w-24 sm:w-32', current === t.id && 'text-gray-200')}>
                  <div className={ACCENT[t.id]}>{t.name}</div>
                  <div className="text-gray-500 normal-case tracking-normal tabular-nums">{fmtPrice(t.price_usd_month)}/mo</div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groups.map(([group, list]) => (
              <GroupRows key={group} group={group} list={list} tiers={tiers} highlight={highlight} litRow={litRow} current={current} />
            ))}
          </tbody>
        </table>
      </section>

      {!signedIn && config?.auth_required && (
        <p className="text-xs text-gray-500">
          Sign in with Google to start on the Free plan — no card needed.{' '}
          {onSignIn && <button onClick={onSignIn} className="text-blue-300 hover:underline">Back to sign in</button>}
        </p>
      )}
    </div>
  )
}

function GroupRows({ group, list, tiers, highlight, litRow, current }: {
  group: string; list: FeatureRow[]; tiers: PricingTier[]; highlight: string | null
  litRow: MutableRefObject<HTMLTableRowElement | null>; current: string | null
}) {
  return (
    <>
      <tr className="bg-gray-800/30">
        <td colSpan={1 + tiers.length} className="px-4 py-1.5 text-[10px] font-bold uppercase tracking-wider text-gray-400">{group}</td>
      </tr>
      {list.map(r => {
        const lit = highlight === r.key
        return (
          <tr key={r.key} ref={lit ? litRow : undefined}
            className={clsx('border-t border-gray-800/60 transition-colors', lit ? 'bg-blue-900/30' : 'hover:bg-gray-800/30')}>
            <td className="px-4 py-2">
              <div className={clsx('text-xs', lit ? 'text-blue-200 font-semibold' : 'text-gray-200')}>{r.label}</div>
              {r.note && <div className="text-[10px] text-gray-500">{r.note}</div>}
            </td>
            {tiers.map(t => (
              <td key={t.id} className={clsx('px-3 py-2', current === t.id && 'bg-gray-800/20')}>
                <div className="flex justify-center"><Mark on={t.features.includes(r.key)} strong={lit} /></div>
              </td>
            ))}
          </tr>
        )
      })}
    </>
  )
}
