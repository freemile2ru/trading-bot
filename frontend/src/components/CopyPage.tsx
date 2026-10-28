import { useEffect, useState } from 'react'
import { Users, RefreshCw, ShieldCheck, Clock } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import type { CopyStatus, CopyLeader } from '../types/copy'

const pct = (v: number | null | undefined, d = 1) => v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`
const tone = (v: number | null | undefined) => v == null ? 'text-gray-500' : v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : 'text-gray-300'
const ago = (ts: number | null) => {
  if (!ts) return 'never'
  const h = (Date.now() / 1000 - ts) / 3600
  return h < 1 ? `${Math.round(h * 60)}m ago` : h < 48 ? `${Math.round(h)}h ago` : `${Math.round(h / 24)}d ago`
}
const when = (iso: string | null | undefined) => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
const whenTs = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const TIER: Record<string, string> = { LOW_LEVERAGE: '≤5×', MEDIUM_LEVERAGE: '10–20×', HIGH_LEVERAGE: '>20×' }

/** A leader's 90-day ROI curve as a small sparkline. */
function Spark({ curve, w = 120, h = 32 }: { curve: [number, number][]; w?: number; h?: number }) {
  if (!curve || curve.length < 2) return <span className="text-gray-600 text-[10px]">no curve</span>
  const ys = curve.map(c => c[1])
  const lo = Math.min(...ys, 0), hi = Math.max(...ys, 0)
  const span = hi - lo || 1
  const pts = curve.map((c, i) => `${(i / (curve.length - 1)) * w},${h - ((c[1] - lo) / span) * h}`).join(' ')
  const zero = h - ((0 - lo) / span) * h
  return (
    <svg width={w} height={h} className="block" role="img" aria-label={`ROI curve, now ${pct(ys[ys.length - 1])}`}>
      <line x1={0} x2={w} y1={zero} y2={zero} stroke="#374151" strokeWidth={1} />
      <polyline points={pts} fill="none" stroke={ys[ys.length - 1] >= 0 ? '#34d399' : '#f87171'} strokeWidth={1.5} />
    </svg>
  )
}

const VENUE: Record<string, string> = { binance: 'bg-yellow-900/50 text-yellow-200 border-yellow-800/60', hyperliquid: 'bg-emerald-900/50 text-emerald-200 border-emerald-800/60' }

const STATE: Record<string, string> = { copied: 'bg-blue-900/60 text-blue-200 border-blue-700/60', seen: 'bg-gray-800 text-gray-400 border-gray-700' }
const stateTone = (s: string) => STATE[s] ?? (s.startsWith('skipped') ? 'bg-gray-800 text-gray-400 border-gray-700' : 'bg-gray-800/60 text-gray-500 border-gray-800')
const ageOf = (ts: number | null) => { if (!ts) return 'held before tracking'; const h = (Date.now() / 1000 - ts) / 3600; return h < 1 ? `${Math.round(h * 60)}m ago` : h < 48 ? `${h.toFixed(1)}h ago` : `${Math.round(h / 24)}d ago` }

/** The pairs a leader holds now, with direction, leverage, entry, the move since and what we did about each. */
function Holdings({ l }: { l: CopyLeader }) {
  const ps = l.positions ?? []
  if (!ps.length) return <tr className="align-top"><td colSpan={9} className="pb-3 pl-2 text-[11px] text-gray-600">No open position at the last poll.</td></tr>
  return (
    <tr className="align-top">
      <td colSpan={9} className="pb-3 pl-2">
        <div className="flex flex-wrap gap-1.5">
          {ps.map((p, i) => (
            <span key={i} className="inline-flex items-center gap-1.5 rounded-md border border-gray-800 bg-gray-950 px-2 py-1 text-[11px]"
              title={`${p.side} ${p.symbol} · entry ${p.entry} · ${p.leverage ?? '—'}× · ${p.remaining < 1 ? `${Math.round(p.remaining * 100)}% still on · ` : ''}opened ${ageOf(p.opened_ts)}${p.mark ? ` · mark ${p.mark}` : ''}`}>
              <span className={clsx('font-semibold uppercase', p.side === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{p.side}</span>
              <span className="font-mono text-gray-100">{p.symbol.replace('USDT', '')}</span>
              {p.leverage != null && <span className="text-gray-500">{p.leverage}×</span>}
              <span className="font-mono text-gray-400">@{p.entry}</span>
              {p.moved_pct != null && <span className={clsx('font-mono', tone(p.moved_pct))}>{pct(p.moved_pct, 2)}</span>}
              {p.remaining < 1 && <span className="text-gray-500">{Math.round(p.remaining * 100)}% left</span>}
              <span className={clsx('rounded border px-1 py-0.5 text-[9px] uppercase tracking-wider', stateTone(p.state))}>{p.state.startsWith('skipped') ? 'not copied' : p.state === 'seen' ? 'not copied' : p.state}</span>
              {p.state.startsWith('skipped') && <span className="text-[10px] text-gray-500">{p.state.replace('skipped: ', '')}</span>}
              {p.state === 'seen' && <span className="text-[10px] text-gray-500">seen while the book was off or before a price was available</span>}
            </span>
          ))}
        </div>
      </td>
    </tr>
  )
}

function LeaderRow({ l }: { l: CopyLeader }) {
  const name = l.nickname_en && l.nickname_en.length < 40 ? l.nickname_en : l.nickname
  const hl = l.venue === 'hyperliquid'
  return (
    <tr className="border-t border-gray-800/70 align-top">
      <td className="py-2 pr-3">
        <div className="text-gray-100 font-medium flex items-center gap-2"><span className={clsx('rounded border px-1 py-0.5 text-[9px] font-bold uppercase tracking-wider', VENUE[l.venue ?? 'binance'])}>{l.venue ?? 'binance'}</span>{name}{l.retired && <span className="rounded border border-gray-700 bg-gray-800 px-1 py-0.5 text-[9px] uppercase tracking-wider text-gray-400" title="No longer on the daily selection; still followed until our copies of theirs close">following out</span>}</div>
        <div className="text-[11px] text-gray-500">{hl
          ? <>on-chain wallet · equity {fmtUsd(l.aum, 0)} · turnover {l.turnover ?? '—'}× a month · month P&L {fmtUsdSigned(l.copier_pnl, 0)}</>
          : <>{l.days_running}d on the board · {l.copiers} copiers · AUM {fmtUsd(l.aum, 0)} · {TIER[l.leverage_tier ?? ''] ?? '—'} leverage{l.badge ? ` · ${l.badge.toLowerCase()}` : ''}</>}</div>
      </td>
      <td className="py-2 pr-3"><Spark curve={l.curve ?? []} /></td>
      <td className={clsx('py-2 pr-3 text-right font-mono', tone(l.roi))}>{pct(l.roi, 0)}</td>
      <td className="py-2 pr-3 text-right font-mono text-gray-300">{l.mdd.toFixed(1)}%</td>
      <td className="py-2 pr-3 text-right font-mono text-gray-300">{l.stats.weeks_positive_pct ?? '—'}%</td>
      <td className="py-2 pr-3 text-right font-mono text-gray-300">{l.stats.sharpe ?? '—'}</td>
      <td className={clsx('py-2 pr-3 text-right font-mono', tone(l.copier_pnl))}>{fmtUsdSigned(l.copier_pnl, 0)}</td>
      <td className="py-2 pr-3 text-right font-mono text-gray-100 font-semibold">{l.score}</td>
      <td className="py-2 text-[11px] text-gray-400">
        {l.profile.median_hold_h != null ? <>holds ~{l.profile.median_hold_h}h · wins {pct(l.profile.win_move_pct, 2)} / loses {pct(-(l.profile.loss_move_pct ?? 0), 2)} · {l.profile.win_rate}% won · {l.profile.n} trades</> : 'no profile'}
      </td>
    </tr>
  )
}

/** Copy trading: who is being mirrored and why, what's been copied, what was skipped and how that went. */
export function CopyPage() {
  const [d, setD] = useState<CopyStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let live = true
    const load = () => api.copy.status().then(x => { if (live) { setD(x); setErr(null) } }).catch(e => { if (live) setErr(String(e?.message ?? e)) })
    load()
    const id = setInterval(load, 60_000)
    return () => { live = false; clearInterval(id) }
  }, [])
  const refresh = async () => { setBusy(true); try { setD(await api.copy.refresh()) } finally { setBusy(false) } }

  if (err && !d) return <div className="card text-sm text-rose-300">Copy trading unavailable: {err}</div>
  if (!d) return <div className="card text-sm text-gray-500">Loading…</div>
  const b = d.book
  const ret = b.initial ? (b.equity / b.initial - 1) * 100 : 0

  return (
    <div className="space-y-4">
      {/* Summary */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Book equity</div><div className="text-xl font-black text-white tabular-nums mt-1">{fmtUsd(b.equity, 0)}</div><div className={clsx('text-xs', tone(ret))}>{pct(ret, 2)} since start</div></div>
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Copies</div><div className="text-xl font-black text-white tabular-nums mt-1">{b.open} open</div><div className="text-xs text-gray-400">{b.closed} closed{b.win_rate != null ? ` · ${b.win_rate}% won` : ''} · <span className={tone(b.net_pnl)}>{fmtUsdSigned(b.net_pnl, 0)}</span></div></div>
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Leaders</div><div className="text-xl font-black text-white tabular-nums mt-1">{d.leaders.length}</div><div className="text-xs text-gray-400">re-selected daily · last {ago(d.leaders_refreshed_at)}</div></div>
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Too-late rule</div><div className="text-xl font-black text-white tabular-nums mt-1">{d.skip_rule.judged} judged</div><div className="text-xs text-gray-400">{d.skip_rule.judged ? <>skipped trades went <span className={tone(d.skip_rule.avg_outcome_pct)}>{pct(d.skip_rule.avg_outcome_pct, 2)}</span> on average · {d.skip_rule.would_have_won_pct}% would have won</> : 'no skipped trade has resolved yet'}</div></div>
        <div className="card flex flex-col justify-between"><div className="text-[10px] uppercase tracking-wider text-gray-500">Selection</div>
          <button onClick={refresh} disabled={busy} className="mt-1 inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50"><RefreshCw className={clsx('w-3.5 h-3.5', busy && 'animate-spin')} /> Re-run leader selection</button>
          <div className="text-[10px] text-gray-600 mt-1">Pulls the 90-day board and re-applies the bar (≈1 min).</div></div>
      </div>

      {/* Leaders */}
      <section className="card space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white inline-flex items-center gap-2"><Users className="w-4 h-4 text-gray-400" /> Leaders being mirrored</h3>
          <span className="text-[11px] text-gray-500">Binance bar: ≥{d.config.bar.min_copiers} copiers · AUM ≥ {fmtUsd(d.config.bar.min_aum, 0)} · copiers in profit · ≥{d.config.bar.min_days}d · MDD ≤ {d.config.bar.max_mdd}% · win ≥ {d.config.bar.min_win}% · ≥{d.config.bar.min_weeks_pos}% weeks up · last 30d up · no scalpers, no never-lose bots</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs min-w-[64rem]">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-gray-500">
                <th className="text-left font-semibold pb-1">Leader</th><th className="text-left font-semibold pb-1">90-day ROI curve</th>
                <th className="text-right font-semibold pb-1">ROI</th><th className="text-right font-semibold pb-1">Max DD</th><th className="text-right font-semibold pb-1">Weeks up</th>
                <th className="text-right font-semibold pb-1">Sharpe</th><th className="text-right font-semibold pb-1">Copier / month P&L</th><th className="text-right font-semibold pb-1">Score</th><th className="text-left font-semibold pb-1">Their style (what we judge entries against)</th>
              </tr>
            </thead>
            <tbody>{d.leaders.map(l => [<LeaderRow key={l.id} l={l} />, <Holdings key={l.id + ':p'} l={l} />])}</tbody>
          </table>
          {!d.leaders.length && <div className="text-xs text-gray-500 py-2">Nobody has passed the bar yet.</div>}
        </div>
        {d.why_not && (
          <div className={clsx('rounded-lg border px-3 py-2 text-xs', d.why_not.copied < d.why_not.total ? 'border-amber-900/60 bg-amber-950/20 text-amber-100/90' : 'border-gray-800 bg-gray-900/60 text-gray-300')}>
            <b className="text-gray-100">Why nothing more is copied: </b>{d.why_not.text}
          </div>
        )}
        {d.log && (
          <div className="text-[10px] text-gray-500">Record kept for later study: {d.log.observations.toLocaleString()} observations of {d.log.positions} leader positions{d.log.since ? ` since ${new Date(d.log.since * 1000).toLocaleDateString()}` : ''} · {d.log.selections} daily selections — every poll's pair, side, leverage, entry, price and our decision (<span className="font-mono">/api/copy/log</span>).</div>
        )}
        <div className="text-[10px] text-gray-600">Under each leader: the pairs they hold at the last poll{d.positions_at ? ` (${ago(d.positions_at)})` : ''} — direction, leverage, entry, move since entry in their favour, and what we did: copied, skipped (hover for why), seen but not copied, or held before we started tracking them.</div>
        <div className="text-[10px] text-gray-600">Hyperliquid bar: equity $50k–$20M, month ROI 5–300% (more is deposits), week and all-time positive, monthly turnover 2–300× equity (more is a market maker), then the same profile and curve tests on the wallet's fills. ROI for Binance is 90-day, for Hyperliquid the month. Score = the curve's daily Sharpe × share of positive weeks − drawdown. ROI figures are Binance's, on the leader's margin; our copies run at {d.config.leg_pct}% of equity each, leverage capped at {d.config.max_leverage}×, an {d.config.disaster_pct}% stop.</div>
      </section>

      {d.consensus.length > 0 && (
        <section className="card">
          <h3 className="text-sm font-semibold text-white">Leaders agreeing right now</h3>
          <div className="mt-1 flex flex-wrap gap-2 text-xs">
            {d.consensus.map((c, i) => <span key={i} className="rounded border border-gray-700 bg-gray-900 px-2 py-1"><span className={clsx('font-semibold', c.side === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{c.side}</span> <span className="font-mono text-gray-200">{c.symbol.replace('USDT', '')}</span> <span className="text-gray-500">· {c.leaders} leaders</span></span>)}
          </div>
          <div className="text-[10px] text-gray-600 mt-1">A new copy on a side that ≥2 leaders already hold is sized ×{d.config.consensus_x ?? 1.5}.</div>
        </section>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        {/* Open copies */}
        <section className="card space-y-2">
          <h3 className="text-sm font-semibold text-white inline-flex items-center gap-2"><ShieldCheck className="w-4 h-4 text-gray-400" /> Open copies</h3>
          {d.open.length === 0 ? <div className="text-xs text-gray-500">Nothing copied right now — a leader's new position is copied only within ¼ of their typical hold and before it has run half their typical win.</div> : (
            <table className="w-full text-xs">
              <thead><tr className="text-[10px] uppercase tracking-wider text-gray-500"><th className="text-left font-semibold pb-1">Position</th><th className="text-left font-semibold pb-1">Leader</th><th className="text-right font-semibold pb-1">Our entry</th><th className="text-right font-semibold pb-1">Vs theirs</th><th className="text-right font-semibold pb-1">Lag</th><th className="text-right font-semibold pb-1">P&L</th></tr></thead>
              <tbody>
                {d.open.map((p, i) => (
                  <tr key={i} className="border-t border-gray-800/70">
                    <td className="py-1.5"><span className={clsx('font-semibold', p.direction === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{p.direction}</span> <span className="font-mono text-gray-200">{p.symbol.replace('USDT', '')}</span><div className="text-[10px] text-gray-500">{when(p.opened_at)}</div></td>
                    <td className="py-1.5 text-gray-300">{p.leader}{p.venue && <span className="text-gray-600"> · {p.venue}</span>}{(p.consensus ?? 1) >= 2 && <span className="ml-1 text-[10px] text-blue-300">×{p.consensus} agree</span>}</td>
                    <td className="py-1.5 text-right font-mono text-gray-300">{p.entry}</td>
                    <td className={clsx('py-1.5 text-right font-mono', tone(p.moved_pct_at_entry))}>{pct(p.moved_pct_at_entry, 2)}</td>
                    <td className="py-1.5 text-right font-mono text-gray-400">{p.age_h_at_entry != null ? `${p.age_h_at_entry}h` : '—'}</td>
                    <td className={clsx('py-1.5 text-right font-mono', tone(p.pnl_usd))}>{fmtUsdSigned(p.pnl_usd ?? 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {b.recent.length > 0 && (
            <>
              <h4 className="text-xs font-semibold text-gray-300 pt-2">Recently closed</h4>
              <table className="w-full text-xs">
                <tbody>
                  {b.recent.map((t, i) => (
                    <tr key={i} className="border-t border-gray-800/70">
                      <td className="py-1"><span className={clsx('font-semibold', t.direction === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{t.direction}</span> <span className="font-mono text-gray-200">{t.symbol.replace('USDT', '')}</span></td>
                      <td className="py-1 text-gray-400">{t.leader}</td>
                      <td className="py-1 text-gray-500">{t.close_reason?.replace('_', ' ')}</td>
                      <td className="py-1 text-right text-gray-500">{when(t.closed_at)}</td>
                      <td className={clsx('py-1 text-right font-mono', tone(t.pnl_usd))}>{fmtUsdSigned(t.pnl_usd ?? 0)}{t.roe_pct != null && <span className="text-gray-500"> ({pct(t.roe_pct)})</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </section>

        {/* Skipped */}
        <section className="card space-y-2">
          <h3 className="text-sm font-semibold text-white inline-flex items-center gap-2"><Clock className="w-4 h-4 text-gray-400" /> Judged too late — and what happened next</h3>
          <p className="text-[11px] text-gray-500">A leader's position we saw but didn't copy, the price at that moment, and the move from there to the leader's exit — the too-late rule is right when these are small or negative.</p>
          {d.skipped.length === 0 ? <div className="text-xs text-gray-500">Nothing skipped yet.</div> : (
            <table className="w-full text-xs">
              <thead><tr className="text-[10px] uppercase tracking-wider text-gray-500"><th className="text-left font-semibold pb-1">Position</th><th className="text-left font-semibold pb-1">Why skipped</th><th className="text-right font-semibold pb-1">Then went</th></tr></thead>
              <tbody>
                {d.skipped.map((s, i) => (
                  <tr key={i} className="border-t border-gray-800/70 align-top">
                    <td className="py-1.5 whitespace-nowrap"><span className={clsx('font-semibold', s.side === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{s.side}</span> <span className="font-mono text-gray-200">{s.symbol.replace('USDT', '')}</span><div className="text-[10px] text-gray-500">{s.leader} · {whenTs(s.ts)}</div></td>
                    <td className="py-1.5 text-gray-400">{s.reason}</td>
                    <td className={clsx('py-1.5 text-right font-mono whitespace-nowrap', s.outcome_pct == null ? 'text-gray-600' : tone(s.outcome_pct))}>{s.outcome_pct == null ? 'still open' : pct(s.outcome_pct, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  )
}
