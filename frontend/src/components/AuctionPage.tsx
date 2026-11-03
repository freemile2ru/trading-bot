import { useEffect, useState } from 'react'
import { Crosshair, RefreshCw } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import type { AuctionInstrument, AuctionSnapshot } from '../types/auction'
import type { UnifiedTrade } from '../types/overview'
import { SortHeader, useSort } from './ui/Sortable'
import { AuctionChart } from './AuctionChart'

const tone = (v: number | null | undefined) => v == null ? 'text-gray-500' : v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : 'text-gray-300'
const pct = (v: number | null | undefined, d = 2) => v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`
const when = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const px = (v: number) => v >= 1000 ? v.toFixed(1) : v >= 10 ? v.toFixed(2) : v.toPrecision(4)
const dir = (d: number) => d > 0 ? 'UP' : d < 0 ? 'DOWN' : 'RANGE'
const dirTone = (d: number) => d > 0 ? 'text-emerald-300' : d < 0 ? 'text-rose-300' : 'text-amber-300'
const GRADE_TONE = (g: number) => g >= 8 ? 'text-emerald-300' : g >= 7 ? 'text-emerald-400' : g >= 6 ? 'text-lime-300' : g >= 5 ? 'text-amber-300' : 'text-orange-300'
const letter = (g: number) => g >= 8 ? 'A+' : g === 7 ? 'A' : g === 6 ? 'B+' : g === 5 ? 'B' : 'C'

function Chip({ on, label, tone: t }: { on: boolean; label: string; tone?: string }) {
  return <span className={clsx('rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wider', on ? (t ?? 'border-emerald-800/60 bg-emerald-900/40 text-emerald-200') : 'border-gray-800 bg-gray-900 text-gray-600')}>{label}</span>
}

/** One instrument: the level ladder around price, the dashboard readings, and where the book stands in the sequence. */
function Instrument({ i, labels }: { i: AuctionInstrument; labels: Record<string, string> }) {
  if (i.error) return <section className="card"><h3 className="text-sm font-semibold text-white">{i.symbol.replace('USDT', '')}</h3><p className="text-xs text-gray-500">{i.error}</p></section>
  const c = i.context
  const ent = i.state?.entering ?? null
  const open = ent ?? i.state?.pending ?? null
  const phase = i.state?.entering ? 'confirmed — waiting for the retest' : i.state?.pending ? 'accepted — waiting for confirmation' : 'watching the levels'
  const above = i.levels.filter(l => l.price > i.price).slice(-4)
  const below = i.levels.filter(l => l.price <= i.price).slice(0, 4)
  return (
    <section className={clsx('card space-y-3', !i.in_session && 'opacity-80')}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-base font-bold text-white">{i.symbol.replace('USDT', '')}</h3>
        <span className="font-mono text-gray-200">{px(i.price)}</span>
        <span className="text-[11px] text-gray-500">{i.et} · {i.in_session ? 'NY session' : 'outside the session'}{i.atr_pct != null ? ` · ATR ${i.atr_pct}%` : ''}</span>
        <span className={clsx('ml-auto text-[11px]', i.state?.entering || i.state?.pending ? 'text-amber-300' : 'text-gray-500')}>{phase}</span>
      </div>
      {/* the ladder: nearest levels above and below price, distance in bps */}
      <div className="grid grid-cols-[auto_1fr_auto] gap-x-3 gap-y-0.5 text-[11px]">
        {above.map(l => (
          <div key={l.name} className="contents">
            <span className="font-mono text-gray-300 text-right">{px(l.price)}</span>
            <span className={clsx('truncate', i.next_above.name === l.name ? 'text-emerald-200 font-semibold' : 'text-gray-400')} title={labels[l.name] ?? l.name}>{l.name}{i.next_above.name === l.name ? ' · next target up' : ''}</span>
            <span className="font-mono text-gray-500 text-right">+{l.bps.toFixed(0)} bps</span>
          </div>
        ))}
        <div className="contents">
          <span className="font-mono text-white text-right font-semibold">{px(i.price)}</span>
          <span className="text-white font-semibold">price · VWAP {px(i.vwap)} · EMA20 {px(i.ema20)} · EMA50 {px(i.ema50)}</span>
          <span />
        </div>
        {below.map(l => (
          <div key={l.name} className="contents">
            <span className="font-mono text-gray-300 text-right">{px(l.price)}</span>
            <span className={clsx('truncate', i.next_below.name === l.name ? 'text-rose-200 font-semibold' : 'text-gray-400')} title={labels[l.name] ?? l.name}>{l.name}{i.next_below.name === l.name ? ' · next target down' : ''}</span>
            <span className="font-mono text-gray-500 text-right">{l.bps.toFixed(0)} bps</span>
          </div>
        ))}
        {!i.orb_valid && <div className="col-span-3 text-[10px] text-gray-600">Opening-range and initial-balance levels appear after 10:00 / 10:30 ET.</div>}
      </div>
      {/* the dashboard labels the study paints at the top of the chart */}
      <div className="flex flex-wrap gap-1.5">
        <span className={clsx('rounded border border-gray-800 bg-gray-900 px-1.5 py-0.5 text-[10px] font-semibold', dirTone(c.trend30))}>30M {dir(c.trend30)}</span>
        <span className={clsx('rounded border border-gray-800 bg-gray-900 px-1.5 py-0.5 text-[10px] font-semibold', dirTone(c.trend15))}>15M {dir(c.trend15)}</span>
        <span className={clsx('rounded border border-gray-800 bg-gray-900 px-1.5 py-0.5 text-[10px] font-semibold', dirTone(c.master))}>MASTER {dir(c.master)}</span>
        <span className={clsx('rounded border border-gray-800 bg-gray-900 px-1.5 py-0.5 text-[10px] font-semibold', dirTone(c.bias))}>5M {c.bias === 0 ? 'NO BIAS' : `${c.bias > 0 ? 'L' : 'S'} ${c.bias_kind.toUpperCase()}`}{c.bias !== 0 && !c.healthy ? ' · PL9' : ''}</span>
        <Chip on={c.indecision30} label="indecision" tone="border-amber-800/60 bg-amber-900/40 text-amber-200" />
        <Chip on={c.divergence} label="divergence" tone="border-fuchsia-800/60 bg-fuchsia-900/40 text-fuchsia-200" />
        <Chip on={c.pullback} label="pullback" tone="border-amber-800/60 bg-amber-900/40 text-amber-200" />
        <Chip on={c.caution} label="weak mom" tone="border-orange-800/60 bg-orange-900/40 text-orange-200" />
        <Chip on={c.reaccel} label="re-accel" tone="border-cyan-800/60 bg-cyan-900/40 text-cyan-200" />
        <Chip on={c.atr_low} label="atr low" tone="border-gray-700 bg-gray-800 text-gray-300" />
        <span className={clsx('rounded border border-gray-800 bg-gray-900 px-1.5 py-0.5 text-[10px]', c.vol_pressure >= 40 ? 'text-emerald-300' : c.vol_pressure <= -40 ? 'text-rose-300' : 'text-gray-400')}>vol pressure {c.vol_pressure.toFixed(0)}</span>
      </div>
      {/* what a long or a short would grade right now */}
      <div className="grid grid-cols-2 gap-2 text-[11px]">
        {([['long', i.grade_long], ['short', i.grade_short]] as const).map(([side, g]) => (
          <div key={side} className="rounded bg-gray-800/40 px-2 py-1.5">
            <div className="text-[10px] uppercase tracking-wider text-gray-500">a {side} right now</div>
            {g.stand_down ? <div className="text-red-300 font-semibold">STAND DOWN — {g.reasons.join(' ')}</div>
              : <div className={clsx('font-semibold', GRADE_TONE(g.score))}>grade {g.score}/8 ({letter(g.score)})</div>}
          </div>
        ))}
      </div>
      {open && (
        <div className="rounded-lg border border-amber-900/60 bg-amber-950/20 px-3 py-2 text-[11px] text-amber-100/90">
          <b>{open.direction} {i.symbol.replace('USDT', '')}</b> accepted above <b>{open.level_name}</b> {px(open.level)} · stop {px(open.stop)} · target {open.target_name} {px(open.target)}
          {ent && <> · grade {ent.grade}/8 · target {ent.target_bps.toFixed(0)} bps away{ent.waited ? ` · retest bar ${ent.waited}/3` : ''}</>}
        </div>
      )}
      {i.gate && <div className="text-[11px] text-amber-300">Gated: {i.gate}</div>}
    </section>
  )
}

const extra = (t: UnifiedTrade) => t as unknown as Record<string, unknown>

/** The book's trades, open and closed, sortable — level accepted, target level, grade, exit, R and net. */
function TradesTable({ rows }: { rows: UnifiedTrade[] }) {
  const sorted = useSort(rows, { opened: t => t.opened_ts, closed: t => t.closed_ts, symbol: t => t.symbol, level: t => String(extra(t).level_name ?? ''), grade: t => Number(extra(t).grade ?? 0),
    entry: t => t.entry_price, notional: t => t.notional, r: t => t.r, net: t => t.net_pnl, exit: t => t.exit_reason })
  if (!rows.length) return <div className="text-xs text-gray-500">No trades yet — the book trades in the New York session, 09:30–17:00 ET.</div>
  const cols = [['opened', 'Opened', false], ['closed', 'Closed', false], ['symbol', 'Trade', false], ['level', 'Level → target', false], ['grade', 'Grade', true], ['entry', 'Entry → exit', true], ['notional', 'Notional', true], ['exit', 'Exit', false], ['r', 'R', true], ['net', 'Net', true]] as const
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs min-w-[56rem]">
        <thead><tr className="text-[10px] uppercase tracking-wider text-gray-500 text-left">
          {cols.map(([k, l, right]) => <th key={k} className={clsx('py-1 pr-2 font-normal', right && 'text-right')}><SortHeader k={k} sort={sorted.sort} onSort={sorted.toggle} align={right ? 'right' : 'left'}>{l}</SortHeader></th>)}
        </tr></thead>
        <tbody>
          {sorted.rows.map(t => (
            <tr key={t.id} className={clsx('border-t border-gray-800/70', t.status === 'open' && 'bg-amber-950/10')}>
              <td className="py-1 pr-2 text-gray-500 whitespace-nowrap">{t.opened_ts ? when(t.opened_ts) : '—'}</td>
              <td className="py-1 pr-2 text-gray-500 whitespace-nowrap">{t.closed_ts ? when(t.closed_ts) : <span className="text-amber-300">open</span>}</td>
              <td className="py-1 pr-2"><span className={clsx('font-semibold', t.direction === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{t.direction}</span> <span className="font-mono text-gray-200">{t.symbol.replace('USDT', '')}</span></td>
              <td className="py-1 pr-2 text-gray-400">{String(extra(t).level_name ?? '—')} → {String(extra(t).target_name ?? '—')}</td>
              <td className="py-1 pr-2 text-right text-gray-300">{extra(t).grade != null ? `${String(extra(t).grade)}/8` : '—'}</td>
              <td className="py-1 pr-2 text-right font-mono text-gray-300">{t.entry_price != null ? px(t.entry_price) : '—'} → {t.status === 'open' ? (t.mark_price != null ? px(t.mark_price) : '—') : (t.exit_price != null ? px(t.exit_price) : '—')}</td>
              <td className="py-1 pr-2 text-right font-mono text-gray-300">{t.notional != null ? fmtUsd(t.notional, 0) : '—'}</td>
              <td className="py-1 pr-2 text-gray-400">{t.exit_reason?.replace(/_/g, ' ') ?? '—'}</td>
              <td className={clsx('py-1 pr-2 text-right font-mono', tone(t.r))}>{t.r == null ? '—' : `${t.r > 0 ? '+' : ''}${t.r.toFixed(2)}`}</td>
              <td className={clsx('py-1 text-right font-mono', tone(t.net_pnl))}>{fmtUsdSigned(t.net_pnl ?? 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** The mentor's system on its own page: a different project from the autotrader and the main wallet. */
export function AuctionPage() {
  const [d, setD] = useState<AuctionSnapshot | null>(null)
  const [trades, setTrades] = useState<UnifiedTrade[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [view, setView] = useState<'dashboard' | 'charts' | 'trades'>(() => (window.location.hash.includes('/charts') ? 'charts' : window.location.hash.includes('/trades') ? 'trades' : 'dashboard'))
  const [tradeSym, setTradeSym] = useState<string>('')
  const [chartSym, setChartSym] = useState<string>('')
  const load = async () => {
    try {
      const [s, t] = await Promise.all([api.auction.status(), api.trades.all({ system: 'auction', sort: 'closed_ts', order: 'desc', page: 1, page_size: 200 })])
      setD(s); setTrades(t.rows); setErr(null)
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }
  useEffect(() => { load(); const id = setInterval(load, 60_000); return () => clearInterval(id) }, [])
  const runNow = async () => { setBusy(true); try { const out = await api.auction.tick(); setD(out.snapshot) } finally { setBusy(false) } }

  if (err && !d) return <div className="card text-sm text-rose-300">Mentor’s system unavailable: {err}</div>
  if (!d) return <div className="card text-sm text-gray-500">Loading…</div>
  const b = d.book
  const v = d.verdict
  const vTone = v.status === 'edge' ? 'text-emerald-300' : v.status === 'no_edge' ? 'text-red-300' : 'text-gray-300'
  const vLabel = v.status === 'collecting' ? `Collecting — ${v.trades}/${v.needed} trades` : v.status === 'edge' ? 'Edge shown' : v.status === 'no_edge' ? 'No edge' : 'Inconclusive'
  return (
    <div className="space-y-4">
      <section className="card flex flex-wrap items-center gap-3">
        <Crosshair className="w-5 h-5 text-amber-400" />
        <div>
          <h2 className="text-sm font-semibold text-white">Mentor’s system — auction levels on stock and index perps</h2>
          <p className="text-[11px] text-gray-500">The levels are plotted, price accepts beyond one, the next close confirms, the retest is the entry, the level beyond the nearest is the exit. Its own paper book ({fmtUsd(b.initial, 0)}), nothing to do with the autotrader or the main wallet.</p>
        </div>
        <div className="ml-auto flex items-center gap-1 rounded-lg bg-gray-800 p-0.5 text-xs">
          {(['dashboard', 'charts', 'trades'] as const).map(v => <button key={v} onClick={() => { setView(v); window.history.replaceState(null, '', v === 'dashboard' ? '#mentor' : `#mentor/${v}`) }} className={clsx('px-3 py-1 rounded-md capitalize', view === v ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-gray-200')}>{v}</button>)}
        </div>
        <button onClick={runNow} disabled={busy} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 disabled:opacity-50">
          <RefreshCw className={clsx('w-3.5 h-3.5', busy && 'animate-spin')} /> {busy ? 'Running…' : 'Run the sequence now'}
        </button>
      </section>

      {view === 'charts' && <AuctionChart symbols={d.config.symbols} symbol={chartSym || d.config.symbols[0]} onSymbol={setChartSym} />}
      {view === 'trades' && (
        <section className="card space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-white">This book’s trades</h3>
            <span className="text-[11px] text-gray-500">Only the mentor’s system — these rows are kept off the main Trades tab.</span>
            <div className="ml-auto flex gap-1 text-xs">
              <button onClick={() => setTradeSym('')} className={clsx('rounded border px-2 py-0.5', !tradeSym ? 'border-gray-600 text-gray-100' : 'border-gray-800 text-gray-400')}>all</button>
              {d.config.symbols.map(sy => <button key={sy} onClick={() => setTradeSym(sy)} className={clsx('rounded border px-2 py-0.5', tradeSym === sy ? 'border-gray-600 text-gray-100' : 'border-gray-800 text-gray-400')}>{sy.replace('USDT', '')}</button>)}
            </div>
          </div>
          <TradesTable rows={trades.filter(t => !tradeSym || t.symbol === tradeSym)} />
        </section>
      )}
      {view === 'dashboard' && <>
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Book</div><div className="text-xl font-black text-white tabular-nums mt-1">{fmtUsd(b.equity, 0)}</div><div className={clsx('text-xs', tone(b.return_pct))}>{pct(b.return_pct)} since start · {b.enabled ? 'on' : 'off'}</div></div>
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Record</div><div className="text-xl font-black text-white tabular-nums mt-1">{b.closed} closed</div><div className="text-xs text-gray-400">{b.wins} won · avg {b.avg_r == null ? '—' : `${b.avg_r > 0 ? '+' : ''}${b.avg_r}R`} · <span className={tone(b.net_pnl)}>{fmtUsdSigned(b.net_pnl, 0)}</span></div></div>
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Verdict</div><div className={clsx('text-xl font-black tabular-nums mt-1', vTone)}>{vLabel}</div><div className="text-xs text-gray-400">{v.expectancy_usd != null ? `${fmtUsdSigned(v.expectancy_usd)} a trade (95% ${v.ci[0]} … ${v.ci[1]})` : 'judged on its own 40 trades'}</div></div>
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">$/day vs goal</div><div className={clsx('text-xl font-black tabular-nums mt-1', tone(d.daily.today))}>{fmtUsdSigned(d.daily.today, 0)}<span className="text-sm text-gray-500 font-normal"> today</span></div><div className="text-xs text-gray-400">7-day avg <span className={tone(d.daily.avg7)}>{fmtUsdSigned(d.daily.avg7, 0)}</span> · goal {fmtUsd(d.daily.goal[0], 0)}–{fmtUsd(d.daily.goal[1], 0)} a day · {b.open.length} open</div></div>
        <div className="card"><div className="text-[10px] uppercase tracking-wider text-gray-500">Rules</div><div className="text-xs text-gray-300 mt-1">{d.config.session} · entry {String(d.config.variant.entry)} · target {String(d.config.variant.target_mode)} level · grade ≥ {String(d.config.variant.min_grade)} · target ≥ {String(d.config.variant.min_target_bps)} bps · risk {d.config.risk_pct}% a trade · calendar gates</div></div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        {d.instruments.map(i => <Instrument key={i.symbol} i={i} labels={d.level_labels} />)}
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="card space-y-2">
          <h3 className="text-sm font-semibold text-white">Decisions</h3>
          {!d.log.length ? <div className="text-xs text-gray-500">Nothing yet — decisions appear as closed bars accept, confirm, retest, enter or are gated.</div> : (
            <table className="w-full text-xs">
              <tbody>
                {d.log.map((e, k) => (
                  <tr key={k} className="border-t border-gray-800/70">
                    <td className="py-1 text-gray-500 whitespace-nowrap">{when(e.ts)}</td>
                    <td className="py-1 font-mono text-gray-200">{e.sym.replace('USDT', '')}</td>
                    <td className={clsx('py-1 font-semibold', e.event === 'opened' ? 'text-emerald-300' : e.event === 'exit_through_level' ? 'text-rose-300' : e.event === 'gated' || e.event === 'stand_down' ? 'text-amber-300' : 'text-gray-300')}>{e.event.replace(/_/g, ' ')}</td>
                    <td className="py-1 text-gray-400">{[e.direction, e.level, e.grade != null ? `grade ${e.grade}` : null, e.target ? `→ ${e.target}` : null, e.target_bps != null ? `${e.target_bps.toFixed(0)} bps` : null, e.why].filter(Boolean).join(' · ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
        <section className="card space-y-2">
          <h3 className="text-sm font-semibold text-white">The replay behind it</h3>
          {!d.replay ? <div className="text-xs text-gray-500">No replay results on file — run research/auction_lab.py.</div> : (
            <>
              <p className="text-[11px] text-gray-500">{d.replay.variant_verdict}</p>
              <table className="w-full text-xs">
                <thead><tr className="text-[10px] uppercase tracking-wider text-gray-500 text-left"><th className="py-1 font-normal">Arrangement</th><th className="py-1 font-normal text-right">Trades</th><th className="py-1 font-normal text-right">Binance costs</th><th className="py-1 font-normal text-right">Futures costs</th><th className="py-1 font-normal text-right">Halves (futures)</th></tr></thead>
                <tbody>
                  {Object.entries(d.replay.variants).map(([k, r]) => (
                    <tr key={k} className="border-t border-gray-800/70">
                      <td className="py-1 text-gray-300">{k}</td><td className="py-1 text-right tabular-nums text-gray-400">{r.trades}</td>
                      <td className={clsx('py-1 text-right font-mono', tone(r.binance))}>{pct(r.binance, 3)}</td>
                      <td className={clsx('py-1 text-right font-mono', tone(r.futures))}>{pct(r.futures, 3)}</td>
                      <td className="py-1 text-right font-mono text-gray-500">{pct(r.halves_futures[0], 3)} / {pct(r.halves_futures[1], 3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-[10px] text-gray-600">Per trade after costs, QQQ + NVDA + TSLA pooled, 7 months of 5-minute bars. Binance perps cost 8 bps a side; CME micro futures about 0.5.</p>
            </>
          )}
        </section>
      </div>

      <section className="card space-y-2">
        <div className="flex items-baseline gap-2"><h3 className="text-sm font-semibold text-white">Recent trades</h3><button onClick={() => { setView('trades'); window.history.replaceState(null, '', '#mentor/trades') }} className="text-[11px] text-blue-400 hover:text-blue-300">all trades →</button></div>
        <TradesTable rows={trades.slice(0, 12)} />
      </section>
      </>}
    </div>
  )
}
