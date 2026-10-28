import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { History, RefreshCw } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsdSigned } from '../utils/format'
import type { ExitComparison, HistoryInsights, InsightGroup, TradeStory } from '../types/history'
import type { SystemKey } from '../types/overview'
import { TradeDrawer } from './TradeDrawer'
import { DebriefSection } from './DebriefViews'
import { LedgerSwitch } from './ui/LedgerSwitch'
import { useLedger } from '../hooks/useLedger'
import { SortHeader, useSort } from './ui/Sortable'

const EMPTY: never[] = []

/* The History tab answers "what have the trades taught us?" — the questions no other page
   covers: which entry inputs pay, which exit rule works best, whether our interventions and
   Claude's calls help, and the stories behind the biggest wins and losses. Browsing, filtering,
   charts and CSV of individual trades live on the Trades page and aren't repeated here. */

const MIN_SAMPLE = 10          // groups smaller than this are shown greyed — too few to read into
const REFRESH_MS = 60_000
type Wallet = 'all' | 'main' | 'auto'
const WALLETS: [Wallet, string][] = [['all', 'All swing trading'], ['main', 'Main wallet'], ['auto', 'Autotrader']]

const OUTCOME_LABEL: Record<string, string> = {
  target: 'target hit',
  stopped: 'stopped out',
  breakeven: 'breakeven',
  expired: 'expired',
  left_watchlist: 'left watchlist',
  signal_flip: 'signal flip',
  manual: 'manual close',
  liquidated: 'LIQUIDATED',
  trailing_stop: 'trailing stop',
  stale: 'no progress (stale)',
  stop_loss: 'stop loss',
  tightened_stop: 'tightened stop',
  win: 'win',
  loss: 'loss',
}

function fmtR(n: number | null | undefined): string {
  return n == null ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(2)}R`
}

function tone(n: number | null | undefined) {
  if (n == null || n === 0) return 'text-gray-300'
  return n > 0 ? 'text-emerald-400' : 'text-red-400'
}

function fmtHold(minutes: number | null | undefined): string {
  if (minutes == null) return '—'
  if (minutes < 60) return `${Math.round(minutes)}m`
  const h = minutes / 60
  return h < 48 ? `${h.toFixed(1)}h` : `${(h / 24).toFixed(1)}d`
}

// ── Key lessons ────────────────────────────────────────────────────────────────

function lessons(ins: HistoryInsights, ex: ExitComparison | null): string[] {
  const out: string[] = []
  const solid = (g: InsightGroup[]) => g.filter(x => x.trades >= MIN_SAMPLE && x.avg_r != null)
  const inputs: [string, InsightGroup[]][] = [
    ['setup', ins.by_setup], ['conviction', ins.by_conviction], ['confirmation', ins.by_source],
    ['FA at entry', ins.by_fa], ['entry type', ins.by_entry_type], ['time armed', ins.by_armed], ['market regime', ins.by_regime],
  ]
  for (const [name, groups] of inputs) {
    const g = solid(groups)
    if (g.length >= 2) {
      const best = g[0], worst = g[g.length - 1]
      if ((best.avg_r ?? 0) - (worst.avg_r ?? 0) >= 0.3) {
        out.push(`By ${name}: "${best.key.replace(/_/g, ' ')}" averages ${fmtR(best.avg_r)} over ${best.trades} trades, ` +
                 `"${worst.key.replace(/_/g, ' ')}" ${fmtR(worst.avg_r)} over ${worst.trades}.`)
      }
    }
  }
  if (ex) {
    const scored = ex.policies.filter(p => p.trades >= MIN_SAMPLE && p.avg_r != null)
    if (scored.length >= 2) {
      const best = [...scored].sort((a, b) => (b.avg_r ?? 0) - (a.avg_r ?? 0))[0]
      const current = scored.find(p => p.policy === 'trail')
      if (current && best.policy !== current.policy && (best.avg_r ?? 0) - (current.avg_r ?? 0) >= 0.1) {
        out.push(`Exits: "${best.policy}" would have averaged ${fmtR(best.avg_r)} on the same trades vs ${fmtR(current.avg_r)} for the current "trail" rule.`)
      }
    }
    const iv = ex.interventions ?? []
    const total = iv.reduce((a, r) => a + r.total_delta_r, 0)
    const n = iv.reduce((a, r) => a + r.trades, 0)
    if (n >= MIN_SAMPLE && Math.abs(total) >= 1) {
      out.push(`Interventions (tightened stops, flips, manual closes, adds and trims) ${total > 0 ? 'added' : 'cost'} ${fmtR(total)} in total across ${n} trades versus leaving them alone.`)
    }
    const v = ex.calibration?.by_verdict
    if (v && v.approved.trades >= MIN_SAMPLE && v.declined.trades >= MIN_SAMPLE && v.approved.avg_r != null && v.declined.avg_r != null) {
      out.push(v.approved.avg_r > v.declined.avg_r
        ? `Claude's approvals (${fmtR(v.approved.avg_r)}) beat the entries it declined (${fmtR(v.declined.avg_r)}) — its filter helps.`
        : `The entries Claude declined (${fmtR(v.declined.avg_r)}) did better than the ones it approved (${fmtR(v.approved.avg_r)}) — its filter isn't helping yet.`)
    }
  }
  if (!out.length) out.push(`Nothing clear yet — no input has at least ${MIN_SAMPLE} trades on each side of a real difference. Lessons appear here as trades accumulate.`)
  return out
}

