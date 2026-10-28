import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Microscope, RefreshCw, Sparkles } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsdSigned } from '../utils/format'
import { evenTicks } from '../utils/chart'
import type { BookStats, Comparison, Experiment, LabSummary, SwingReview, SwingTrade, Verdict } from '../types/lab'
import { StrategyCandidates } from './lab/StrategyCandidates'
import { SignalScoresPanel } from './lab/SignalScoresPanel'
import { LedgerSwitch } from './ui/LedgerSwitch'
import { useLedger } from '../hooks/useLedger'

const WITH = '#3987e5'      // categorical slot 1
const WITHOUT = '#d95926'   // categorical slot 2
const AXIS = { fill: '#6b7280', fontSize: 10 }
const TIP = { background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }
const tone = (v: number | null | undefined) => (v == null || v === 0 ? 'text-gray-300' : v > 0 ? 'text-emerald-400' : 'text-red-400')
const coin = (s: string) => s.replace('USDT', '')
const when = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const fmtDay = (ts: number) => new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })

function VerdictBadge({ v }: { v: Verdict }) {
  const cls = {
    collecting: 'bg-gray-800 text-gray-300', inconclusive: 'bg-gray-800 text-gray-200', no_difference: 'bg-gray-800 text-gray-200',
    edge: 'bg-emerald-900/50 text-emerald-300', adds: 'bg-emerald-900/50 text-emerald-300',
    no_edge: 'bg-red-900/40 text-red-300', costs: 'bg-red-900/40 text-red-300',
  }[v.status]
  return <span className={clsx('px-2 py-0.5 rounded text-[11px] font-semibold whitespace-nowrap', cls)}>{v.label}</span>
}

function Stats({ s }: { s: BookStats }) {
  const cells: [string, ReactNode][] = [
    ['Closed', s.trades], ['Win rate', s.win_rate == null ? '—' : `${s.win_rate}%`],
    ['Net', <span className={tone(s.net_pnl)}>{fmtUsdSigned(s.net_pnl)}</span>],
    ['Per trade', s.avg_pnl == null ? '—' : <span className={tone(s.avg_pnl)}>{fmtUsdSigned(s.avg_pnl)}</span>],
    ['Avg R', s.avg_r == null ? '—' : `${s.avg_r > 0 ? '+' : ''}${s.avg_r.toFixed(2)}R`],
    ['Open', s.open ? <span>{s.open} <span className={clsx('text-[11px]', tone(s.unrealized))}>({fmtUsdSigned(s.unrealized)})</span></span> : 0],
  ]
  return (
    <div className="grid grid-cols-3 gap-x-3 gap-y-1.5 text-xs">
      {cells.map(([k, v]) => (
        <div key={k}><div className="text-[10px] uppercase tracking-wider text-gray-500">{k}</div><div className="tabular-nums text-gray-100">{v}</div></div>
      ))}
    </div>
  )
}

function PairChart({ c }: { c: Comparison }) {
  const pts = [...c.with.curve.map(p => ({ ts: p.ts, with: p.pnl })), ...c.without.curve.map(p => ({ ts: p.ts, without: p.pnl }))]
    .sort((a, b) => a.ts - b.ts)
  if (pts.length < 2) return <p className="text-xs text-gray-500 py-6 text-center">The curves appear once both books have closed trades.</p>
  return (
    <ResponsiveContainer width="100%" height={160}>
      <LineChart data={pts} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']} ticks={evenTicks(pts.map(p => p.ts), 4)}
               tickFormatter={fmtDay} tick={AXIS} tickLine={false} axisLine={false} />
        <YAxis width={56} tick={AXIS} tickLine={false} axisLine={false} tickFormatter={v => fmtUsdSigned(v, 0)} />
        <ReferenceLine y={0} stroke="#4b5563" />
        <Tooltip contentStyle={TIP} labelFormatter={v => when(Number(v))}
                 formatter={(v: number, k: string) => [fmtUsdSigned(v), k === 'with' ? 'With Claude' : 'Without Claude']} />
        <Line dataKey="with" stroke={WITH} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
        <Line dataKey="without" stroke={WITHOUT} strokeWidth={2} strokeDasharray="6 3" dot={false} connectNulls isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  )
}

