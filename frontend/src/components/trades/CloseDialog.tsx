import { useEffect, useState } from 'react'
import clsx from 'clsx'
import { api } from '../../utils/api'
import { fmtUsd, fmtUsdSigned } from '../../utils/format'
import { SYSTEM_LABEL, isLive, type ClosePreview, type UnifiedTrade } from '../../types/overview'

const px = (v: number | null | undefined) => (v == null ? '—' : v.toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 2 : 6 }))

/** The Close button's confirmation (2026-11-17, the user: "the close button will prompt the PNL and fees, then ask for
 *  confirmation before executing"): the live mark, the fill after slippage, the fee the close costs, funding so far and
 *  the net P&L, refreshed every few seconds until the person confirms or backs out. */
export function CloseDialog({ trade, onConfirm, onCancel, busy }: {
  trade: UnifiedTrade; onConfirm: () => void; onCancel: () => void; busy: boolean
}) {
  const [p, setP] = useState<ClosePreview | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const live = isLive(trade.system)
  useEffect(() => {
    let stale = false
    const load = () => api.positions.preview(trade.system, trade.id)
      .then(x => { if (!stale) { setP(x); setErr(null) } })
      .catch(e => { if (!stale) setErr(e instanceof Error ? e.message : 'Could not price the close') })
    load()
    const id = setInterval(load, 3000)
    return () => { stale = true; clearInterval(id) }
  }, [trade.system, trade.id])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])
  const net = p?.net_pnl ?? null
  const roe = net != null && p?.margin_usd ? net / p.margin_usd * 100 : null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" role="dialog" aria-modal="true" aria-label="Close position">
      <div className="absolute inset-0 bg-black/60" onClick={onCancel} />
      <div className="relative w-full max-w-md bg-[#1e2329] border border-[#2b3139] rounded-lg shadow-2xl text-xs text-[#eaecef]">
        <div className="px-4 py-3 border-b border-[#2b3139] flex items-center gap-2">
          <span className="font-bold text-sm">Close {trade.symbol}</span>
          <span className={clsx('font-semibold', trade.direction === 'long' ? 'text-[#0ecb81]' : 'text-[#f6465d]')}>{trade.direction === 'long' ? 'Long' : 'Short'}{trade.leverage ? ` ${trade.leverage}x` : ''}</span>
          <span className={clsx('ml-auto text-[9px] font-black px-1 py-0.5 rounded', live ? 'bg-rose-600 text-white' : 'bg-[#2b3139] text-[#b7bdc6]')}>{SYSTEM_LABEL[trade.system]}</span>
        </div>
        <div className="px-4 py-3 space-y-1 tabular-nums">
          {live && <p className="mb-2 text-[#f6465d] font-semibold">LIVE — this sends a real market order on Binance.</p>}
          {err && <p className="text-[#f6465d]">{err}</p>}
          <Row k="Size" v={p ? `${fmtUsd(p.notional_usd, 0)} · ${px(p.quantity)} units` : '…'} />
          <Row k="Entry" v={px(p?.entry_price ?? trade.entry_price)} />
          <Row k="Mark now" v={px(p?.mark)} />
          <Row k="Fill after slippage" v={px(p?.fill)} />
          <div className="border-t border-[#2b3139] my-2" />
          <Row k="Gross P&L" v={p ? fmtUsdSigned(p.gross_pnl) : '…'} tone={p ? sign(p.gross_pnl) : undefined} />
          <Row k="Fees so far" v={p ? `−${fmtUsd(p.fees_so_far)}` : '…'} />
          <Row k="Closing fee" v={p ? `−${fmtUsd(p.exit_fee)}` : '…'} />
          <Row k="Funding" v={p ? fmtUsdSigned(-p.funding) : '…'} />
          <div className="border-t border-[#2b3139] my-2" />
          <Row k="Net P&L" v={net == null ? '…' : `${fmtUsdSigned(net)}${roe != null ? ` (${roe >= 0 ? '+' : ''}${roe.toFixed(2)}%)` : ''}`} tone={net == null ? undefined : sign(net)} big />
          {p?.note && <p className="text-[#848e9c] pt-1">{p.note}</p>}
          <p className="text-[#848e9c] pt-1">Prices refresh every few seconds until you confirm.</p>
        </div>
        <div className="px-4 py-3 border-t border-[#2b3139] flex justify-end gap-2">
          <button onClick={onCancel} disabled={busy} className="px-3 py-1.5 rounded bg-[#2b3139] hover:bg-[#363c45]">Keep it open</button>
          <button onClick={onConfirm} disabled={busy || !p} className={clsx('px-3 py-1.5 rounded font-semibold text-white disabled:opacity-40', live ? 'bg-rose-600 hover:bg-rose-500' : 'bg-[#f6465d] hover:bg-[#ff6d7d]')}>
            {busy ? 'Closing…' : net == null ? 'Close at market' : `Close at market · ${fmtUsdSigned(net)}`}
          </button>
        </div>
      </div>
    </div>
  )
}

const sign = (v: number): 'up' | 'down' => (v >= 0 ? 'up' : 'down')

function Row({ k, v, tone, big }: { k: string; v: string; tone?: 'up' | 'down'; big?: boolean }) {
  return (
    <div className={clsx('flex justify-between gap-3', big && 'text-sm font-semibold')}>
      <span className="text-[#848e9c]">{k}</span>
      <span className={tone === 'up' ? 'text-[#0ecb81]' : tone === 'down' ? 'text-[#f6465d]' : ''}>{v}</span>
    </div>
  )
}
