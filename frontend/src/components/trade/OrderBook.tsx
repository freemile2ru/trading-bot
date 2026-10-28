import { useState } from 'react'
import clsx from 'clsx'

export interface Book { bids: [number, number][]; asks: [number, number][] }
export interface Tape { price: number; qty: number; time: number; buy: boolean }

const fmt = (v: number, p: number) => v.toLocaleString(undefined, { minimumFractionDigits: p, maximumFractionDigits: p })
const fmtQty = (q: number) => (q >= 1000 ? `${(q / 1000).toFixed(2)}K` : q < 1 ? q.toFixed(4) : q.toFixed(3))

/** Depth (top 12 each side, with cumulative-size bars) and the recent trade tape. Click a price to use it. */
export function OrderBook({ book, tape, last, precision, onPick }: {
  book: Book | null; tape: Tape[]; last: number | null; precision: number; onPick: (p: number) => void
}) {
  const [view, setView] = useState<'book' | 'trades'>('book')
  const asks = (book?.asks ?? []).slice(0, 20)
  const bids = (book?.bids ?? []).slice(0, 20)
  const cum = (rows: [number, number][]) => rows.reduce<number[]>((a, [, q]) => [...a, (a[a.length - 1] ?? 0) + q], [])
  const askCum = cum(asks), bidCum = cum(bids)
  const maxCum = Math.max(askCum[askCum.length - 1] ?? 0, bidCum[bidCum.length - 1] ?? 0) || 1
  const spread = asks[0] && bids[0] ? asks[0][0] - bids[0][0] : null

  const Row = ({ p, q, c, side }: { p: number; q: number; c: number; side: 'ask' | 'bid' }) => (
    <button onClick={() => onPick(p)} className="relative w-full grid grid-cols-3 px-3 py-[2px] text-[11px] tabular-nums hover:bg-white/5 text-left">
      <span className={clsx('absolute inset-y-0 right-0', side === 'ask' ? 'bg-[#f6465d]/10' : 'bg-[#0ecb81]/10')}
            style={{ width: `${(c / maxCum) * 100}%` }} />
      <span className={clsx('relative', side === 'ask' ? 'text-[#f6465d]' : 'text-[#0ecb81]')}>{fmt(p, precision)}</span>
      <span className="relative text-right text-[#eaecef]">{fmtQty(q)}</span>
      <span className="relative text-right text-[#848e9c]">{fmtQty(c)}</span>
    </button>
  )

  return (
    <div className="flex flex-col h-full bg-[#161a1e] rounded-lg overflow-hidden">
      <div className="flex gap-4 px-3 pt-2 pb-1 text-xs font-semibold border-b border-[#2b3139]">
        {(['book', 'trades'] as const).map(v => (
          <button key={v} onClick={() => setView(v)}
            className={clsx('pb-1 border-b-2', view === v ? 'text-[#eaecef] border-[#f0b90b]' : 'text-[#848e9c] border-transparent')}>
            {v === 'book' ? 'Order Book' : 'Trades'}
          </button>
        ))}
      </div>
      {view === 'book' ? (
        <>
          <div className="grid grid-cols-3 px-3 py-1 text-[10px] text-[#848e9c]">
            <span>Price</span><span className="text-right">Size</span><span className="text-right">Total</span>
          </div>
          <div className="flex-1 flex flex-col justify-end overflow-hidden">
            {asks.map((a, i) => ({ a, i })).reverse().map(({ a, i }) => <Row key={`a${a[0]}`} p={a[0]} q={a[1]} c={askCum[i]} side="ask" />)}
          </div>
          <div className="px-3 py-1.5 flex items-baseline justify-between border-y border-[#2b3139]">
            <span className="text-base font-bold tabular-nums text-[#eaecef]">{last != null ? fmt(last, precision) : '—'}</span>
            <span className="text-[10px] text-[#848e9c]">spread {spread != null ? fmt(spread, precision) : '—'}</span>
          </div>
          <div className="flex-1 overflow-hidden">
            {bids.map((b, i) => <Row key={`b${b[0]}`} p={b[0]} q={b[1]} c={bidCum[i]} side="bid" />)}
          </div>
        </>
      ) : (
        <div className="flex-1 overflow-y-auto">
          <div className="grid grid-cols-3 px-3 py-1 text-[10px] text-[#848e9c] sticky top-0 bg-[#161a1e]">
            <span>Price</span><span className="text-right">Size</span><span className="text-right">Time</span>
          </div>
          {tape.map((t, i) => (
            <button key={i} onClick={() => onPick(t.price)} className="w-full grid grid-cols-3 px-3 py-[2px] text-[11px] tabular-nums hover:bg-white/5 text-left">
              <span className={t.buy ? 'text-[#0ecb81]' : 'text-[#f6465d]'}>{fmt(t.price, precision)}</span>
              <span className="text-right text-[#eaecef]">{fmtQty(t.qty)}</span>
              <span className="text-right text-[#848e9c]">{new Date(t.time).toLocaleTimeString('en-GB')}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
