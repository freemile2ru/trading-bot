import { Suspense, lazy, useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { AlertCircle, ArrowLeft, Zap } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { FALLBACK_PRICING, fmtPrice, tierRank } from '../utils/pricing'
import { useAuth } from './AuthProvider'
import type { PricingTier } from '../types/auth'

const PricingPage = lazy(() => import('../components/PricingPage').then(m => ({ default: m.PricingPage })))

const GSI_SRC = 'https://accounts.google.com/gsi/client'

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (opts: { client_id: string; callback: (r: { credential: string }) => void; auto_select?: boolean; ux_mode?: 'popup' | 'redirect' }) => void
          renderButton: (el: HTMLElement, opts: Record<string, unknown>) => void
          prompt?: () => void
        }
      }
    }
  }
}

let gsiLoading: Promise<void> | null = null
function loadGsi(): Promise<void> {
  if (window.google?.accounts?.id) return Promise.resolve()
  if (gsiLoading) return gsiLoading
  gsiLoading = new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${GSI_SRC}"]`)
    if (existing) {
      existing.addEventListener('load', () => resolve())
      existing.addEventListener('error', () => reject(new Error('Google sign-in script failed to load')))
      return
    }
    const s = document.createElement('script')
    s.src = GSI_SRC
    s.async = true
    s.defer = true
    s.onload = () => resolve()
    s.onerror = () => { gsiLoading = null; reject(new Error('Google sign-in script failed to load')) }
    document.head.appendChild(s)
  })
  return gsiLoading
}

/** #pricing (or #pricing/<feature>) opens the plans without a session. */
const pricingFromHash = (): { open: boolean; feature: string | null } => {
  const [h, feature] = window.location.hash.slice(1).split('/')
  return { open: h === 'pricing', feature: feature || null }
}

