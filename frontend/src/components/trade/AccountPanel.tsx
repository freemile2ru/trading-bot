import { useState } from 'react'
import clsx from 'clsx'
import { api } from '../../utils/api'
import { fmtUsd, fmtUsdSigned } from '../../utils/format'
import type { ClosedTrade, ExchangePosition, PendingOrder, Position, TradeAccount } from '../../types/trade'

const Mode = ({ m }: { m: 'paper' | 'live' }) => (
  <span className={clsx('text-[9px] font-black px-1 py-0.5 rounded', m === 'live' ? 'bg-rose-600 text-white' : 'bg-[#2b3139] text-[#848e9c]')}>
    {m === 'live' ? 'LIVE' : 'PAPER'}
  </span>
)
const Side = ({ d, lev }: { d: string; lev?: number }) => (
  <span className={d === 'long' ? 'text-[#0ecb81]' : 'text-[#f6465d]'}>{d === 'long' ? 'Long' : 'Short'}{lev ? ` ${lev}x` : ''}</span>
)
const px = (v: number | null | undefined) => (v == null ? '—' : v.toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 2 : 6 }))
const th = 'px-3 py-2 text-left font-normal text-[#848e9c] whitespace-nowrap'
const td = 'px-3 py-2 whitespace-nowrap tabular-nums'

/** Positions (live P&L on the selected symbol), open limit orders, and recent history — both main ledgers. */
export function AccountPanel({ account, marks, onChanged, onPickSymbol }: {
  account: TradeAccount | null; marks: Record<string, number>; onChanged: (msg?: string) => void
  onPickSymbol: (s: string) => void
}) {
  const [tab, setTab] = useState<'positions' | 'orders' | 'history' | 'exchange'>('positions')
  const pos = account?.positions ?? []
  const ex = account?.exchange ?? []
  const orders = account?.orders ?? []
  const hist = account?.history ?? []

  return (
    <div className="bg-[#161a1e] rounded-lg overflow-hidden text-xs text-[#eaecef]">
      <div className="flex gap-5 px-3 pt-2 border-b border-[#2b3139]">
        {([['positions', `Positions (${pos.length})`], ['orders', `Open orders (${orders.length})`], ['history', 'Trade history'],
           ...(account?.mode === 'live' ? [['exchange', `Binance account (${ex.length})`] as const] : [])] as const).map(([k, l]) => (
          <button key={k} onClick={() => setTab(k)}
            className={clsx('pb-2 font-semibold border-b-2', tab === k ? 'text-[#eaecef] border-[#f0b90b]' : 'text-[#848e9c] border-transparent')}>{l}</button>
        ))}
        {account && (
          <span className="ml-auto pb-2 text-[#848e9c] tabular-nums">
            {account.mode === 'live' ? 'Binance' : 'Paper'} equity <span className="text-[#eaecef]">{fmtUsd(account.balance.equity)}</span>
            {' · '}unrealized <span className={account.balance.unrealized >= 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]'}>{fmtUsdSigned(account.balance.unrealized)}</span>
            {account.balance.error && <span className="text-[#f6465d]"> · {account.balance.error}</span>}
          </span>
        )}
      </div>
      <div className="overflow-x-auto max-h-80 overflow-y-auto">
        {tab === 'positions' && <Positions rows={pos} marks={marks} onChanged={onChanged} onPick={onPickSymbol} />}
        {tab === 'orders' && <Orders rows={orders} onChanged={onChanged} onPick={onPickSymbol} />}
        {tab === 'history' && <History rows={hist} onPick={onPickSymbol} />}
        {tab === 'exchange' && <Exchange rows={ex} onPick={onPickSymbol} />}
      </div>
    </div>
  )
}