// ── Entry inputs ───────────────────────────────────────────────────────────────

function InsightTable({ title, subtitle, rows }: { title: string; subtitle: string; rows: InsightGroup[] }) {
  const max = Math.max(0.5, ...rows.map(r => Math.abs(r.avg_r ?? 0)))
  const t = useSort(rows, { key: r => r.key, trades: r => r.trades, won: r => r.win_rate, avg: r => r.avg_r, pnl: r => r.pnl })
  const th = (k: string, label: string, cls: string) => (
    <th className={clsx('font-semibold pb-1.5', cls)}><SortHeader k={k} sort={t.sort} onSort={t.toggle} align={cls.includes('right') ? 'right' : 'left'}>{label}</SortHeader></th>
  )
  return (
    <div className="card">
      <div className="text-sm font-semibold text-white">{title}</div>
      <p className="text-[11px] text-gray-500 mt-0.5 mb-3">{subtitle}</p>
      {!rows.length ? <p className="text-xs text-gray-500">Not recorded on these trades.</p> : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-[10px] uppercase tracking-wider text-gray-500">
              {th('key', 'Group', 'text-left')}
              {th('trades', 'Trades', 'text-right')}
              {th('won', 'Won', 'text-right')}
              {th('avg', 'Average result', 'text-center w-[38%]')}
              {th('pnl', 'P&L', 'text-right')}
            </tr>
          </thead>
          <tbody>
            {t.rows.map(r => {
              const small = r.trades < MIN_SAMPLE
              const w = Math.min(1, Math.abs(r.avg_r ?? 0) / max) * 50
              return (
                <tr key={r.key} className={clsx('border-t border-gray-800/60', small && 'opacity-50')}
                    title={small ? `Only ${r.trades} trades — too few to read into` : undefined}>
                  <td className="py-1.5 pr-2 text-gray-200 first-letter:uppercase">{r.key.replace(/_/g, ' ')}</td>
                  <td className="py-1.5 text-right tabular-nums text-gray-400">{r.trades}</td>
                  <td className="py-1.5 text-right tabular-nums text-gray-400">{r.win_rate}%</td>
                  <td className="py-1.5 px-2">
                    <div className="flex items-center gap-2">
                      <div className="relative flex-1 h-3">
                        <div className="absolute inset-y-0 left-1/2 w-px bg-gray-700" />
                        <div className={clsx('absolute inset-y-0.5 rounded-sm', (r.avg_r ?? 0) >= 0 ? 'bg-emerald-500' : 'bg-red-500')}
                             style={(r.avg_r ?? 0) >= 0 ? { left: '50%', width: `${w}%` } : { right: '50%', width: `${w}%` }} />
                      </div>
                      <span className={clsx('tabular-nums w-12 text-right', tone(r.avg_r))}>{fmtR(r.avg_r)}</span>
                    </div>
                  </td>
                  <td className={clsx('py-1.5 text-right tabular-nums', tone(r.pnl))}>{fmtUsdSigned(r.pnl, 0)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}

// ── Notable trades ─────────────────────────────────────────────────────────────

function StoryCard({ t, onOpen }: { t: TradeStory; onOpen: () => void }) {
  const thesis = t.claude_thesis || t.oracle_reasoning
  return (
    <button onClick={onOpen} className="w-full text-left bg-gray-800/40 hover:bg-gray-800/70 rounded-lg px-3 py-2.5 text-xs transition-colors">
      <div className="flex items-center gap-2">
        <span className="font-semibold text-white">{t.symbol.replace('USDT', '')}</span>
        <span className={clsx('capitalize font-semibold', t.direction === 'long' ? 'text-emerald-400' : 'text-red-400')}>{t.direction}</span>
        <span className="text-gray-500">{t.wallet.replace('_live', ' · live')}</span>
        <span className={clsx('ml-auto tabular-nums font-semibold', tone(t.r_multiple))}>{fmtR(t.r_multiple)} · {fmtUsdSigned(t.pnl_usd)}</span>
      </div>
      {thesis && <p className="text-gray-300 mt-1.5 line-clamp-2">{thesis}</p>}
      <p className="text-gray-500 mt-1">
        {OUTCOME_LABEL[t.outcome] ?? t.outcome} after {fmtHold(t.duration_minutes)}
        {t.setups_at_entry?.length ? ` · setups: ${t.setups_at_entry.join(', ').replace(/_/g, ' ')}` : ''}
        {t.exit_note ? ` · ${t.exit_note}` : ''}
      </p>
    </button>
  )
}

// ── Exit policy comparison (shadow book) ─────────────────────────────────────

function ExitPolicies({ data }: { data: ExitComparison | null }) {
  const t = useSort(data?.policies ?? EMPTY, {
    policy: p => p.policy, trades: p => p.trades, avg: p => p.avg_r, total: p => p.total_r, win: p => p.win_rate, hold: p => p.avg_hold_h,
  })
  const th = (k: string, label: string, right = false) => (
    <th className={clsx('py-2 pr-3 font-semibold', right && 'text-right')}><SortHeader k={k} sort={t.sort} onSort={t.toggle} align={right ? 'right' : 'left'}>{label}</SortHeader></th>
  )
  if (!data) return null

  const scored = data.policies.filter(p => p.trades > 0)
  const best = scored.length ? Math.max(...scored.map(p => p.avg_r ?? -Infinity)) : null

  return (
    <div className="card space-y-3">
      <div className="flex items-baseline gap-2">
        <div className="font-bold text-white text-sm">Which exit works best</div>
        <span className="text-xs text-gray-500">
          every trade is also run through each exit policy on the same price path, costs included
          {data.open_shadows > 0 && ` · ${data.open_shadows} still running`}
        </span>
      </div>
      {scored.length === 0 ? (
        <div className="text-xs text-gray-500">
          The comparison fills in as trades close. Each policy's shadow copy keeps running after
          the real trade exits, so the numbers show what that policy alone would have done.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-gray-500 border-b border-gray-800">
                {th('policy', 'Policy')}
                {th('trades', 'Trades', true)}
                {th('avg', 'Avg R', true)}
                {th('total', 'Total R', true)}
                {th('win', 'Win', true)}
                {th('hold', 'Avg hold', true)}
                <th className="py-2 font-semibold">How they ended</th>
              </tr>
            </thead>
            <tbody>
              {t.rows.map(p => (
                <tr key={p.policy} className="border-b border-gray-800/60 align-top">
                  <td className="py-2 pr-3">
                    <div className="flex items-center gap-1.5">
                      <span className="font-semibold text-white">{p.policy.replace('_', ' ')}</span>
                      {p.live && <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-sky-900/30 text-sky-300">live</span>}
                      {best != null && p.avg_r === best && p.trades > 0 && (
                        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-gray-700 text-white">best so far</span>
                      )}
                    </div>
                    <div className="text-[10px] text-gray-500 max-w-md">{data.descriptions[p.policy]}</div>
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums text-gray-300">{p.trades}</td>
                  <td className={clsx('py-2 pr-3 text-right tabular-nums font-semibold', tone(p.avg_r))}>{fmtR(p.avg_r)}</td>
                  <td className={clsx('py-2 pr-3 text-right tabular-nums', tone(p.total_r))}>{fmtR(p.total_r)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums text-gray-300">{p.win_rate != null ? `${p.win_rate}%` : '—'}</td>
                  <td className="py-2 pr-3 text-right tabular-nums text-gray-400">{p.avg_hold_h != null ? `${p.avg_hold_h}h` : '—'}</td>
                  <td className="py-2 text-gray-400">
                    {p.exits ? Object.entries(p.exits).map(([k, v]) => `${OUTCOME_LABEL[k] ?? k} ${v}`).join(' · ') : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.trades_compared < 30 && (
            <div className="text-[10px] text-gray-500 mt-2">
              {data.trades_compared} trades compared — differences this early are mostly noise; wait for 30+ across different market conditions.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Interventions ────────────────────────────────────────────────────────────

function Interventions({ data }: { data: ExitComparison | null }) {
  if (!data?.interventions?.length) return null
  const total = data.interventions.reduce((a, r) => a + r.total_delta_r, 0)
  return (
    <div className="card space-y-3">
      <div className="flex items-baseline gap-2">
        <div className="font-bold text-white text-sm">Do our interventions help?</div>
        <span className="text-xs text-gray-500">
          each real trade vs its own untouched shadow — tightened stops, flip exits, position-manager changes and
          manual closes are the only difference
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-2">
        {data.interventions.map(r => (
          <div key={r.exit} className="bg-gray-800/40 rounded-lg px-2.5 py-1.5 text-xs">
            <div className="flex justify-between gap-2">
              <span className="font-semibold text-white truncate">{OUTCOME_LABEL[r.exit] ?? r.exit.replace('_', ' ')}</span>
              <span className={clsx('tabular-nums font-semibold', tone(r.total_delta_r))}>{fmtR(r.total_delta_r)}</span>
            </div>
            <div className="text-[10px] text-gray-500">{r.trades} trades · {fmtR(r.avg_delta_r)} each</div>
          </div>
        ))}
      </div>
      <p className="text-[11px] text-gray-500">
        Net effect so far: <span className={clsx('font-semibold', tone(total))}>{fmtR(total)}</span>.
        Exits nobody intervened in should read ≈ 0R — a check that the shadow copies are faithful.
      </p>
    </div>
  )
}

// ── Claude calibration ───────────────────────────────────────────────────────

function ClaudeCalibration({ data }: { data: ExitComparison | null }) {
  if (!data?.calibration) return null
  const { by_verdict, by_confidence, policy } = data.calibration
  const total = by_verdict.approved.trades + by_verdict.declined.trades

  const Cell = ({ label, s }: { label: string; s: { trades: number; avg_r?: number; win_rate?: number } }) => (
    <div className="bg-gray-800/40 rounded-lg px-3 py-2 text-xs">
      <div className="flex justify-between">
        <span className="font-semibold text-white">{label}</span>
        <span className={clsx('tabular-nums font-semibold', tone(s.avg_r))}>{s.trades ? fmtR(s.avg_r) : '—'}</span>
      </div>
      <div className="text-[10px] text-gray-500">{s.trades} trades{s.trades ? ` · ${s.win_rate}% won` : ''}</div>
    </div>
  )

  return (
    <div className="card space-y-3">
      <div className="flex items-baseline gap-2">
        <div className="font-bold text-white text-sm">Is Claude's approval worth it?</div>
        <span className="text-xs text-gray-500">
          autotrader entries Claude approved (traded) vs declined (tracked as rules-only), both under the {policy} exit
        </span>
      </div>
      {total === 0 ? (
        <div className="text-xs text-gray-500">
          Fills in as confirmed and declined entries play out. If declined entries do as well as approved
          ones, Claude's confirmation is costing money without filtering anything.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2">
            <Cell label="Approved" s={by_verdict.approved} />
            <Cell label="Declined (rules only)" s={by_verdict.declined} />
          </div>
          <div>
            <div className="text-[11px] text-gray-400 mb-1">By Claude's confidence</div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {by_confidence.map(b => <Cell key={b.band} label={b.band} s={b} />)}
            </div>
          </div>
          {total < 30 && (
            <div className="text-[10px] text-gray-500">{total} entries so far — too few to judge; wait for 30+.</div>
          )}
        </>
      )}
    </div>
  )
}

// ── Page ───────────────────────────────────────────────────────────────────────

function Group({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">{children}</div>
}

export function TradeHistory() {
  const [wallet, setWallet] = useState<Wallet>('all')
  const [ledger] = useLedger()
  const [ins, setIns] = useState<HistoryInsights | null>(null)
  const [ex, setEx] = useState<ExitComparison | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [drawer, setDrawer] = useState<{ system: SystemKey; id: string } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [i, e] = await Promise.all([api.history.insights(wallet, ledger), api.trades.exits(wallet, ledger)])
      setIns(i); setEx(e); setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load the history')
    } finally {
      setLoading(false)
    }
  }, [wallet, ledger])
  useEffect(() => {
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => clearInterval(id)
  }, [load])

  const open = (t: TradeStory) => setDrawer({ system: t.wallet as SystemKey, id: t.id })
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <History className="w-4 h-4 text-gray-400" />
        <h2 className="text-sm font-bold text-white">What the trades teach</h2>
        <span className="text-xs text-gray-500">
          closed swing trades · browse individual trades, charts and CSV on the Trades page
        </span>
        <div className="ml-auto flex items-center gap-2">
          <LedgerSwitch />
          <div className="inline-flex rounded-lg bg-gray-800/70 p-0.5">
            {WALLETS.map(([w, label]) => (
              <button key={w} onClick={() => setWallet(w)}
                className={clsx('px-2.5 py-1 rounded-md text-xs font-semibold', wallet === w ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-gray-200')}>
                {label}
              </button>
            ))}
          </div>
          <button onClick={load} disabled={loading} className="inline-flex items-center gap-1.5 text-xs text-gray-400 hover:text-white disabled:opacity-50">
            <RefreshCw className={clsx('w-3.5 h-3.5', loading && 'animate-spin')} /> Refresh
          </button>
        </div>
      </div>
      {error && <div className="bg-red-900/30 border border-red-800 rounded-lg px-4 py-3 text-red-400 text-sm">{error}</div>}
      {!ins && !error && <div className="card text-sm text-gray-500">Loading…</div>}

      {ins && (
        <div className={clsx('space-y-4 transition-opacity', loading && 'opacity-40 pointer-events-none')} aria-busy={loading}>
          <section className="card">
            <div className="text-sm font-semibold text-white">Key lessons</div>
            <p className="text-[11px] text-gray-500 mt-0.5 mb-3">
              From {ins.trades} closed trades. Only differences backed by at least {MIN_SAMPLE} trades on each side are called out.
            </p>
            <ul className="list-disc pl-5 space-y-1.5 text-sm text-gray-300">{lessons(ins, ex).map(l => <li key={l}>{l}</li>)}</ul>
          </section>

          <h3 className="text-sm font-bold text-white pt-1">Why trades won or lost</h3>
          <DebriefSection onOpen={(system, id) => setDrawer({ system, id })} />

          <h3 className="text-sm font-bold text-white pt-1">What makes a good entry</h3>
          <Group>
            <InsightTable title="How the trade was confirmed" subtitle="Claude, a fresh scanner signal, or you" rows={ins.by_source} />
            <InsightTable title="Setup at entry" subtitle="A trade with several setups counts in each" rows={ins.by_setup} />
            <InsightTable title="Conviction at entry" subtitle="Strength of the scanner's reading (absolute)" rows={ins.by_conviction} />
            <InsightTable title="Fundamentals at entry" subtitle="Whether FA agreed with the trade's direction" rows={ins.by_fa} />
            <InsightTable title="Entry type" subtitle="At market now, or a limit order waiting for a better price" rows={ins.by_entry_type} />
            <InsightTable title="Time armed before entry" subtitle="How long the autotrader waited for its timing signal" rows={ins.by_armed} />
            <InsightTable title="Market regime at entry" subtitle="BTC trend and volatility when the trade opened (recorded from 29 Sept)" rows={ins.by_regime} />
          </Group>

          <h3 className="text-sm font-bold text-white pt-1">What makes a good exit</h3>
          <ExitPolicies data={ex} />
          <Interventions data={ex} />
          <ClaudeCalibration data={ex} />

          <h3 className="text-sm font-bold text-white pt-1">Notable trades</h3>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <section className="card space-y-2">
              <div className="text-sm font-semibold text-white">Best</div>
              {ins.best.length ? ins.best.map(t => <StoryCard key={t.wallet + t.id} t={t} onOpen={() => open(t)} />)
                : <p className="text-xs text-gray-500">No closed trades yet.</p>}
            </section>
            <section className="card space-y-2">
              <div className="text-sm font-semibold text-white">Worst</div>
              {ins.worst.length ? ins.worst.map(t => <StoryCard key={t.wallet + t.id} t={t} onOpen={() => open(t)} />)
                : <p className="text-xs text-gray-500">No closed trades yet.</p>}
            </section>
          </div>
        </div>
      )}
      {drawer && <TradeDrawer system={drawer.system} id={drawer.id} onClose={() => setDrawer(null)} />}
    </div>
  )
}
