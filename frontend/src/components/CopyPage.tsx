import { useEffect, useState } from 'react'
import { Users, RefreshCw, ShieldCheck, Clock } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import type { CopyStatus, CopyLeader, CopyBook } from '../types/copy'
import { SortHeader, useSort, type SortState } from './ui/Sortable'

type Market = 'crypto' | 'stocks' | 'futures'
const MARKETS: [Market, string, string][] = [
  ['crypto', 'Crypto', 'the leaders’ crypto perpetual positions'],
  ['stocks', 'Stocks', 'the leaders’ positions in Binance’s stock perpetuals (TSLA, NVDA, SK Hynix, Meta…)'],
  ['futures', 'Futures', 'the leaders’ positions in Binance’s commodity perpetuals (gold, silver, oil, gas, metals)'],
]
const EMPTY: never[] = []
const GOAL_PER_DAY: [number, number] = [100, 200]          // what each copy book is asked to earn a day at its $20k
const leaderName = (l: CopyLeader) => (l.nickname_en && l.nickname_en.length < 40 ? l.nickname_en : l.nickname)

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
const VARIANTS = ['base', 'wide', 'fast', 'late'] as const
const VARIANT_TONE: Record<string, string> = {
  base: 'bg-gray-800 text-gray-300 border-gray-700', wide: 'bg-indigo-900/50 text-indigo-200 border-indigo-800/60',
  fast: 'bg-amber-900/50 text-amber-200 border-amber-800/60', late: 'bg-teal-900/50 text-teal-200 border-teal-800/60',
}
const VariantTag = ({ v }: { v?: string }) => <span className={clsx('rounded border px-1 py-0.5 text-[9px] uppercase tracking-wider', VARIANT_TONE[v ?? 'base'])}>{v ?? 'base'}</span>

