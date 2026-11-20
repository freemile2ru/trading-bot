import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertCircle, KeyRound, Lock, Settings as SettingsIcon, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../utils/api'
import { useAuth } from '../../auth/AuthProvider'
import { TierBadge } from './TierBadge'
import type { EngineMode, ExchangeVenue, Settings } from '../../types/auth'

const VENUES: { id: ExchangeVenue; label: string }[] = [
  { id: 'binance', label: 'Binance' },
  { id: 'hyperliquid', label: 'Hyperliquid' },
]
const RISK_STEPS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]

function Switch({ on, disabled, onChange, label }: { on: boolean; disabled?: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button role="switch" aria-checked={on} aria-label={label} disabled={disabled} onClick={() => onChange(!on)}
      className={clsx('relative w-9 h-5 rounded-full transition-colors shrink-0 disabled:opacity-40 disabled:cursor-not-allowed',
        on ? 'bg-emerald-500' : 'bg-gray-600')}>
      <span className={clsx('absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform', on && 'translate-x-4')} />
    </button>
  )
}

/**
 * The tenant's own knobs: risk multiplier on every mirrored leg, paper capital, which engine books mirror into
 * their wallets, and (Elite) exchange keys — stored only; nothing trades on them yet (docs/saas.md "Not in this round").
 */