function ComparisonCard({ c, minTrades }: { c: Comparison; minTrades: number }) {
  const v = c.verdict
  return (
    <section className="card space-y-4">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-white">{c.title}</h3>
          <p className="text-[11px] text-gray-500 mt-0.5">Same opportunities, same exits — only Claude's judgment differs. Since {when(c.since)}.</p>
        </div>
        <div className="ml-auto"><VerdictBadge v={v} /></div>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold text-gray-200 mb-2"><span className="w-3 h-0.5" style={{ background: WITH }} />With Claude</div>
          <Stats s={c.with} />
        </div>
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold text-gray-200 mb-2"><span className="w-3 border-t-2 border-dashed" style={{ borderColor: WITHOUT }} />Without Claude</div>
          <Stats s={c.without} />
        </div>
      </div>
      {v.diff_per_trade != null && v.diff_ci && (
        <p className="text-xs text-gray-300">
          Claude changes the average trade by <span className={clsx('font-semibold', tone(v.diff_per_trade))}>{fmtUsdSigned(v.diff_per_trade)}</span>
          {' '}(95% range {fmtUsdSigned(v.diff_ci[0])} to {fmtUsdSigned(v.diff_ci[1])}; better in {v.claude_better_share}% of resamples).
          {v.status === 'collecting' && ` Judged once both books have ${minTrades} closed trades.`}
        </p>
      )}
      <PairChart c={c} />
    </section>
  )
}