/** One copy rule's book in this market: its equity, record and open copies. */
function VariantCard({ b, open }: { b: CopyBook; open: CopyStatus['open'] }) {
  const ret = b.initial ? (b.equity / b.initial - 1) * 100 : 0
  return (
    <section className={clsx('card space-y-2', !b.enabled && 'opacity-60')}>
      <div className="flex items-start gap-2">
        <div className="min-w-0">
          <h4 className="text-sm font-semibold text-white flex items-center gap-2"><VariantTag v={b.variant} /> {b.label}</h4>
          <p className="text-[11px] text-gray-500">{b.note}</p>
        </div>
        <span className={clsx('ml-auto text-[10px] font-semibold px-1.5 py-0.5 rounded', b.enabled ? 'bg-emerald-900/50 text-emerald-300' : 'bg-gray-800 text-gray-400')} title="Switch it on the Books tab">{b.enabled ? 'ON' : 'OFF'}</span>
      </div>
      <div className="grid grid-cols-3 gap-2 text-xs">
        {b.shared_wallet
          ? <div className="bg-gray-800/40 rounded px-2 py-1.5"><div className="text-[10px] text-gray-500">7-day</div><div className="tabular-nums text-gray-100"><span className={tone(b.per_day_7d ?? 0)}>{fmtUsdSigned(b.per_day_7d ?? 0, 0)}</span>/day · {b.trades_7d ?? 0} trades</div></div>
          : <div className="bg-gray-800/40 rounded px-2 py-1.5"><div className="text-[10px] text-gray-500">Equity</div><div className="tabular-nums text-gray-100">{fmtUsd(b.equity, 0)} <span className={clsx('text-[10px]', tone(ret))}>{pct(ret, 2)}</span></div></div>}
        <div className="bg-gray-800/40 rounded px-2 py-1.5"><div className="text-[10px] text-gray-500">Open · closed</div><div className="tabular-nums text-gray-100">{b.open} · {b.closed}</div></div>
        <div className="bg-gray-800/40 rounded px-2 py-1.5"><div className="text-[10px] text-gray-500">Won · net</div><div className="tabular-nums text-gray-100">{b.win_rate == null ? '—' : `${b.win_rate}%`} · <span className={tone(b.net_pnl)}>{fmtUsdSigned(b.net_pnl, 0)}</span></div></div>
      </div>
      {open.length > 0 && (
        <div className="space-y-1">
          {open.map((p, i) => (
            <div key={i} className="flex flex-wrap items-center gap-x-2 text-[11px]">
              <span className={clsx('font-semibold', p.direction === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{p.direction}</span>
              <span className="font-mono text-gray-200">{p.symbol.replace('USDT', '')}</span>
              <span className="text-gray-500">{p.leader}{p.size_x != null && p.size_x !== 1 ? ` · ×${p.size_x}` : ''}</span>
              <span className={clsx('ml-auto font-mono', tone(p.pnl_usd))}>{fmtUsdSigned(p.pnl_usd ?? 0)}</span>
            </div>
          ))}
        </div>
      )}
      {!!b.recent.length && <div className="text-[10px] text-gray-500">Last closed: {b.recent.slice(0, 3).map(t => `${t.symbol.replace('USDT', '')} ${fmtUsdSigned(t.pnl_usd ?? 0, 0)}`).join(' · ')}</div>}
    </section>
  )
}

function readMarket(): Market {
  const h = window.location.hash.split('/')[1] as Market
  return MARKETS.some(m => m[0] === h) ? h : 'crypto'
}

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

const VENUE: Record<string, string> = { binance: 'bg-yellow-900/50 text-yellow-200 border-yellow-800/60', hyperliquid: 'bg-emerald-900/50 text-emerald-200 border-emerald-800/60', lighter: 'bg-sky-900/50 text-sky-200 border-sky-800/60' }
const STATE: Record<string, string> = { copied: 'bg-blue-900/60 text-blue-200 border-blue-700/60', seen: 'bg-gray-800 text-gray-400 border-gray-700' }
const stateTone = (s: string) => STATE[s] ?? (s.startsWith('skipped') ? 'bg-gray-800 text-gray-400 border-gray-700' : 'bg-gray-800/60 text-gray-500 border-gray-800')
const ageOf = (ts: number | null) => { if (!ts) return 'held before tracking'; const h = (Date.now() / 1000 - ts) / 3600; return h < 1 ? `${Math.round(h * 60)}m ago` : h < 48 ? `${h.toFixed(1)}h ago` : `${Math.round(h / 24)}d ago` }
const marketOf = (p: { market?: string }) => (p.market ?? 'crypto') as Market

/** The pairs a leader holds now in this market, with direction, leverage, entry, the move since and what we did about each. */
function Holdings({ l, market }: { l: CopyLeader; market: Market }) {
  const ps = (l.positions ?? []).filter(p => marketOf(p) === market)
  if (!ps.length) return <tr className="align-top"><td colSpan={10} className="pb-3 pl-2 text-[11px] text-gray-600">No open {market === 'crypto' ? 'crypto' : market === 'stocks' ? 'stock' : 'commodity'} position at the last poll.</td></tr>
  return (
    <tr className="align-top">
      <td colSpan={10} className="pb-3 pl-2">
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

function LeaderRow({ l, market }: { l: CopyLeader; market: Market }) {
  const name = leaderName(l)
  const total = Object.values(l.markets ?? {}).reduce((a, b) => a + b, 0)
  const count = l.markets?.[market] ?? 0
  const share = total ? count / total : null
  const hl = l.venue === 'hyperliquid'
  return (
    <tr className="border-t border-gray-800/70 align-top">
      <td className="py-2 pr-3">
        <div className="text-gray-100 font-medium flex items-center gap-2"><span className={clsx('rounded border px-1 py-0.5 text-[9px] font-bold uppercase tracking-wider', VENUE[l.venue ?? 'binance'])}>{l.venue ?? 'binance'}</span>{name}{(l.variants ?? []).map(v => <VariantTag key={v} v={v} />)}{l.retired && <span className="rounded border border-gray-700 bg-gray-800 px-1 py-0.5 text-[9px] uppercase tracking-wider text-gray-400" title="No longer on the daily selection; still followed until our copies of theirs close">following out</span>}{l.probation && <span className="rounded border border-rose-900 bg-rose-950/60 px-1 py-0.5 text-[9px] uppercase tracking-wider text-rose-300" title={l.probation}>probation</span>}{l.record && !l.probation && <span className={clsx('text-[10px] tabular-nums', l.record.avg_r > 0 ? 'text-green-400' : 'text-gray-500')} title={`Our record with them: ${l.record.avg_r > 0 ? '+' : ''}${l.record.avg_r.toFixed(2)}R over their last ${l.record.n} copies`}>{l.record.avg_r > 0 ? '+' : ''}{l.record.avg_r.toFixed(2)}R · {l.record.n}</span>}{l.week && l.week.n > 0 && <span className={clsx('text-[10px] tabular-nums', (l.week_x ?? 1) > 1 ? 'text-green-400' : (l.week_x ?? 1) < 1 ? 'text-rose-300' : 'text-gray-500')} title={`Their own week: ${l.week.wins} of ${l.week.n} closes won, ${l.week.sum_move_pct > 0 ? '+' : ''}${l.week.sum_move_pct.toFixed(1)}% summed → copies at ×${l.week_x ?? 1}`}>wk {l.week.wins}/{l.week.n}{(l.week_x ?? 1) !== 1 ? ` ×${l.week_x}` : ''}</span>}</div>
        <div className="text-[11px] text-gray-500">{hl
          ? <>on-chain wallet · equity {fmtUsd(l.aum, 0)} · turnover {l.turnover ?? '—'}× a month · month P&L {fmtUsdSigned(l.copier_pnl, 0)}</>
          : <>{l.days_running}d on the board · {l.copiers} copiers · AUM {fmtUsd(l.aum, 0)} · {TIER[l.leverage_tier ?? ''] ?? '—'} leverage{l.badge ? ` · ${l.badge.toLowerCase()}` : ''}</>}</div>
      </td>
      <td className="py-2 pr-3 text-right font-mono text-gray-300" title="Share of the leader's recorded positions in this market">{share != null ? `${Math.round(share * 100)}%` : '—'}<div className="text-[10px] text-gray-500">{count} pos.</div></td>
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

/** Copy trading: one sub-tab per market — who is mirrored, what's copied there, what was skipped and how that went. */
export function CopyPage() {
  const [d, setD] = useState<CopyStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [market, setMarketState] = useState<Market>(readMarket)
  const setMarket = (m: Market) => { setMarketState(m); window.history.replaceState(null, '', m === 'crypto' ? '#copy' : `#copy/${m}`) }
  useEffect(() => {
    let live = true
    const load = () => api.copy.status().then(x => { if (live) { setD(x); setErr(null) } }).catch(e => { if (live) setErr(String(e?.message ?? e)) })
    load()
    const id = setInterval(load, 60_000)
    return () => { live = false; clearInterval(id) }
  }, [])
  const refresh = async () => { setBusy(true); try { setD(await api.copy.refresh()) } finally { setBusy(false) } }

  const inMarket = <T extends { market?: string }>(xs: T[] | undefined) => (xs ?? EMPTY).filter(x => marketOf(x) === market)
  // A leader belongs to a market's tab when that market is a real part of their trading: at least a quarter of their
  // recorded positions, or ten of them, or a position held there right now. One stock trade in a hundred doesn't count.
  const shareHere = (l: CopyLeader) => { const m = l.markets ?? {}; const total = Object.values(m).reduce((a, b) => a + b, 0); return total ? (m[market] ?? 0) / total : 0 }
  const tradesHere = (l: CopyLeader) => !l.markets || Object.keys(l.markets).length === 0 ? market === 'crypto'
    : shareHere(l) >= 0.25 || (l.markets[market] ?? 0) >= 10 || (l.positions ?? []).some(p => marketOf(p) === market)
  const leadersHere = (d?.leaders ?? EMPTY).filter(tradesHere).sort((a, b) => shareHere(b) - shareHere(a))
  const leaders = useSort(leadersHere, {
    here: l => shareHere(l),
    leader: l => leaderName(l), roi: l => l.roi, mdd: l => l.mdd, weeks: l => l.stats.weeks_positive_pct, sharpe: l => l.stats.sharpe,
    pnl: l => l.copier_pnl, score: l => l.score, style: l => l.profile.median_hold_h,
    open_here: l => (l.positions ?? []).filter(p => marketOf(p) === market).length,
  })
  const open = useSort(inMarket(d?.open), {
    position: p => `${p.symbol} ${p.direction}`, leader: p => p.leader, entry: p => p.entry, vs: p => p.moved_pct_at_entry,
    lag: p => p.age_h_at_entry, pnl: p => p.pnl_usd,
  })
  const skipped = useSort(inMarket(d?.skipped), { position: s => `${s.symbol} ${s.side}`, why: s => s.reason, went: s => s.outcome_pct })
  const H = ({ k, s, children, right }: { k: string; s: { sort: SortState | null; toggle: (key: string) => void }; children: React.ReactNode; right?: boolean }) =>
    <th className={clsx('font-semibold pb-1', right ? 'text-right' : 'text-left')}><SortHeader k={k} sort={s.sort} onSort={s.toggle} align={right ? 'right' : 'left'}>{children}</SortHeader></th>

  if (err && !d) return <div className="card text-sm text-rose-300">Copy trading unavailable: {err}</div>
  if (!d) return <div className="card text-sm text-gray-500">Loading…</div>
  const b = d.books?.[market] ?? d.variants?.[market]?.base ?? null
  const ret = b && b.initial ? (b.equity / b.initial - 1) * 100 : 0
  const info = MARKETS.find(m => m[0] === market)!
  const variants = d.variants?.[market]
  // What the leaders hold in this market, and how much of it is copied.
  const heldHere = d.leaders.flatMap(l => (l.positions ?? []).filter(p => marketOf(p) === market))
  const copiedHere = heldHere.filter(p => p.state === 'copied').length
  const activeHere = d.leaders.filter(l => (l.positions ?? []).some(p => marketOf(p) === market)).length

  return (
    <div className="space-y-4">
      {/* Sub-tabs: one per market */}
      <div className="flex flex-wrap items-end gap-1 border-b border-gray-800">
        {MARKETS.filter(([k]) => !d.books || d.books[k]).map(([k, label]) => {
          const bk = d.books?.[k]
          return (
            <button key={k} onClick={() => setMarket(k)}
              className={clsx('flex items-center gap-2 px-3 pb-2 pt-1 text-sm font-semibold border-b-2 -mb-px transition-colors',
                market === k ? 'border-blue-500 text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-300')}>
              {label}
              {bk && <span className={clsx('text-[10px] font-normal tabular-nums', bk.open ? 'text-gray-300' : 'text-gray-600')}>{bk.open} open</span>}
            </button>
          )
        })}
        <span className="ml-auto pb-2 text-[11px] text-gray-500">{info[2]} · same leaders in every tab, separate wallet and verdict per market</span>
      </div>

      {/* Summary for this market */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">{info[1]} book equity</div><div className="text-xl font-black text-white tabular-nums mt-1">{b ? fmtUsd(b.equity, 0) : '—'}</div><div className={clsx('text-xs', tone(ret))}>{b ? `${pct(ret, 2)} since start` : 'no book'}</div></div>
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Copies here · $/day vs goal</div><div className="text-xl font-black text-white tabular-nums mt-1"><span className={tone(b?.per_day_7d)}>{fmtUsdSigned(b?.per_day_7d ?? 0, 0)}</span><span className="text-sm text-gray-500 font-normal"> /day · goal {fmtUsd(GOAL_PER_DAY[0], 0)}–{fmtUsd(GOAL_PER_DAY[1], 0)}</span></div><div className="text-xs text-gray-400">{b?.open ?? 0} open · {b?.closed ?? 0} closed{b?.win_rate != null ? ` · ${b.win_rate}% won` : ''} · {b?.trades_7d ?? 0} this week · <span className={tone(b?.net_pnl)}>{fmtUsdSigned(b?.net_pnl ?? 0, 0)} all time</span></div></div>
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Leaders who trade {info[1].toLowerCase()}</div><div className="text-xl font-black text-white tabular-nums mt-1">{leadersHere.length} of {d.leaders.length}</div><div className="text-xs text-gray-400">{activeHere} hold a position here now · {heldHere.length} positions · {copiedHere} copied · re-selected daily, last {ago(d.leaders_refreshed_at)}</div></div>
        {d.second_look && (
          <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Second look · {d.second_look.mode}</div>
            <div className="text-xl font-black text-white tabular-nums mt-1">{Object.values(d.second_look.by_view).reduce((a, v) => a + v.n, 0)} read</div>
            <div className="text-xs text-gray-400">{(['agree', 'doubt', 'disagree'] as const).map(v => <span key={v} className="mr-2"><span className={clsx(v === 'agree' ? 'text-emerald-300' : v === 'doubt' ? 'text-amber-300' : 'text-rose-300')}>{v}</span> {d.second_look!.by_view[v].n}{d.second_look!.by_view[v].n ? <> · <span className={tone(d.second_look!.by_view[v].pnl)}>{fmtUsdSigned(d.second_look!.by_view[v].pnl)}</span></> : null}</span>)}
              {d.second_look.vetoed.n > 0 && <div className="mt-1">{d.second_look.vetoed.n} vetoed{d.second_look.vetoed.judged ? <> · went <span className={tone(d.second_look.vetoed.avg_outcome_pct)}>{pct(d.second_look.vetoed.avg_outcome_pct, 2)}</span> on average, {d.second_look.vetoed.would_have_won_pct}% would have won</> : ' · none resolved yet'}</div>}
            </div>
          </div>
        )}
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Too-late rule</div><div className="text-xl font-black text-white tabular-nums mt-1">{d.skip_rule.judged} judged</div><div className="text-xs text-gray-400">{d.skip_rule.judged ? <>skipped trades went <span className={tone(d.skip_rule.avg_outcome_pct)}>{pct(d.skip_rule.avg_outcome_pct, 2)}</span> on average · {d.skip_rule.would_have_won_pct}% would have won</> : 'no skipped trade has resolved yet'}</div></div>
        <div className="card flex flex-col justify-between"><div className="text-[10px] uppercase tracking-wider text-gray-500">Selection</div>
          <button onClick={refresh} disabled={busy} className="mt-1 inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50"><RefreshCw className={clsx('w-3.5 h-3.5', busy && 'animate-spin')} /> Re-run leader selection</button>
          <div className="text-[10px] text-gray-600 mt-1">Pulls the 90-day board and re-applies the bar (≈1 min).</div></div>
      </div>

      {/* Leaders, with their holdings in this market */}
      <section className="card space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white inline-flex items-center gap-2"><Users className="w-4 h-4 text-gray-400" /> Leaders being mirrored · their {info[1].toLowerCase()} holdings</h3>
          <span className="text-[11px] text-gray-500">Binance bar: ≥{d.config.bar.min_copiers} copiers · AUM ≥ {fmtUsd(d.config.bar.min_aum, 0)} · copiers in profit · ≥{d.config.bar.min_days}d · MDD ≤ {d.config.bar.max_mdd}% · win ≥ {d.config.bar.min_win}% · ≥{d.config.bar.min_weeks_pos}% weeks up · last 30d up · no scalpers, no never-lose bots</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs min-w-[64rem]">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-gray-500">
                <H k="leader" s={leaders}>Leader</H><H k="here" s={leaders} right>{info[1]} share</H><th className="text-left font-semibold pb-1">90-day ROI curve</th>
                <H k="roi" s={leaders} right>ROI</H><H k="mdd" s={leaders} right>Max DD</H><H k="weeks" s={leaders} right>Weeks up</H>
                <H k="sharpe" s={leaders} right>Sharpe</H><H k="pnl" s={leaders} right>Copier / month P&L</H><H k="score" s={leaders} right>Score</H><H k="style" s={leaders}>Their style (what we judge entries against)</H>
              </tr>
            </thead>
            <tbody>{leaders.rows.map(l => [<LeaderRow key={l.id} l={l} market={market} />, <Holdings key={l.id + ':p'} l={l} market={market} />])}</tbody>
          </table>
          {!d.leaders.length && <div className="text-xs text-gray-500 py-2">Nobody has passed the bar yet.</div>}
          {d.leaders.length > 0 && !leadersHere.length && <div className="text-xs text-amber-200/90 py-2">None of the {d.leaders.length} leaders who passed the bar does a meaningful share of their trading in {market === 'stocks' ? 'stock' : 'commodity'} perps (a quarter of their positions, or ten of them). Copies in this market need a leader who does — the daily selection keeps looking on Hyperliquid's stock market and Lighter.</div>}
          {leadersHere.length > 0 && leadersHere.length < d.leaders.length && <div className="text-[11px] text-gray-500 py-1">{leadersHere.length} of the {d.leaders.length} leaders do a meaningful share of their trading here (a quarter of their positions, ten of them, or one open now); the rest are on the other tabs.</div>}
        </div>
        {d.leaders.length > 0 && <div className="text-[10px] text-gray-600">Each leader’s badges say which rules follow them: <VariantTag v="base" /> top {d.config.leaders_n} a venue · <VariantTag v="wide" /> top {d.config.wide_n ?? 15} · <VariantTag v="fast" /> holds ≤ {d.config.fast_hold_h ?? 24}h · <VariantTag v="late" /> same leaders as base, later entries allowed.</div>}
        {market === 'crypto' && d.why_not && (
          <div className={clsx('rounded-lg border px-3 py-2 text-xs', d.why_not.copied < d.why_not.total ? 'border-amber-900/60 bg-amber-950/20 text-amber-100/90' : 'border-gray-800 bg-gray-900/60 text-gray-300')}>
            <b className="text-gray-100">Why nothing more is copied: </b>{d.why_not.text}
          </div>
        )}
        {market !== 'crypto' && (
          <div className="rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2 text-xs text-gray-300">
            {heldHere.length === 0
              ? `None of the current leaders holds a ${market === 'stocks' ? 'stock' : 'commodity'} perp right now. Copies happen the moment one opens a new position here that passes the too-late check${market === 'stocks' ? ' and is not inside an earnings blackout' : ''}.`
              : `Of the ${heldHere.length} ${market === 'stocks' ? 'stock' : 'commodity'} positions the leaders hold, ${copiedHere} are copied — the rest were open before we started watching them or had already run.`}
          </div>
        )}
        {d.log && (
          <div className="text-[10px] text-gray-500">Record kept for later study: {d.log.observations.toLocaleString()} observations of {d.log.positions} leader positions{d.log.since ? ` since ${new Date(d.log.since * 1000).toLocaleDateString()}` : ''} · {d.log.selections} daily selections — every poll's pair, side, leverage, entry, price and our decision (<span className="font-mono">/api/copy/log</span>).</div>
        )}
        <div className="text-[10px] text-gray-600">Under each leader: the pairs they hold in this market at the last poll{d.positions_at ? ` (${ago(d.positions_at)})` : ''} — direction, leverage, entry, move since entry in their favour, and what we did: copied, skipped (hover for why), seen but not copied, or held before we started tracking them.</div>
        <div className="text-[10px] text-gray-600">Hyperliquid and Lighter bar: equity $50k–$20M, month ROI 5–300% (more is deposits), the month's P&L positive (Hyperliquid also week and all-time), monthly turnover 2–300× equity (more is a market maker), then the same profile and curve tests on the account's fills. ROI for Binance is 90-day, for Hyperliquid the month. Score = the curve's daily Sharpe × share of positive weeks − drawdown. Each copy's stop sits at {d.config.stop_x ?? 2}× that leader's median losing move (between {d.config.stop_pct_min ?? 1}% and {d.config.disaster_pct}%) and the leg is sized so the stop costs {d.config.risk_pct ?? 0.5}% of the book (never more than {d.config.max_leg_pct ?? 25}% of it, and never more than {d.config.max_gross_pct ?? 150}% of the book on in total), leverage capped at {d.config.max_leverage}×. A book that has lost {d.config.day_loss_halt_pct ?? 3}% of itself today opens nothing more until the next UTC day. A position whose leader runs above {d.config.max_leader_leverage ?? 10}× is not copied, and one leader never has more than {d.config.max_per_leader ?? 2} copies open. Our own record sizes a copy: the side, the leader's leverage bucket and the leader each multiply the leg by {d.config.pattern_up ?? 1.25} when their last 20 copies average above 0R and by {d.config.pattern_down ?? 0.5} when not, once a cell has {d.config.pattern_n ?? 8} copies; the leader's own week does the same ({d.config.week_n ?? 3}+ closes seen in their feed over {d.config.week_days ?? 7} days: ≥60% winners with a positive sum ×{d.config.pattern_up ?? 1.25}, a losing week ×{d.config.pattern_down ?? 0.5}). A leader whose last {d.config.leader_probation_n ?? 5}+ copies average 0R or worse is not copied for {d.config.leader_probation_days ?? 7} days; a venue whose last {d.config.venue_measure_n ?? 10}+ copies average 0R or worse is copied at {Math.round((d.config.venue_measure_x ?? 0.25) * 100)}% size until its record turns. A copy is skipped when what is left of the leader's median win would not cover the round trip ({d.config.round_trip_pct ?? 0.16}%) {d.config.fee_gate_x ?? 3} times over. Profits are locked: once a copy has been $100 and a full R up, half of its best open P&L is kept by a ratcheting stop. When the leader halves we close half and ride the rest to their close; the only clock is a {Math.round((d.config.max_hold_h ?? 168) / 24)}-day backstop.</div>
      </section>

      {/* The copy rules under test in this market: base + three variants, each with its own wallet and verdict */}
      {variants && (
        <section className="space-y-2">
          <div className="flex flex-wrap items-baseline gap-2">
            <h3 className="text-sm font-semibold text-white">Copy rules under test · {info[1].toLowerCase()}</h3>
            <span className="text-[11px] text-gray-500">Four readings of the same leaders’ board, each in its own $-wallet and judged on its own 40 trades. Pool: {d.pool ?? d.leaders.length} leaders passed the bar; base/late follow the top {d.config.leaders_n} a venue, wide the top {d.config.wide_n ?? 15}, fast the top {d.config.leaders_n} among those holding ≤ {d.config.fast_hold_h ?? 24}h.</span>
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4 items-start">
            {VARIANTS.map(v => variants[v] && <VariantCard key={v} b={variants[v]} open={inMarket(d.open).filter(p => (p.variant ?? 'base') === v)} />)}
          </div>
        </section>
      )}

      {market === 'crypto' && d.consensus.length > 0 && (
        <section className="card">
          <h3 className="text-sm font-semibold text-white">Leaders agreeing right now</h3>
          <div className="mt-1 flex flex-wrap gap-2 text-xs">
            {d.consensus.map((c, i) => <span key={i} className="rounded border border-gray-700 bg-gray-900 px-2 py-1"><span className={clsx('font-semibold', c.side === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{c.side}</span> <span className="font-mono text-gray-200">{c.symbol.replace('USDT', '')}</span> <span className="text-gray-500">· {c.leaders} leaders</span></span>)}
          </div>
          <div className="text-[10px] text-gray-600 mt-1">A new copy on a side that ≥2 leaders already hold is sized ×{d.config.consensus_x ?? 1.5}.</div>
        </section>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        {/* Open copies in this market */}
        <section className="card space-y-2">
          <h3 className="text-sm font-semibold text-white inline-flex items-center gap-2"><ShieldCheck className="w-4 h-4 text-gray-400" /> Open {info[1].toLowerCase()} copies</h3>
          {open.rows.length === 0 ? <div className="text-xs text-gray-500">Nothing copied in this market right now under any rule — the base rule copies a leader's new position only within ¼ of their typical hold and before it has run half their typical win; the variants above loosen one thing each.</div> : (
            <table className="w-full text-xs">
              <thead><tr className="text-[10px] uppercase tracking-wider text-gray-500"><H k="position" s={open}>Position</H><H k="leader" s={open}>Leader</H><H k="entry" s={open} right>Our entry</H><H k="vs" s={open} right>Vs theirs</H><H k="lag" s={open} right>Lag</H><H k="pnl" s={open} right>P&L</H></tr></thead>
              <tbody>
                {open.rows.map((p, i) => (
                  <tr key={i} className="border-t border-gray-800/70">
                    <td className="py-1.5"><span className={clsx('font-semibold', p.direction === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{p.direction}</span> <span className="font-mono text-gray-200">{p.symbol.replace('USDT', '')}</span><div className="text-[10px] text-gray-500">{when(p.opened_at)}</div></td>
                    <td className="py-1.5 text-gray-300"><VariantTag v={p.variant} /> {p.leader}{p.venue && <span className="text-gray-600"> · {p.venue}</span>}{(p.consensus ?? 1) >= 2 && <span className="ml-1 text-[10px] text-blue-300">×{p.consensus} agree</span>}{p.second_look_view && <span className={clsx('ml-1 rounded border px-1 py-0.5 text-[9px] uppercase tracking-wider', p.second_look_view === 'agree' ? 'border-emerald-900 bg-emerald-950/50 text-emerald-300' : p.second_look_view === 'doubt' ? 'border-amber-900 bg-amber-950/50 text-amber-300' : 'border-rose-900 bg-rose-950/60 text-rose-300')} title={`Our second look at entry: ${p.second_look_why ?? ''}${p.second_look_x != null && p.second_look_x !== 1 ? ` · leg ×${p.second_look_x}` : ''}`}>{p.second_look_view}</span>}</td>
                    <td className="py-1.5 text-right font-mono text-gray-300">{p.entry}</td>
                    <td className={clsx('py-1.5 text-right font-mono', tone(p.moved_pct_at_entry))}>{pct(p.moved_pct_at_entry, 2)}</td>
                    <td className="py-1.5 text-right font-mono text-gray-400">{p.age_h_at_entry != null ? `${p.age_h_at_entry}h` : '—'}</td>
                    <td className={clsx('py-1.5 text-right font-mono', tone(p.pnl_usd))}>{fmtUsdSigned(p.pnl_usd ?? 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {!!b?.recent?.length && (
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

        {/* Skipped in this market */}
        <section className="card space-y-2">
          <h3 className="text-sm font-semibold text-white inline-flex items-center gap-2"><Clock className="w-4 h-4 text-gray-400" /> Judged too late — and what happened next</h3>
          <p className="text-[11px] text-gray-500">A leader's {info[1].toLowerCase()} position we saw but didn't copy, the price at that moment, and the move from there to the leader's exit — the too-late rule is right when these are small or negative.</p>
          {skipped.rows.length === 0 ? <div className="text-xs text-gray-500">Nothing skipped in this market yet.</div> : (
            <table className="w-full text-xs">
              <thead><tr className="text-[10px] uppercase tracking-wider text-gray-500"><H k="position" s={skipped}>Position</H><H k="why" s={skipped}>Why skipped</H><H k="went" s={skipped} right>Then went</H></tr></thead>
              <tbody>
                {skipped.rows.slice(0, 25).map((s, i) => (
                  <tr key={i} className="border-t border-gray-800/70 align-top">
                    <td className="py-1.5 whitespace-nowrap"><span className={clsx('font-semibold', s.side === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{s.side}</span> <span className="font-mono text-gray-200">{s.symbol.replace('USDT', '')}</span><div className="text-[10px] text-gray-500">{s.leader} · {whenTs(s.ts)}</div></td>
                    <td className="py-1.5 text-gray-400"><VariantTag v={s.variant} /> {s.reason}</td>
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