export function SettingsPanel({ onClose, onOpenPricing }: { onClose: () => void; onOpenPricing: (feature?: string) => void }) {
  const { session, has, tier, config } = useAuth()
  const [data, setData] = useState<Settings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [risk, setRisk] = useState(1)
  const [capital, setCapital] = useState('')
  const [books, setBooks] = useState<Record<string, boolean>>({})
  const [mode, setMode] = useState<EngineMode>('mirror')

  const load = useCallback(() => api.me.settings.get().then(s => {
    setData(s); setRisk(s.risk_x); setCapital(String(s.paper_capital_usd)); setBooks(s.books); setMode(s.engine_mode ?? 'mirror'); setError(null)
  }).catch(e => setError(e instanceof Error ? e.message : 'Could not load your settings')), [])
  useEffect(() => { load() }, [load])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const bookNames = useMemo(() => {
    if (!data) return []
    const names = new Set<string>([...Object.keys(data.books), ...data.allowed_books])
    return [...names].sort((a, b) => {
      const aa = data.allowed_books.includes(a), bb = data.allowed_books.includes(b)
      return aa === bb ? a.localeCompare(b) : aa ? -1 : 1
    })
  }, [data])
  const canMirror = has('paper.mirror')
  const maxBooks = session?.limits.paper_books ?? null
  const onCount = bookNames.filter(n => books[n] && data?.allowed_books.includes(n)).length
  const dirty = data !== null && (risk !== data.risk_x || Number(capital) !== data.paper_capital_usd
    || mode !== (data.engine_mode ?? 'mirror')
    || bookNames.some(n => !!books[n] !== !!data.books[n]))
  // The engine-mode choice shows only when the server runs the tenant-engines flag (docs/saas.md).
  const engineFlag = !!config?.tenant_engines
  const ownAllowed = (data?.engine_modes_allowed ?? ['mirror']).includes('own')

  async function save() {
    if (!data) return
    const cap = Number(capital)
    if (!Number.isFinite(cap) || cap < 0) { setError('Paper capital must be a number'); return }
    setSaving(true); setError(null); setSaved(false)
    try {
      const patch: Parameters<typeof api.me.settings.set>[0] = {}
      if (risk !== data.risk_x) patch.risk_x = risk
      if (cap !== data.paper_capital_usd) patch.paper_capital_usd = cap
      const changed: Record<string, boolean> = {}
      for (const n of bookNames) if (!!books[n] !== !!data.books[n]) changed[n] = !!books[n]
      if (Object.keys(changed).length) patch.books = changed
      if (mode !== (data.engine_mode ?? 'mirror')) patch.engine_mode = mode
      const s = await api.me.settings.set(patch)
      setData(s); setRisk(s.risk_x); setCapital(String(s.paper_capital_usd)); setBooks(s.books); setMode(s.engine_mode ?? 'mirror')
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-start sm:items-center justify-center bg-black/60 p-3 sm:p-6 overflow-y-auto" onClick={onClose} role="dialog" aria-modal="true" aria-label="Settings">
      <div className="card w-full max-w-2xl space-y-5 my-4" onClick={e => e.stopPropagation()}>
        <div className="flex items-start gap-3">
          <SettingsIcon className="w-5 h-5 text-blue-400 mt-0.5" />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-white">Settings</h2>
              <TierBadge tier={tier} />
            </div>
            <p className="text-[11px] text-gray-500">
              {session ? <>{session.tenant.name} · {session.user.email}</> : 'Your account'}. These apply to your own paper wallets only — the shared engines are unchanged.
            </p>
          </div>
          <button onClick={onClose} aria-label="Close" className="text-gray-500 hover:text-gray-300"><X className="w-4 h-4" /></button>
        </div>

        {error && (
          <div className="flex items-center gap-2 bg-red-900/30 border border-red-800 rounded-lg px-3 py-2 text-red-400 text-xs">
            <AlertCircle className="w-4 h-4 shrink-0" /> {error}
          </div>
        )}

        {!data && !error && <div className="text-xs text-gray-500">Loading…</div>}

        {data && (
          <>
            {/* Risk + capital */}
            <section className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="space-y-2">
                <label className="text-xs font-semibold text-gray-200 flex items-center justify-between">
                  Risk multiplier <span className="tabular-nums text-blue-300">{risk.toFixed(2)}×</span>
                </label>
                <input type="range" min={0.25} max={2} step={0.25} value={risk} onChange={e => setRisk(Number(e.target.value))}
                  disabled={!canMirror} className="w-full accent-blue-500 disabled:opacity-40" aria-label="Risk multiplier" />
                <div className="flex justify-between text-[10px] text-gray-500 tabular-nums">
                  {RISK_STEPS.map(s => <span key={s}>{s}×</span>)}
                </div>
                <p className="text-[11px] text-gray-500">Every mirrored leg risks the source leg's share of equity times this. 1× is the engine's own sizing.</p>
              </div>
              <div className="space-y-2">
                <label className="text-xs font-semibold text-gray-200" htmlFor="paper-capital">Paper capital</label>
                <div className="flex items-center gap-2">
                  <span className="text-gray-500 text-sm">$</span>
                  <input id="paper-capital" type="number" min={0} step={500} value={capital} onChange={e => setCapital(e.target.value)}
                    disabled={!canMirror}
                    className="w-full bg-gray-800 border border-gray-700 text-gray-100 text-sm rounded px-2 py-1.5 tabular-nums disabled:opacity-40" />
                </div>
                <p className="text-[11px] text-gray-500">
                  Spread across your books' wallets.
                  {session && <> Your plan allows up to <span className="text-gray-300">${session.limits.paper_capital_usd.toLocaleString()}</span>.</>}
                </p>
              </div>
            </section>

            {/* Engine mode — behind the tenant-engines flag */}
            {engineFlag && (
              <section className="space-y-2">
                <div className="flex items-center gap-2">
                  <h3 className="text-xs font-semibold text-gray-200">How your books run</h3>
                  {!ownAllowed && <Lock className="w-3 h-3 text-gray-500" />}
                  <span className="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300 border border-amber-800/60 font-semibold">Elite · flag</span>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {([
                    ['mirror', 'Mirror the engines', "The shared engines' entries are copied into your wallets, sized to your capital. Exits managed on your legs."],
                    ['own', 'Run the engines on my account', 'Every book you switch on runs for you: your own watchlist, switches, cooldowns and positions, sized from your capital. Behind a flag; Claude reviews share one budget.'],
                  ] as const).map(([id, label, help]) => {
                    const disabled = id === 'own' && !ownAllowed
                    return (
                      <label key={id} className={clsx('flex items-start gap-2 rounded px-2.5 py-2 text-xs border cursor-pointer',
                        mode === id ? 'border-blue-500/60 bg-blue-950/30' : 'border-gray-800 bg-gray-800/30', disabled && 'opacity-50 cursor-not-allowed')}>
                        <input type="radio" name="engine-mode" value={id} checked={mode === id} disabled={disabled}
                          onChange={() => setMode(id)} className="mt-0.5 accent-blue-500" />
                        <span className="min-w-0">
                          <span className="block font-semibold text-gray-200">{label}</span>
                          <span className="block text-[11px] text-gray-500">{help}</span>
                          {disabled && (
                            <button type="button" onClick={() => onOpenPricing('engines.own')} className="text-[10px] text-blue-300 hover:underline">Elite plan</button>
                          )}
                        </span>
                      </label>
                    )
                  })}
                </div>
              </section>
            )}

            {/* Books */}
            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-semibold text-gray-200">{mode === 'own' ? 'Books running on your account' : 'Books mirrored into your wallets'}</h3>
                <span className="text-[11px] text-gray-500 tabular-nums">
                  {onCount} on{maxBooks != null ? ` of ${maxBooks} allowed` : ''}
                </span>
              </div>
              {!canMirror && (
                <p className="text-[11px] text-amber-300">
                  Paper books are part of the Pro and Elite plans.{' '}
                  <button onClick={() => onOpenPricing('paper.mirror')} className="underline hover:text-amber-200">See plans</button>
                </p>
              )}
              {bookNames.length === 0 ? (
                <p className="text-[11px] text-gray-500">No books available to mirror.</p>
              ) : (
                <ul className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                  {bookNames.map(n => {
                    const allowed = data.allowed_books.includes(n)
                    const on = !!books[n] && allowed
                    const atCap = !on && maxBooks != null && onCount >= maxBooks
                    return (
                      <li key={n} className={clsx('flex items-center justify-between gap-2 rounded px-2.5 py-1.5 text-xs',
                        allowed ? 'bg-gray-800/40 text-gray-200' : 'bg-gray-800/20 text-gray-500')}>
                        <span className="flex items-center gap-1.5 min-w-0">
                          {!allowed && <Lock className="w-3 h-3 shrink-0" />}
                          <span className="truncate">{n}</span>
                        </span>
                        {allowed ? (
                          <Switch on={on} disabled={!canMirror || atCap} onChange={v => setBooks(b => ({ ...b, [n]: v }))} label={`${n} mirrored on or off`} />
                        ) : (
                          <button onClick={() => onOpenPricing('paper.mirror')} className="text-[10px] text-gray-500 hover:text-blue-300" title="Not in your plan">upgrade</button>
                        )}
                      </li>
                    )
                  })}
                </ul>
              )}
            </section>

            <div className="flex items-center gap-3 border-t border-gray-800 pt-3">
              <button onClick={save} disabled={!dirty || saving}
                className="bg-blue-600 hover:bg-blue-500 text-white px-4 py-1.5 rounded-lg text-sm font-semibold transition-all disabled:opacity-50">
                {saving ? 'Saving…' : 'Save'}
              </button>
              {saved && !dirty && <span className="text-xs text-emerald-400">Saved.</span>}
              {dirty && <span className="text-xs text-gray-500">Unsaved changes</span>}
            </div>

            {/* Exchange keys */}
            <ExchangeKeys data={data} onChanged={load} elite={has('mirror.exchange')} onOpenPricing={onOpenPricing} />
          </>
        )}
      </div>
    </div>
  )
}

function ExchangeKeys({ data, onChanged, elite, onOpenPricing }: {
  data: Settings; onChanged: () => void; elite: boolean; onOpenPricing: (feature?: string) => void
}) {
  const [venue, setVenue] = useState<ExchangeVenue>('binance')
  const [key, setKey] = useState('')
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null)

  async function save() {
    if (!key.trim() || !secret.trim()) { setMsg({ text: 'Both the key and the secret are needed', ok: false }); return }
    setBusy(true); setMsg(null)
    try {
      await api.me.exchangeKeys.set(venue, key.trim(), secret.trim())
      setKey(''); setSecret('')
      setMsg({ text: `${VENUES.find(v => v.id === venue)?.label} keys stored (encrypted at rest).`, ok: true })
      onChanged()
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : 'Could not store the keys', ok: false })
    } finally {
      setBusy(false)
    }
  }
  async function remove(v: ExchangeVenue) {
    if (!confirm(`Remove the stored ${VENUES.find(x => x.id === v)?.label ?? v} keys?`)) return
    setBusy(true); setMsg(null)
    try {
      await api.me.exchangeKeys.delete(v)
      setMsg({ text: 'Keys removed.', ok: true })
      onChanged()
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : 'Could not remove the keys', ok: false })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="space-y-3 border-t border-gray-800 pt-4">
      <div className="flex items-center gap-2">
        <KeyRound className="w-4 h-4 text-gray-400" />
        <h3 className="text-xs font-semibold text-gray-200">Exchange keys</h3>
        {!elite && <Lock className="w-3 h-3 text-gray-500" />}
        <span className="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300 border border-amber-800/60 font-semibold">Elite</span>
      </div>
      <p className="text-[11px] text-amber-300/90 bg-amber-950/30 border border-amber-900/40 rounded px-2.5 py-2">
        Execution on your own keys is <b>not live yet</b>: keys are stored encrypted and the plan gate exists, but no real orders are
        placed on them in this round — mirroring to your exchange account is the next round. Use read-only or trade-only keys, never withdrawal rights.
      </p>

      {/* What is configured — the configured flag only, never the stored secret */}
      <ul className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
        {VENUES.map(v => {
          const k = data.exchange_keys[v.id]
          return (
            <li key={v.id} className="flex items-center justify-between gap-2 rounded px-2.5 py-1.5 text-xs bg-gray-800/40">
              <span className="text-gray-200">{k?.label ?? v.label}</span>
              <span className="flex items-center gap-2">
                <span className={clsx('text-[10px] font-semibold', k?.configured ? 'text-emerald-400' : 'text-gray-500')}>
                  {k?.configured ? 'configured' : 'not set'}
                </span>
                {k?.configured && elite && (
                  <button onClick={() => remove(v.id)} disabled={busy} className="text-[10px] text-gray-500 hover:text-red-400 disabled:opacity-50">remove</button>
                )}
              </span>
            </li>
          )
        })}
      </ul>

      {elite ? (
        <div className="grid grid-cols-1 sm:grid-cols-[8rem_1fr_1fr_auto] gap-2 items-end">
          <label className="text-[11px] text-gray-400 space-y-1">
            <span>Venue</span>
            <select value={venue} onChange={e => setVenue(e.target.value as ExchangeVenue)}
              className="w-full bg-gray-800 border border-gray-700 text-gray-100 text-xs rounded px-2 py-1.5">
              {VENUES.map(v => <option key={v.id} value={v.id}>{v.label}</option>)}
            </select>
          </label>
          <label className="text-[11px] text-gray-400 space-y-1">
            <span>API key</span>
            <input type="text" value={key} onChange={e => setKey(e.target.value)} autoComplete="off" spellCheck={false}
              className="w-full bg-gray-800 border border-gray-700 text-gray-100 text-xs rounded px-2 py-1.5 font-mono" />
          </label>
          <label className="text-[11px] text-gray-400 space-y-1">
            <span>Secret</span>
            <input type="password" value={secret} onChange={e => setSecret(e.target.value)} autoComplete="new-password"
              className="w-full bg-gray-800 border border-gray-700 text-gray-100 text-xs rounded px-2 py-1.5 font-mono" />
          </label>
          <button onClick={save} disabled={busy}
            className="bg-gray-800 hover:bg-gray-700 text-white px-3 py-1.5 rounded-lg text-xs font-semibold transition-all disabled:opacity-50">
            {busy ? 'Saving…' : data.exchange_keys[venue]?.configured ? 'Replace' : 'Store'}
          </button>
        </div>
      ) : (
        <p className="text-[11px] text-gray-500">
          Storing exchange keys is part of the Elite plan.{' '}
          <button onClick={() => onOpenPricing('mirror.exchange')} className="text-blue-300 hover:underline">See plans</button>
        </p>
      )}
      {msg && <p className={clsx('text-[11px]', msg.ok ? 'text-emerald-400' : 'text-red-400')}>{msg.text}</p>}
    </section>
  )
}