function ExperimentCard({ e, minTrades }: { e: Experiment; minTrades: number }) {
  const v = e.verdict
  return (
    <section className="card space-y-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-white">{e.label}</h3>
          <p className="text-[11px] text-gray-500 mt-0.5">{e.description}</p>
        </div>
        <div className="ml-auto"><VerdictBadge v={v} /></div>
      </div>
      <Stats s={e} />
      <div className="text-[11px] text-gray-500">
        Running {e.running_days} days.{e.pending_orders ? ` ${e.pending_orders} limit order${e.pending_orders === 1 ? '' : 's'} waiting to fill.` : ''}
        {v.expectancy_ci ? ` Per-trade 95% range ${fmtUsdSigned(v.expectancy_ci[0])} to ${fmtUsdSigned(v.expectancy_ci[1])}.` : ''}
        {v.status === 'collecting' ? ` Verdict after ${minTrades} closed trades.` : ''}
      </div>
      {e.by_side && (e.by_side.long.trades + e.by_side.short.trades > 0) && (
        <div className="text-[11px] text-gray-400">
          By side: longs {e.by_side.long.trades} closed, {fmtUsdSigned(e.by_side.long.net_pnl)} · shorts {e.by_side.short.trades} closed, {fmtUsdSigned(e.by_side.short.net_pnl)}
        </div>
      )}
      {e.carry_q && (
        <div className="text-[11px] text-gray-400 space-y-1">
          <div>Equity {fmtUsdSigned(e.carry_q.equity - (e.carry_q.initial ?? 10000))} since start · notional {e.carry_q.notional_x}× equity</div>
          {e.carry_q.positions.map(p => (
            <div key={p.symbol}>{p.symbol.replace('USDT', '')}: short {p.contract} · basis locked <span className="text-emerald-400">{p.basis_locked_pct > 0 ? '+' : ''}{p.basis_locked_pct}%/yr</span> · {p.days_to_delivery}d to delivery · P&L {fmtUsdSigned(p.pnl)}</div>
          ))}
          {!e.carry_q.positions.length && <div className="text-gray-500">No positions yet.</div>}
        </div>
      )}
      {e.copy_trading && (
        <div className="text-xs text-gray-300 space-y-1.5">
          <div className="text-gray-400">Leaders ({e.copy_trading.leaders.length}, refreshed {e.copy_trading.leaders_refreshed_at ? `${Math.round((Date.now() / 1000 - e.copy_trading.leaders_refreshed_at) / 3600)}h ago` : 'never'}):</div>
          {e.copy_trading.leaders.map(l => (
            <div key={l.id} className="pl-2">
              <span className="text-gray-100 font-medium">{l.nickname_en || l.nickname}</span>
              <span className="text-gray-500"> · {l.venue === 'hyperliquid' ? 'month' : '90d'} ROI {l.roi.toFixed(0)}% · MDD {l.mdd.toFixed(1)}% · {l.stats.weeks_positive_pct ?? '—'}% weeks up · Sharpe {l.stats.sharpe ?? '—'}{l.venue === 'hyperliquid' ? ' · on-chain wallet' : ` · ${l.copiers} copiers · ${l.days_running ?? '—'}d`} · score {l.score}</span>
              {l.profile.median_hold_h != null && <span className="text-gray-500"> · holds ~{l.profile.median_hold_h}h, wins {l.profile.win_move_pct}% / loses {l.profile.loss_move_pct}%, {l.profile.win_rate}% won</span>}
            </div>
          ))}
          {!e.copy_trading.leaders.length && <div className="text-gray-500">No leader has passed the consistency bar yet.</div>}
          {e.copy_trading.open.length > 0 && <div>Copying: {e.copy_trading.open.map(p => `${p.direction} ${p.symbol.replace('USDT', '')} (${p.leader}, ${p.moved_pct_at_entry != null ? `${p.moved_pct_at_entry > 0 ? '+' : ''}${p.moved_pct_at_entry.toFixed(2)}% from their entry` : ''}${p.pnl_usd != null ? `, ${fmtUsdSigned(p.pnl_usd)}` : ''})`).join(' · ')}</div>}
          {e.copy_trading.skipped.length > 0 && (
            <div className="text-gray-400">
              Skipped (last {Math.min(e.copy_trading.skipped.length, 6)}): {e.copy_trading.skipped.slice(0, 6).map(s => `${s.side} ${s.symbol.replace('USDT', '')} — ${s.reason}${s.outcome_pct != null ? ` → went ${s.outcome_pct > 0 ? '+' : ''}${s.outcome_pct.toFixed(2)}%` : ''}`).join(' · ')}
              {e.copy_trading.skip_rule.judged > 0 && <span> · the too-late rule so far: {e.copy_trading.skip_rule.judged} judged, avg {e.copy_trading.skip_rule.avg_outcome_pct}% if taken, {e.copy_trading.skip_rule.would_have_won_pct}% would have won</span>}
            </div>
          )}
        </div>
      )}
      {e.unlocks && (
        <div className="text-xs text-gray-300 space-y-1">
          {e.unlocks.open.length > 0 && <div>Open: {e.unlocks.open.map(p => `${p.symbol.replace('USDT', '')} (${p.pct.toFixed(1)}% unlock ${new Date(p.unlock_ts * 1000).toISOString().slice(5, 10)}, ${p.mark_pct != null ? `${p.mark_pct > 0 ? '+' : ''}${p.mark_pct.toFixed(2)}%` : '—'})`).join(' · ')}</div>}
          {e.unlocks.pending.length > 0 && <div>Entering: {e.unlocks.pending.map(p => `${p.symbol.replace('USDT', '')} (${p.pct.toFixed(1)}%)`).join(' · ')}</div>}
          {e.unlocks.watch.length > 0 && <div className="text-gray-400">On the calendar: {e.unlocks.watch.map(u => `${u.symbol} ${u.pct.toFixed(1)}% on ${new Date(u.ts * 1000).toISOString().slice(5, 10)}${u.insider_share >= 0.5 ? ' (insider)' : ''}`).join(' · ')}</div>}
          {!e.unlocks.open.length && !e.unlocks.pending.length && !e.unlocks.watch.length && <div className="text-gray-500">No qualifying cliff unlock on the calendar ({e.unlocks.calendar_coins} coins tracked).</div>}
        </div>
      )}
      {e.vol_premium && (
        <div className="text-xs text-gray-300 space-y-1">
          {e.vol_premium.open.map(p => (
            <div key={p.coin}>{p.coin}: {p.qty}× condor {p.strikes.slice().sort((a, b) => a - b).map(s => s.toLocaleString()).join(' / ')} · IV {p.iv.toFixed(0)}% · credit {fmtUsdSigned(p.credit)} · max loss {fmtUsdSigned(-p.max_loss)} · mark <span className={p.mark >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{fmtUsdSigned(p.mark)}</span> · {p.days_to_expiry}d to expiry</div>
          ))}
          {!e.vol_premium.open.length && <div className="text-gray-500">No structure open — the next weekly is sold when one is 5–9 days out.</div>}
        </div>
      )}
      {e.events && (
        <div className="text-[11px] text-gray-400 space-y-1">
          {e.events.open.length > 0 && <div>Open: {e.events.open.map(p => `${p.symbol.replace('USDT', '')} (${p.kind}, ${p.mark_pct != null ? `${p.mark_pct > 0 ? '+' : ''}${p.mark_pct.toFixed(2)}%` : '—'}, until ${new Date(p.deadline_ts * 1000).toISOString().slice(5, 16).replace('T', ' ')} UTC)`).join(' · ')}</div>}
          {e.events.pending.length > 0 && <div>Waiting to enter: {e.events.pending.map(p => `${p.symbol.replace('USDT', '')} (${p.kind})`).join(' · ')}</div>}
          {!e.events.open.length && !e.events.pending.length && <div className="text-gray-500">No event in play — Binance announcements arrive a few times a week.</div>}
          <div className="text-gray-500">The autotrader mirrors each position on its own ledger (half size), ahead of its scanner setups.</div>
        </div>
      )}
      {e.carry && (
        <div className="space-y-1.5 text-[11px]">
          <div className="text-gray-300">
            Equity {fmtUsdSigned(e.carry.equity - (e.carry.initial ?? 10000))} since start · funding earned <span className="text-emerald-400">{fmtUsdSigned(e.carry.funding_earned)}</span>
            {e.carry.annualised_pct != null && <> · {e.carry.annualised_pct > 0 ? '+' : ''}{e.carry.annualised_pct}%/yr so far</>} · notional {e.carry.notional_x}× equity
          </div>
          {e.carry.positions.map(p => (
            <div key={p.symbol} className="flex flex-wrap gap-x-3 text-gray-400">
              <span className="text-gray-200 font-semibold">{coin(p.symbol)}</span>
              <span>${Math.round(p.notional).toLocaleString()} a leg</span>
              <span>funding {fmtUsdSigned(p.funding)}</span>
              <span>basis {p.basis_bps_now == null ? '—' : `${p.basis_bps_now > 0 ? '+' : ''}${p.basis_bps_now} bps`}</span>
              <span className={tone(p.pnl)}>{fmtUsdSigned(p.pnl)}</span>
              <span>{p.days}d</span>
            </div>
          ))}
          {e.research && (
            <div className="rounded-md bg-gray-800/50 px-3 py-2 text-gray-300">
              <span className="text-gray-500">What it's testing — the untouched holdout year from {e.research.holdout_from}: </span>
              {e.carry.coins.map(c => e.research?.[c] ? `${coin(c)} ${e.research[c].annual_pct}%/yr (Sharpe ${e.research[c].sharpe}, worst drawdown ${e.research[c].max_drawdown_pct}%)` : null).filter(Boolean).join(' · ')}.
              <span className="text-gray-500"> Down from ~9%/yr in 2022–24; carry on altcoins lost money in the same year.</span>
            </div>
          )}
        </div>
      )}
      {e.replay && (
        <div className="rounded-md bg-gray-800/50 px-3 py-2 text-[11px] text-gray-300">
          <span className="text-gray-500">What it's testing — a year of replayed scans ({e.replay.trades.toLocaleString()} entries): </span>
          {e.replay.mean_bps_day > 0 ? '+' : ''}{e.replay.mean_bps_day} bps/day after costs, 95% range {e.replay.ci_low} to {e.replay.ci_high}
          {' '}(train {e.replay.train_bps_day}, test {e.replay.test_bps_day}; {e.replay.quarters_positive}/{e.replay.quarters} quarters positive;
          {' '}longs {e.replay.by_side_bps.long ?? '—'}, shorts {e.replay.by_side_bps.short ?? '—'} bps).
          <span className={e.replay.passed ? ' text-emerald-400' : ' text-amber-300'}>{e.replay.passed ? ' Passed the lab rule.' : ' Not passed — the range includes zero; this book tests it going forward.'}</span>
        </div>
      )}
    </section>
  )
}

function TradeLine({ t, rejected }: { t: SwingTrade; rejected?: boolean }) {
  return (
    <li className={clsx('rounded-lg px-3 py-2 text-xs', rejected ? 'bg-gray-800/30' : 'bg-gray-800/60')}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold text-white">{coin(t.symbol)}</span>
        <span className={clsx('font-semibold capitalize', t.direction === 'long' ? 'text-emerald-400' : 'text-red-400')}>{t.direction}</span>
        <span className="text-gray-400 tabular-nums">
          {t.entry_type} {t.entry} · stop {t.stop} · target {t.target} · {t.hold_days}d · {t.confidence}%
        </span>
        {rejected && <span className="text-yellow-400">not taken — {t.why}</span>}
      </div>
      <p className="text-gray-300 mt-1">{t.thesis}</p>
      {t.invalidation && <p className="text-gray-500 mt-0.5">Wrong if: {t.invalidation}</p>}
    </li>
  )
}

function ReviewLog({ reviews }: { reviews: SwingReview[] }) {
  if (!reviews.length) return <p className="text-xs text-gray-500">No reviews yet — the first runs after the next market sweep.</p>
  return (
    <ol className="space-y-4">
      {reviews.map(r => (
        <li key={r.ts} className="border-l-2 border-gray-700 pl-3">
          <div className="text-[11px] text-gray-500">{when(r.ts)}{r.usage ? ` · ${(r.usage.input_tokens / 1000).toFixed(1)}k tokens in` : ''}</div>
          {r.error ? <p className="text-xs text-red-400 mt-1">Failed: {r.error}</p> : (
            <>
              <p className="text-sm text-gray-200 mt-1">{r.market_view}</p>
              {!r.opened?.length && !r.managed?.length && <p className="text-xs text-gray-500 mt-1">No trades — passed on all {r.coins?.length ?? 0} coins.</p>}
              {!!r.opened?.length && <ul className="space-y-1.5 mt-2">{r.opened.map(t => <TradeLine key={t.symbol + r.ts} t={t} />)}</ul>}
              {!!r.managed?.length && (
                <ul className="mt-2 text-xs text-gray-300 space-y-0.5">
                  {r.managed.map((m, i) => <li key={i}>{coin(m.symbol)}: {m.action}{m.new_stop ? ` → stop ${m.new_stop}` : ''}{m.reason ? ` — ${m.reason}` : ''}</li>)}
                </ul>
              )}
              {!!r.rejected?.length && <ul className="space-y-1.5 mt-2">{r.rejected.map(t => <TradeLine key={t.symbol + r.ts + 'x'} t={t} rejected />)}</ul>}
            </>
          )}
        </li>
      ))}
    </ol>
  )
}

export function LabPage() {
  const [data, setData] = useState<LabSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [ledger] = useLedger()
  const load = useCallback(() => api.lab.summary(ledger).then(d => { setData(d); setError(null) })
    .catch(e => setError(e instanceof Error ? e.message : 'Could not load the Lab')), [ledger])
  useEffect(() => {
    load()
    const id = setInterval(load, 30_000)
    return () => clearInterval(id)
  }, [load])

  async function reviewNow() {
    setBusy(true)
    try {
      await api.lab.reviewNow()
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Review failed')
    } finally {
      setBusy(false)
    }
  }

  if (!data) return <div className="card text-sm text-gray-500">{error ?? 'Loading the Lab…'}</div>
  const cs = data.claude_swing
  const swing = data.experiments.find(e => e.key === 'claude_swing')
  const others = data.experiments.filter(e => e.key !== 'claude_swing')
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Microscope className="w-5 h-5 text-sky-400" />
        <h2 className="text-base font-bold text-white">Lab</h2>
        <span className="text-xs text-gray-500">
          Paper-only side experiments. Verdicts are fixed rules decided in advance: judged only after {data.min_trades} closed trades,
          on whether the 95% range sits clear of zero.
        </span>
        <button onClick={load} className="ml-auto inline-flex items-center gap-1.5 text-xs text-gray-400 hover:text-white">
          <RefreshCw className="w-3.5 h-3.5" /> Refresh
        </button>
      </div>
      {!data.enabled && <div className="card text-sm text-yellow-400">Experiments are off (EXPERIMENTS_ENABLED=false).</div>}
      {error && <div className="text-sm px-4 py-2 rounded-lg border bg-red-900/30 border-red-800 text-red-400">{error}</div>}

      {data.comparisons.length > 0 && <>
      <div className="flex flex-wrap items-center gap-3 pt-1">
        <h3 className="text-sm font-bold text-white">What does Claude add?</h3>
        <span className="text-[11px] text-gray-500">The real autotrader's side counts its {data.ledger === 'both' ? 'paper wallet and live ledger' : data.ledger === 'live' ? 'live ledger only' : 'paper wallet only'}; the experiment books are paper only.</span>
        <LedgerSwitch className="ml-auto" />
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {data.comparisons.map(c => <ComparisonCard key={c.key} c={c} minTrades={data.min_trades} />)}
      </div>
      </>}

      <h3 className="text-sm font-bold text-white pt-1">Claude on its own</h3>
      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] gap-4 items-start">
        <div className="space-y-4">
          {swing && <ExperimentCard e={swing} minTrades={data.min_trades} />}
          <section className="card space-y-2 text-xs text-gray-400">
            <div className="flex items-center gap-2">
              <Sparkles className="w-4 h-4 text-sky-400" />
              <span className="font-semibold text-gray-200">Schedule</span>
              <button onClick={reviewNow} disabled={busy || cs.calls_today >= cs.daily_calls}
                      className="ml-auto px-2.5 py-1 rounded-md bg-gray-800 hover:bg-gray-700 text-white font-semibold disabled:opacity-40">
                {busy ? 'Reviewing…' : 'Review now'}
              </button>
            </div>
            <p>Every {cs.interval_h}h with {cs.model} · at most {cs.max_open} open · {cs.risk_pct}% risk per trade.</p>
            <p>Last review {cs.last_review ? when(cs.last_review) : '—'} · next {cs.next_review ? when(cs.next_review) : 'after the next sweep'} ·
              Claude calls today {cs.calls_today}/{cs.daily_calls}.</p>
          </section>
        </div>
        <section className="card">
          <h3 className="text-sm font-semibold text-white mb-3">Claude's reviews</h3>
          <ReviewLog reviews={cs.reviews} />
        </section>
      </div>

      <h3 className="text-sm font-bold text-white pt-1">Does anything the scanner reads predict the next move?</h3>
      <SignalScoresPanel />

      <h3 className="text-sm font-bold text-white pt-1">Strategy candidates — different kinds of edge, backtested</h3>
      <StrategyCandidates />

      <h3 className="text-sm font-bold text-white pt-1">Every book</h3>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {others.map(e => <ExperimentCard key={e.key} e={e} minTrades={data.min_trades} />)}
      </div>

      {data.research_shelf && data.research_shelf.length > 0 && (
        <>
          <h3 className="text-sm font-bold text-white pt-1">Research shelf</h3>
          <p className="text-[11px] text-gray-500 -mt-2">Studies still in use or waiting on data: the question asked, what the data said and what was done about it. Findings are read from each study's results file; rerun the script to reproduce. Rejected studies are on the <a href="#retired" className="text-blue-300 hover:underline">Retired tab</a>.</p>
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
            {data.research_shelf.map(r => (
              <section key={r.key} className="card space-y-1.5 text-xs">
                <div className="flex items-start gap-2">
                  <span className={clsx('shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider',
                    r.status === 'promoted' ? 'bg-emerald-900/50 text-emerald-200 border-emerald-800/60' : r.status === 'rejected' ? 'bg-gray-800 text-gray-400 border-gray-700'
                    : r.status === 'pending' ? 'bg-sky-900/40 text-sky-200 border-sky-800/60' : 'bg-amber-900/40 text-amber-200 border-amber-800/60')}>{r.status}</span>
                  <div className="min-w-0">
                    <div className="font-semibold text-gray-100">{r.title}</div>
                    <div className="text-[10px] text-gray-500 font-mono">{r.script}{r.ran_at ? ` · ${new Date(r.ran_at * 1000).toLocaleDateString()}` : ''}{r.book ? ` · ${r.book}` : ''}</div>
                  </div>
                </div>
                <div className="text-gray-400"><span className="text-gray-500">Question — </span>{r.question}</div>
                <div className="text-gray-300"><span className="text-gray-500">Found — </span>{r.finding}</div>
                <div className="text-gray-300"><span className="text-gray-500">Done — </span>{r.decision}</div>
              </section>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
