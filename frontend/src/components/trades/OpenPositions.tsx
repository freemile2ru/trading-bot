import { useState } from 'react'
import clsx from 'clsx'
import { api } from '../../utils/api'
import { fmtUsd, fmtUsdSigned } from '../../utils/format'
import { useBinanceStream } from '../../hooks/useBinanceStream'
import { SYSTEM_LABEL, isLive, positionActions, type SystemKey, type UnifiedOrder, type UnifiedTrade } from '../../types/overview'

// Same look as the Trade tab's account panel (Binance palette).
const th = 'px-3 py-2 text-left font-normal text-[#848e9c] whitespace-nowrap'
const td = 'px-3 py-2 whitespace-nowrap tabular-nums'
const px = (v: number | null | undefined) => (v == null ? '—' : v.toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 2 : 6 }))
// Paper books fill at Binance mainnet prices, so their marks stream from there.
const MAINNET_MARKET_WS = 'wss://fstream.binance.com/market/stream'

function SystemTag({ s }: { s: SystemKey }) {
  const live = isLive(s)
  return (
    <span className={clsx('text-[9px] font-black px-1 py-0.5 rounded whitespace-nowrap',
      live ? 'bg-rose-600 text-white' : 'bg-[#2b3139] text-[#b7bdc6]')}>
      {SYSTEM_LABEL[s]}
    </span>
  )
}

const Side = ({ d, lev }: { d: string; lev?: number | null }) => (
  <span className={d === 'long' ? 'text-[#0ecb81]' : 'text-[#f6465d]'}>{d === 'long' ? 'Long' : 'Short'}{lev ? ` ${lev}x` : ''}</span>
)

export function OpenPositions({ positions, orders, onChanged, onOpen }: {
  positions: UnifiedTrade[]
  orders: UnifiedOrder[]
  onChanged: (msg?: string) => void
  onOpen: (system: SystemKey, id: string) => void
}) {
  const [tab, setTab] = useState<'positions' | 'orders'>('positions')
  const [marks, setMarks] = useState<Record<string, number>>({})
  const paperSymbols = [...new Set(positions.filter(p => !isLive(p.system)).map(p => p.symbol))].sort()
  useBinanceStream(paperSymbols.length ? MAINNET_MARKET_WS : undefined,
    paperSymbols.map(s => `${s.toLowerCase()}@markPrice@1s`),
    (_stream, d) => { if (d?.s && d?.p) setMarks(m => (m[d.s] === Number(d.p) ? m : { ...m, [d.s]: Number(d.p) })) })

  const unrealized = positions.reduce((a, p) => a + livePnl(p, marks).pnl, 0)
  return (
    <section className="bg-[#161a1e] rounded-lg overflow-hidden text-xs text-[#eaecef]">
      <div className="flex flex-wrap gap-x-5 px-3 pt-2 border-b border-[#2b3139]">
        {([['positions', `Positions (${positions.length})`], ['orders', `Open orders (${orders.length})`]] as const).map(([k, l]) => (
          <button key={k} onClick={() => setTab(k)}
            className={clsx('pb-2 font-semibold border-b-2', tab === k ? 'text-[#eaecef] border-[#f0b90b]' : 'text-[#848e9c] border-transparent')}>{l}</button>
        ))}
        <span className="ml-auto pb-2 text-[#848e9c] tabular-nums">
          unrealized <span className={unrealized >= 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]'}>{fmtUsdSigned(unrealized)}</span>
        </span>
      </div>
      <div className="overflow-x-auto max-h-[28rem] overflow-y-auto">
        {tab === 'positions'
          ? <Positions rows={positions} marks={marks} onChanged={onChanged} onOpen={onOpen} />
          : <Orders rows={orders} onChanged={onChanged} />}
      </div>
    </section>
  )
}

// Two-legged books: a row shows one leg (carry: the short perp; Hold-72: the coin), so its P&L can't be
// recomputed from that leg's price — the other leg (spot coin / BTC hedge) cancels most of it. Use the
// server's net figure, marked every minute from both legs.
const HEDGED = new Set(['carry', 'carry_q', 'hold72', 'fade24', 'events', 'unlocks', 'vol_premium'])

function livePnl(p: UnifiedTrade, marks: Record<string, number>): { mark: number | null; pnl: number } {
  const mark = !isLive(p.system) && !HEDGED.has(p.system) ? marks[p.symbol] : undefined
  if (mark == null || p.entry_price == null || !p.quantity) return { mark: p.mark_price, pnl: p.net_pnl ?? 0 }
  const s = p.direction === 'long' ? 1 : -1
  return { mark, pnl: (mark - p.entry_price) * s * p.quantity - (p.fees ?? 0) - (p.funding ?? 0) }
}