function Positions({ rows, marks, onChanged, onPick }: {
  rows: Position[]; marks: Record<string, number>; onChanged: (m?: string) => void; onPick: (s: string) => void
}) {
  const [edit, setEdit] = useState<{ id: string; sl: string; tp: string } | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  async function close(p: Position) {
    if (p.mode === 'live' && !confirm(`Close LIVE ${p.symbol} ${p.direction} at market on Binance?`)) return
    setBusy(p.id); setErr(null)
    try { await api.trade.close(p.id); onChanged(`${p.symbol} closed`) }
    catch (e) { setErr(e instanceof Error ? e.message : 'Close failed') }
    finally { setBusy(null) }
  }
  async function save() {
    if (!edit) return
    setBusy(edit.id); setErr(null)
    try {
      await api.trade.levels(edit.id, Number(edit.sl) || null, Number(edit.tp) || null)
      setEdit(null); onChanged('Stop/target updated')
    } catch (e) { setErr(e instanceof Error ? e.message : 'Update failed') }
    finally { setBusy(null) }
  }

  if (!rows.length) return <p className="px-3 py-8 text-center text-[#848e9c]">No open positions.</p>
  return (
    <>
      {err && <p className="px-3 pt-2 text-[#f6465d]">{err}</p>}
      <table className="w-full">
        <thead><tr>{['Symbol', '', 'Side', 'Size', 'Entry', 'Mark', 'Liq.', 'Margin', 'PnL (ROE)', 'Stop / Target', ''].map((h, i) => <th key={i} className={th}>{h}</th>)}</tr></thead>
        <tbody>
          {rows.map(p => {
            const mark = marks[p.symbol] ?? p.current_price
            const s = p.direction === 'long' ? 1 : -1
            const pnl = marks[p.symbol] != null ? (mark - p.entry_price) * s * p.quantity - p.fees_usd : p.pnl_usd
            const roe = p.margin_usd ? pnl / p.margin_usd * 100 : 0
            const editing = edit?.id === p.id
            return (
              <tr key={p.id} className="border-t border-[#2b3139] hover:bg-white/[0.02]">
                <td className={td}><button onClick={() => onPick(p.symbol)} className="font-semibold hover:text-[#f0b90b]">{p.symbol}</button>
                  {p.strategy && p.strategy !== 'manual' && <span className="ml-1 text-[10px] text-[#848e9c]">({p.strategy})</span>}</td>
                <td className={td}><Mode m={p.mode} /></td>
                <td className={td}><Side d={p.direction} lev={p.leverage} /></td>
                <td className={td}>{fmtUsd(p.notional_usd, 0)}</td>
                <td className={td}>{px(p.entry_price)}</td>
                <td className={td}>{px(mark)}</td>
                <td className={clsx(td, 'text-[#f0b90b]')}>{px(p.liquidation_price)}</td>
                <td className={td}>{fmtUsd(p.margin_usd)}</td>
                <td className={clsx(td, pnl >= 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]')}>{fmtUsdSigned(pnl)} ({roe.toFixed(1)}%)</td>
                <td className={td}>
                  {editing ? (
                    <span className="inline-flex gap-1 items-center">
                      <input value={edit.sl} onChange={e => setEdit({ ...edit, sl: e.target.value })} className="w-20 bg-[#2b3139] rounded px-1 py-0.5 outline-none" placeholder="stop" />
                      <input value={edit.tp} onChange={e => setEdit({ ...edit, tp: e.target.value })} className="w-20 bg-[#2b3139] rounded px-1 py-0.5 outline-none" placeholder="target" />
                      <button onClick={save} disabled={busy === p.id} className="text-[#f0b90b]">Save</button>
                      <button onClick={() => setEdit(null)} className="text-[#848e9c]">×</button>
                    </span>
                  ) : (
                    <button onClick={() => setEdit({ id: p.id, sl: String(p.stop_loss), tp: p.tp_set === false ? '' : String(p.take_profit_2) })}
                      className="hover:text-[#f0b90b]" title="Edit stop / target">
                      <span className="text-[#f6465d]">{px(p.stop_loss)}</span> / <span className="text-[#0ecb81]">{p.tp_set === false ? '—' : px(p.take_profit_2)}</span> ✎
                    </button>
                  )}
                </td>
                <td className={td}>
                  <button onClick={() => close(p)} disabled={busy === p.id}
                    className="px-2 py-0.5 rounded bg-[#2b3139] hover:bg-[#f6465d] hover:text-white disabled:opacity-40">Close</button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </>
  )
}

function Orders({ rows, onChanged, onPick }: { rows: PendingOrder[]; onChanged: (m?: string) => void; onPick: (s: string) => void }) {
  const [err, setErr] = useState<string | null>(null)
  async function cancel(o: PendingOrder) {
    try { await api.trade.cancel(o.id); onChanged(`${o.symbol} order cancelled`) }
    catch (e) { setErr(e instanceof Error ? e.message : 'Cancel failed') }
  }
  if (!rows.length) return <p className="px-3 py-8 text-center text-[#848e9c]">No open orders.</p>
  return (
    <>
      {err && <p className="px-3 pt-2 text-[#f6465d]">{err}</p>}
      <table className="w-full">
        <thead><tr>{['Placed', 'Symbol', '', 'Side', 'Limit', 'Size', 'Stop', 'Expires', ''].map((h, i) => <th key={i} className={th}>{h}</th>)}</tr></thead>
        <tbody>
          {rows.map(o => (
            <tr key={o.id} className="border-t border-[#2b3139]">
              <td className={clsx(td, 'text-[#848e9c]')}>{new Date(o.placed_at * 1000).toLocaleString('en-GB', { dateStyle: 'short', timeStyle: 'short' })}</td>
              <td className={td}><button onClick={() => onPick(o.symbol)} className="font-semibold hover:text-[#f0b90b]">{o.symbol}</button></td>
              <td className={td}><Mode m={o.mode} /></td>
              <td className={td}><Side d={o.direction} lev={o.leverage} /></td>
              <td className={td}>{px(o.limit_price)}</td>
              <td className={td}>{fmtUsd(o.notional_usd, 0)}</td>
              <td className={clsx(td, 'text-[#f6465d]')}>{px(o.stop_loss)}</td>
              <td className={clsx(td, 'text-[#848e9c]')}>{new Date(o.expires_at * 1000).toLocaleDateString('en-GB')}</td>
              <td className={td}><button onClick={() => cancel(o)} className="px-2 py-0.5 rounded bg-[#2b3139] hover:bg-[#474d57]">Cancel</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function History({ rows, onPick }: { rows: ClosedTrade[]; onPick: (s: string) => void }) {
  if (!rows.length) return <p className="px-3 py-8 text-center text-[#848e9c]">No closed trades yet.</p>
  return (
    <table className="w-full">
      <thead><tr>{['Closed', 'Symbol', '', 'Side', 'Entry → exit', 'PnL', 'R', 'Exit', 'Source'].map((h, i) => <th key={i} className={th}>{h}</th>)}</tr></thead>
      <tbody>
        {rows.map(t => (
          <tr key={`${t.mode}${t.id}`} className="border-t border-[#2b3139]">
            <td className={clsx(td, 'text-[#848e9c]')}>{new Date(t.closed_at).toLocaleString('en-GB', { dateStyle: 'short', timeStyle: 'short' })}</td>
            <td className={td}><button onClick={() => onPick(t.symbol)} className="font-semibold hover:text-[#f0b90b]">{t.symbol}</button></td>
            <td className={td}><Mode m={t.mode} /></td>
            <td className={td}><Side d={t.direction} lev={t.leverage} /></td>
            <td className={td}>{px(t.entry_price)} → {px(t.current_price)}</td>
            <td className={clsx(td, t.pnl_usd >= 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]')}>{fmtUsdSigned(t.pnl_usd)}</td>
            <td className={td}>{t.risk_usd ? `${(t.pnl_usd / t.risk_usd).toFixed(2)}R` : '—'}</td>
            <td className={clsx(td, 'capitalize text-[#848e9c]')}>{t.close_reason?.replace(/_/g, ' ')}</td>
            <td className={clsx(td, 'text-[#848e9c]')}>{t.strategy === 'manual' ? 'Trade tab' : t.strategy ?? 'signal'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

const OWNER: Record<ExchangePosition['owner'], [string, string]> = {
  main: ['Main wallet', 'bg-blue-900/50 text-blue-300'], auto: ['Autotrader', 'bg-violet-900/50 text-violet-300'],
  unmanaged: ['Unmanaged', 'bg-red-900/60 text-red-200'],
}

/** Straight from Binance: every open position on the account and which system manages it. */
function Exchange({ rows, onPick }: { rows: ExchangePosition[]; onPick: (s: string) => void }) {
  if (!rows.length) return <p className="px-3 py-8 text-center text-[#848e9c]">No positions open on the Binance account.</p>
  return (
    <>
      <p className="px-3 pt-2 text-[11px] text-[#848e9c]">
        Read directly from Binance. Positions are managed by the system shown — stops, targets and exits run there.
        {rows.some(r => r.owner === 'unmanaged') && <span className="text-[#f6465d]"> Unmanaged positions aren't tracked by any system — close them on Binance or with the kill switch.</span>}
      </p>
      <table className="w-full">
        <thead><tr>{['Symbol', 'Managed by', 'Side', 'Size', 'Entry', 'Mark', 'Liq.', 'Margin', 'Unrealized', 'Stop', 'Target'].map((h, i) => <th key={i} className={th}>{h}</th>)}</tr></thead>
        <tbody>
          {rows.map(p => (
            <tr key={p.symbol} className="border-t border-[#2b3139]">
              <td className={td}><button onClick={() => onPick(p.symbol)} className="font-semibold hover:text-[#f0b90b]">{p.symbol}</button></td>
              <td className={td}><span className={clsx('text-[10px] font-semibold px-1.5 py-0.5 rounded', OWNER[p.owner][1])}>{OWNER[p.owner][0]}</span></td>
              <td className={td}><Side d={p.side} lev={p.leverage} /></td>
              <td className={td}>{fmtUsd(p.notional, 0)} <span className="text-[#848e9c]">({p.qty})</span></td>
              <td className={td}>{px(p.entry)}</td>
              <td className={td}>{px(p.mark)}</td>
              <td className={clsx(td, 'text-[#f0b90b]')}>{p.liquidation ? px(p.liquidation) : '—'}</td>
              <td className={td}>{fmtUsd(p.margin)}</td>
              <td className={clsx(td, p.unrealized >= 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]')}>{fmtUsdSigned(p.unrealized)}</td>
              <td className={clsx(td, p.stop ? 'text-[#f6465d]' : 'text-[#f6465d] font-bold')}>{p.stop ? px(p.stop) : 'NONE'}</td>
              <td className={clsx(td, 'text-[#0ecb81]')}>{p.target ? px(p.target) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