/** Full-page sign-in, shown when the instance requires a session and there is none. */
export function SignIn() {
  const { config, error: authError, signInWithGoogle, devLogin } = useAuth()
  const clientId = config?.google_client_id ?? null
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [gsiReady, setGsiReady] = useState(false)
  const btn = useRef<HTMLDivElement | null>(null)
  const [pricing, setPricing] = useState(pricingFromHash)
  const [tiers, setTiers] = useState<PricingTier[]>(FALLBACK_PRICING.tiers)

  useEffect(() => {
    const onHash = () => setPricing(pricingFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    let alive = true
    api.pricing.get().then(p => { if (alive && p.tiers.length) setTiers([...p.tiers].sort((a, b) => tierRank(a.id) - tierRank(b.id))) }).catch(() => undefined)
    return () => { alive = false }
  }, [])

  const onCredential = useCallback(async (credential: string) => {
    setBusy(true); setError(null)
    try {
      await signInWithGoogle(credential)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Google sign-in failed')
    } finally {
      setBusy(false)
    }
  }, [signInWithGoogle])

  // Google Identity Services: load the script, initialise with our client id, render the button into `btn`.
  useEffect(() => {
    if (!clientId || pricing.open) return
    let alive = true
    loadGsi().then(() => {
      if (!alive || !window.google || !btn.current) return
      window.google.accounts.id.initialize({ client_id: clientId, callback: r => onCredential(r.credential), ux_mode: 'popup' })
      btn.current.innerHTML = ''
      window.google.accounts.id.renderButton(btn.current, { theme: 'filled_black', size: 'large', shape: 'pill', text: 'continue_with', width: 320, logo_alignment: 'left' })
      setGsiReady(true)
    }).catch(e => { if (alive) setError(e instanceof Error ? e.message : 'Google sign-in is unavailable') })
    return () => { alive = false }
  }, [clientId, onCredential, pricing.open])

  const goSignIn = useCallback(() => {
    window.history.replaceState(null, '', '#all')
    setPricing({ open: false, feature: null })
  }, [])
  const goPricing = useCallback(() => {
    window.history.replaceState(null, '', '#pricing')
    setPricing({ open: true, feature: null })
  }, [])

  if (pricing.open) {
    return (
      <div className="min-h-screen bg-oracle-bg text-gray-100">
        <header className="border-b border-gray-800 py-3 sticky top-0 z-10 bg-oracle-bg/95 backdrop-blur">
          <div className="mx-auto px-4 sm:px-6 flex items-center justify-between max-w-6xl">
            <Brand />
            <button onClick={goSignIn} className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white">
              <ArrowLeft className="w-3.5 h-3.5" /> Sign in
            </button>
          </div>
        </header>
        <div className="mx-auto px-3 sm:px-5 py-5 max-w-6xl">
          <Suspense fallback={<div className="card text-sm text-gray-500">Loading plans…</div>}>
            <PricingPage highlight={pricing.feature} onSignIn={goSignIn} />
          </Suspense>
        </div>
      </div>
    )
  }

  const nothingConfigured = !clientId && !config?.dev_login

  return (
    <div className="min-h-screen bg-oracle-bg text-gray-100 flex flex-col">
      <header className="border-b border-gray-800 py-3">
        <div className="mx-auto px-4 sm:px-6 flex items-center justify-between max-w-6xl">
          <Brand />
          <button onClick={goPricing} className="text-xs text-gray-400 hover:text-white">Plans</button>
        </div>
      </header>

      <main className="flex-1 flex items-center justify-center px-4 py-10">
        <div className="w-full max-w-md space-y-6">
          <div className="text-center space-y-2">
            <h1 className="text-2xl font-black text-white">Sign in to Crypto Oracle</h1>
            <p className="text-sm text-gray-400">
              Live market signals read by Claude, paper books that mirror the engines, and a second look on every leg — one market, your capital.
            </p>
          </div>

          <div className="card space-y-4">
            {(error || authError) && (
              <div className="flex items-center gap-2 bg-red-900/30 border border-red-800 rounded-lg px-3 py-2 text-red-400 text-xs">
                <AlertCircle className="w-4 h-4 shrink-0" /> {error ?? authError}
              </div>
            )}

            {clientId && (
              <div className="flex flex-col items-center gap-2">
                <div ref={btn} className={clsx('min-h-[44px] flex justify-center', busy && 'opacity-50 pointer-events-none')} />
                {!gsiReady && !error && <div className="text-[11px] text-gray-500">Loading Google sign-in…</div>}
                {busy && <div className="text-[11px] text-blue-300">Signing you in…</div>}
              </div>
            )}

            {nothingConfigured && (
              <p className="text-xs text-amber-300 bg-amber-950/30 border border-amber-900/40 rounded px-3 py-2">
                Sign-in is not configured on this instance: set <code className="font-mono">GOOGLE_CLIENT_ID</code> (or allow the local sign-in in development).
              </p>
            )}

            {config?.dev_login && (
              <DevLogin onSubmit={devLogin} withDivider={!!clientId} />
            )}
          </div>

          {/* Plan teaser */}
          <div className="space-y-2">
            <div className="grid grid-cols-3 gap-2">
              {tiers.map(t => (
                <button key={t.id} onClick={goPricing}
                  className={clsx('card py-3 px-2 text-center hover:border-gray-600 transition-colors', t.highlight && 'border-blue-600/60')}>
                  <div className={clsx('text-xs font-bold', t.id === 'elite' ? 'text-amber-300' : t.id === 'pro' ? 'text-blue-300' : 'text-gray-300')}>{t.name}</div>
                  <div className="text-lg font-black text-white tabular-nums">{fmtPrice(t.price_usd_month)}</div>
                  <div className="text-[10px] text-gray-500">per month</div>
                </button>
              ))}
            </div>
            <p className="text-center text-[11px] text-gray-500">
              Free starts with delayed signals, the news and the calendar.{' '}
              <button onClick={goPricing} className="text-blue-300 hover:underline">Compare the plans</button>
            </p>
          </div>
        </div>
      </main>
    </div>
  )
}

function Brand() {
  return (
    <div className="flex items-center gap-3">
      <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center">
        <Zap className="w-5 h-5 text-white" />
      </div>
      <div>
        <div className="font-black text-white text-lg leading-none">Crypto Oracle</div>
        <div className="hidden sm:block text-xs text-gray-500">AI-Powered Trading Signals</div>
      </div>
    </div>
  )
}

/** Development only: the backend enables it when GOOGLE_CLIENT_ID is unset and never in production. */
function DevLogin({ onSubmit, withDivider }: { onSubmit: (email: string, name?: string) => Promise<void>; withDivider: boolean }) {
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!email.trim()) { setError('An email is needed'); return }
    setBusy(true); setError(null)
    try {
      await onSubmit(email.trim(), name.trim() || undefined)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Local sign-in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="space-y-2">
      {withDivider && (
        <div className="flex items-center gap-2 text-[10px] text-gray-600 uppercase tracking-wider">
          <span className="flex-1 border-t border-gray-800" /> or <span className="flex-1 border-t border-gray-800" />
        </div>
      )}
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-gray-300">Local sign-in</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300 border border-amber-800/60 font-semibold">development only</span>
      </div>
      <p className="text-[11px] text-gray-500">No Google client on this instance — any email signs in as a local tenant. Never enabled in production.</p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <input type="email" required placeholder="you@example.com" value={email} onChange={e => setEmail(e.target.value)} autoComplete="email"
          className="bg-gray-800 border border-gray-700 text-gray-100 text-sm rounded px-2 py-1.5" />
        <input type="text" placeholder="Name (optional)" value={name} onChange={e => setName(e.target.value)} autoComplete="name"
          className="bg-gray-800 border border-gray-700 text-gray-100 text-sm rounded px-2 py-1.5" />
      </div>
      {error && <p className="text-[11px] text-red-400">{error}</p>}
      <button type="submit" disabled={busy}
        className="w-full bg-gray-800 hover:bg-gray-700 text-white px-3 py-1.5 rounded-lg text-sm font-semibold transition-all disabled:opacity-50">
        {busy ? 'Signing in…' : 'Sign in locally'}
      </button>
    </form>
  )
}