function Positions({ rows, marks, onChanged, onOpen }: {
  rows: UnifiedTrade[]; marks: Record<string, number>; onChanged: (m?: string) => void; onOpen: (s: SystemKey, id: string) => void
}) {
  const [edit, setEdit] = useState<{ key: string; sl: string; tp: string } | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const key = (p: UnifiedTrade) => `${p.system}:${p.id}`

  async function close(p: UnifiedTrade) {
    const live = isLive(p.system)
    if (!confirm(`${live ? 'LIVE — this sends a real market order on Binance.\n\n' : ''}Close ${p.symbol} ${p.direction} (${SYSTEM_LABEL[p.system]}) at market?`)) return
    setBusy(key(p)); setErr(null)
    try { await api.positions.close(p.system, p.id); onChanged(`${p.symbol} closed`) }
    catch (e) { setErr(e instanceof Error ? e.message : 'Close failed') }
    finally { setBusy(null) }
  }
  async function save(p: UnifiedTrade) {
    if (!edit) return
    const sl = Number(edit.sl) || null, tp = Number(edit.tp) || null
    if (isLive(p.system) && !confirm(`Move the LIVE exchange orders for ${p.symbol}?\nStop ${sl ?? 'unchanged'} · target ${tp ?? 'unchanged'}`)) return
    setBusy(key(p)); setErr(null)
    try {
      await api.positions.levels(p.system, p.id, sl, tp)
      setEdit(null); onChanged('Stop/target updated')
    } catch (e) { setErr(e instanceof Error ? e.message : 'Update failed') }
    finally { setBusy(null) }
  }

  if (!rows.length) return <p className="px-3 py-8 text-center text-[#848e9c]">No open positions.</p>
  return (
    <>
      {err && <p className="px-3 pt-2 text-[#f6465d]">{err}</p>}
      <table className="w-full">
        <thead><tr>{['Symbol', 'System', 'Side', 'Size', 'Entry', 'Mark', 'Liq.', 'Margin', 'PnL (ROE)', 'Stop / Target', 'Open for', ''].map((h, i) => <th key={i} className={th}>{h}</th>)}</tr></thead>
        <tbody>
          {rows.map(p => {
            const { mark, pnl } = livePnl(p, marks)
            const roe = p.margin ? pnl / p.margin * 100 : null
            const acts = positionActions(p.system)
            const editing = edit?.key === key(p)
            return (
              <tr key={key(p)} className="border-t border-[#2b3139] hover:bg-white/[0.02]">
                <td className={td}>
                  <button onClick={() => onOpen(p.system, p.id)} className="font-semibold hover:text-[#f0b90b]" title="Open the trade's full record">{p.symbol}</button>
                </td>
                <td className={td}><SystemTag s={p.system} /></td>
                <td className={td}>
                  {HEDGED.has(p.system)
                    ? <span className="text-[#848e9c]" title={p.note ?? undefined}>{p.system === 'carry' || p.system === 'carry_q' ? (p.system === 'carry_q' ? 'Hedged · spot vs quarterly' : 'Hedged · spot vs perp') : `${p.direction === 'long' ? 'Long' : 'Short'} vs BTC`}</span>
                    : <Side d={p.direction} lev={p.leverage} />}
                </td>
                <td className={td}>{fmtUsd(p.notional, 0)}</td>
                <td className={td}>{px(p.entry_price)}</td>
                <td className={td}>{px(mark)}</td>
                <td className={clsx(td, 'text-[#f0b90b]')}>{px(p.liquidation)}</td>
                <td className={td}>{fmtUsd(p.margin)}</td>
                <td className={clsx(td, pnl >= 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]')}>
                  {fmtUsdSigned(pnl)}{roe != null && ` (${roe.toFixed(1)}%)`}
                </td>
                <td className={td}>
                  {editing ? (
                    <span className="inline-flex gap-1 items-center">
                      <input value={edit.sl} onChange={e => setEdit({ ...edit, sl: e.target.value })} aria-label="Stop"
                        className="w-20 bg-[#2b3139] rounded px-1 py-0.5 outline-none" placeholder="stop" />
                      <input value={edit.tp} onChange={e => setEdit({ ...edit, tp: e.target.value })} aria-label="Target"
                        className="w-20 bg-[#2b3139] rounded px-1 py-0.5 outline-none" placeholder="target" />
                      <button onClick={() => save(p)} disabled={busy === key(p)} className="text-[#f0b90b]">Save</button>
                      <button onClick={() => setEdit(null)} className="text-[#848e9c]" aria-label="Cancel edit">×</button>
                    </span>
                  ) : acts.levels ? (
                    <button onClick={() => setEdit({ key: key(p), sl: String(p.stop_loss ?? ''), tp: p.tp_set === false ? '' : String(p.take_profit ?? '') })}
                      className="hover:text-[#f0b90b]" title="Edit stop / target">
                      <span className="text-[#f6465d]">{px(p.stop_loss)}</span> / <span className="text-[#0ecb81]">{p.tp_set === false ? '—' : px(p.take_profit)}</span> ✎
                    </button>
                  ) : (
                    <span><span className="text-[#f6465d]">{px(p.stop_loss)}</span> / <span className="text-[#0ecb81]">{p.tp_set === false ? '—' : px(p.take_profit)}</span></span>
                  )}
                </td>
                <td className={clsx(td, 'text-[#848e9c]')}>{fmtOpen(p.hold_min)}</td>
                <td className={td}>
                  {acts.close ? (
                    <button onClick={() => close(p)} disabled={busy === key(p)}
                      className="px-2 py-0.5 rounded bg-[#2b3139] hover:bg-[#f6465d] hover:text-white disabled:opacity-40">
                      {busy === key(p) ? '…' : 'Close'}
                    </button>
                  ) : <span className="text-[10px] text-[#848e9c]" title="Experiment books aren't touched by hand">{acts.note}</span>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </>
  )
}

function fmtOpen(min: number | null): string {
  if (min == null) return '—'
  if (min < 60) return `${Math.round(min)}m`
  const h = Math.floor(min / 60)
  return h < 24 ? `${h}h ${Math.round(min % 60)}m` : `${Math.floor(h / 24)}d ${h % 24}h`
}

function Orders({ rows, onChanged }: { rows: UnifiedOrder[]; onChanged: (m?: string) => void }) {
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  async function cancel(o: UnifiedOrder) {
    if (isLive(o.system) && !confirm(`Cancel the LIVE ${o.symbol} limit order on Binance?`)) return
    setBusy(o.id); setErr(null)
    try { await api.positions.cancelOrder(o.system, o.id); onChanged(`${o.symbol} order cancelled`) }
    catch (e) { setErr(e instanceof Error ? e.message : 'Cancel failed') }
    finally { setBusy(null) }
  }
  if (!rows.length) return <p className="px-3 py-8 text-center text-[#848e9c]">No open orders.</p>
  return (
    <>
      {err && <p className="px-3 pt-2 text-[#f6465d]">{err}</p>}
      <table className="w-full">
        <thead><tr>{['Placed', 'Symbol', 'System', 'Side', 'Limit', 'Price now', 'Size', 'Stop', 'Target', 'Expires', ''].map((h, i) => <th key={i} className={th}>{h}</th>)}</tr></thead>
        <tbody>
          {rows.map(o => (
            <tr key={`${o.system}:${o.id}`} className="border-t border-[#2b3139]">
              <td className={clsx(td, 'text-[#848e9c]')}>{o.placed_at ? new Date(o.placed_at * 1000).toLocaleString('en-GB', { dateStyle: 'short', timeStyle: 'short' }) : '—'}</td>
              <td className={clsx(td, 'font-semibold')}>{o.symbol}</td>
              <td className={td}><SystemTag s={o.system} /></td>
              <td className={td}><Side d={o.direction} lev={o.leverage} /></td>
              <td className={td}>{px(o.limit_price)}</td>
              <td className={clsx(td, 'text-[#848e9c]')}>{px(o.current_price)}</td>
              <td className={td}>{fmtUsd(o.notional_usd, 0)}</td>
              <td className={clsx(td, 'text-[#f6465d]')}>{px(o.stop_loss)}</td>
              <td className={clsx(td, 'text-[#0ecb81]')}>{px(o.take_profit)}</td>
              <td className={clsx(td, 'text-[#848e9c]')}>{o.expires_at ? new Date(o.expires_at * 1000).toLocaleString('en-GB', { dateStyle: 'short', timeStyle: 'short' }) : '—'}</td>
              <td className={td}>
                {positionActions(o.system).cancel ? (
                  <button onClick={() => cancel(o)} disabled={busy === o.id}
                    className="px-2 py-0.5 rounded bg-[#2b3139] hover:bg-[#474d57] disabled:opacity-40">Cancel</button>
                ) : <span className="text-[10px] text-[#848e9c]">{positionActions(o.system).note}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
