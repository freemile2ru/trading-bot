import { useEffect, useMemo, useState } from 'react'
import { FileText, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../utils/api'
import { fmtUsd, fmtUsdSigned } from '../../utils/format'
import { SYSTEM_LABEL, type SystemKey } from '../../types/overview'
import { C, type ChartMarker, type ChartSegment, type Level } from './TradeChart'

/** A trade opened from the Trades tab, plotted on the terminal's chart like Binance draws a position: entry, stop,
 *  target and liquidation as price lines that follow every stop move, an entry marker on the candles, and a strip with
 *  the live P&L off the same tick the chart paints. Closed trades draw their entry → exit leg instead. */
export type Focus = { system: SystemKey; id: string }
type Rec = Record<string, any>

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const ts = (v: unknown): number | null => {
  if (typeof v === 'number') return v > 1e12 ? Math.floor(v / 1000) : Math.floor(v)
  if (typeof v === 'string') { const ms = Date.parse(v); return Number.isFinite(ms) ? Math.floor(ms / 1000) : null }
  return null
}

export function readTrade(t: Rec) {
  const direction: 'long' | 'short' = (t.direction ?? t.side) === 'short' ? 'short' : 'long'
  const entry = num(t.entry_price), s = direction === 'long' ? 1 : -1
  // Books without a target stamp one the price cannot reach (±50×, or wrong-sided): drawing it would flatten the chart.
  const real = (tp: number | null) => (tp != null && entry != null && (tp - entry) * s > 0 && Math.abs(tp / entry - 1) <= 0.3 ? tp : null)
  const tp2 = real(num(t.take_profit_2 ?? t.take_profit ?? t.tp))
  return {
    symbol: String(t.symbol ?? ''), direction, status: (t.status ?? (t.closed_at || t.exit_price ? 'closed' : 'open')) as 'open' | 'closed',
    entry, stop: num(t.stop_loss ?? t.sl), originalStop: num(t.original_stop_loss),
    tp1: real(num(t.take_profit_1)), tp2, tpSet: t.tp_set !== false && tp2 != null,
    liq: num(t.liquidation_price ?? t.liq_price), qty: num(t.quantity ?? t.qty), notional: num(t.notional_usd ?? t.notional ?? t.position_value_usd),
    margin: num(t.margin_usd ?? t.margin), leverage: num(t.leverage), fees: num(t.fees_usd ?? t.fees) ?? 0, funding: num(t.funding_usd ?? t.funding) ?? 0,
    realized: num(t.realized_pnl_usd) ?? 0, pnl: num(t.pnl_usd ?? t.net_pnl), locked: num(t.locked_usd),
    // A closed paper trade keeps its fill in current_price (the wallet refreshes the record at the fill); scalps carry exit_price.
    exit: num(t.exit_price ?? t.exit_trigger_price ?? (t.status === 'closed' || t.closed_at ? t.current_price : null)),
    openedTs: ts(t.opened_ts ?? t.entry_ts ?? t.opened_at), closedTs: ts(t.closed_ts ?? t.exit_ts ?? t.closed_at),
    closeReason: (t.close_reason ?? t.exit_reason) as string | undefined, peak: num(t.peak_price), mark: num(t.current_price ?? t.mark_price),
  }
}
export type FocusedTrade = ReturnType<typeof readTrade>

/** Loads the record and keeps it fresh (stop moves, locks, the close) every few seconds while the chart is open. */
export function useFocusedTrade(focus: Focus | null, everyMs = 5000) {
  const [rec, setRec] = useState<Rec | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    if (!focus) { setRec(null); setErr(null); return }
    let stale = false
    const load = () => api.positions.detail(focus.system, focus.id)
      .then(x => { if (!stale) { setRec(x as Rec); setErr(null) } })
      .catch(e => { if (!stale) setErr(e instanceof Error ? e.message : 'Could not load the trade') })
    load()
    const id = setInterval(load, everyMs)
    return () => { stale = true; clearInterval(id) }
  }, [focus?.system, focus?.id, everyMs]) // eslint-disable-line react-hooks/exhaustive-deps
  const trade = useMemo(() => (rec ? readTrade(rec) : null), [rec])
  return { trade, err }
}

/** What the chart paints for the focused trade. The bar `time` is the candle the entry fell in, at the chart's interval. */
export function focusAnnotations(t: FocusedTrade | null, intervalS: number): { levels: Level[]; markers: ChartMarker[]; segments: ChartSegment[] } {
  if (!t || t.entry == null) return { levels: [], markers: [], segments: [] }
  const entry = t.entry
  const s = t.direction === 'long' ? 1 : -1
  const bar = (x: number) => Math.floor(x / intervalS) * intervalS
  const levels: Level[] = []
  const markers: ChartMarker[] = []
  const segments: ChartSegment[] = []
  const who = t.direction === 'long' ? 'Long' : 'Short'
  const twoTargets = t.tp1 != null && t.tp2 != null && Math.abs(t.tp1 - t.tp2) > 1e-9
  if (t.status === 'open') {
    levels.push({ price: entry, color: '#eaecef', title: `${who} entry` })
    if (t.stop != null) levels.push({ price: t.stop, color: C.down, title: t.locked != null && (t.stop - entry) * s > 0 ? `lock +$${t.locked.toFixed(0)}` : 'stop' })
    if (t.originalStop != null && t.stop != null && Math.abs(t.originalStop - t.stop) > 1e-9) levels.push({ price: t.originalStop, color: C.down, title: 'stop at entry', dashed: true })
    if (t.tpSet && twoTargets && t.tp1 != null) levels.push({ price: t.tp1, color: C.up, title: 'TP1', dashed: true })
    if (t.tpSet && t.tp2 != null) levels.push({ price: t.tp2, color: C.up, title: twoTargets ? 'TP2' : 'target' })
    if (t.liq != null) levels.push({ price: t.liq, color: '#f0b90b', title: 'liq.', dashed: true })
    if (t.peak != null && Math.abs(t.peak - entry) > 1e-9) levels.push({ price: t.peak, color: '#848e9c', title: 'best', dashed: true })
  } else if (t.exit != null) {
    const exit = t.exit
    const won = (exit - entry) * s > 0
    levels.push({ price: entry, color: '#848e9c', title: `${who} entry`, dashed: true })
    levels.push({ price: exit, color: won ? C.up : C.down, title: `exit · ${(t.closeReason ?? '').replace(/_/g, ' ')}` })
    if (t.openedTs && t.closedTs && t.closedTs > t.openedTs) {
      const a = bar(t.openedTs), b = bar(t.closedTs)
      segments.push({ a: { time: a, price: entry }, b: { time: b, price: exit }, color: won ? C.up : C.down, width: 2 })
      if (t.stop != null) segments.push({ a: { time: a, price: t.stop }, b: { time: b, price: t.stop }, color: C.down, dashed: true })
      if (t.tpSet && t.tp2 != null) segments.push({ a: { time: a, price: t.tp2 }, b: { time: b, price: t.tp2 }, color: C.up, dashed: true })
      markers.push({ time: b, position: won ? 'aboveBar' : 'belowBar', color: won ? C.up : C.down, shape: 'circle', text: 'exit' })
    }
  }
  if (t.openedTs) markers.push({ time: bar(t.openedTs), position: s > 0 ? 'belowBar' : 'aboveBar', color: s > 0 ? C.up : C.down, shape: s > 0 ? 'arrowUp' : 'arrowDown', text: who })
  return { levels, markers, segments }
}

/** The strip above the chart: the position's numbers off the live tick, like Binance's position row. */
export function FocusStrip({ focus, trade, last, precision, err, note, onClear, onRecord }: {
  focus: Focus; trade: FocusedTrade | null; last: number | null; precision: number; err: string | null; note?: string
  onClear: () => void; onRecord: () => void
}) {
  const fp = (v: number | null | undefined) => (v == null ? '—' : v.toLocaleString(undefined, { minimumFractionDigits: precision, maximumFractionDigits: precision }))
  const t = trade
  const s = t?.direction === 'short' ? -1 : 1
  const mark = t?.status === 'open' ? (last ?? t.mark) : t?.exit ?? null
  const pnl = t == null ? null
    : t.status !== 'open' ? t.pnl
    : t.entry != null && mark != null && t.qty != null ? (mark - t.entry) * s * t.qty + t.realized - t.fees - t.funding : null
  const roe = pnl != null && t?.margin ? pnl / t.margin * 100 : null
  const dist = (lvl: number | null | undefined) => (lvl == null || mark == null ? null : (lvl - mark) / mark * 100)
  const toStop = dist(t?.stop), toTarget = dist(t?.tpSet ? t?.tp2 : null)
  const pct = (x: number) => `${x >= 0 ? '+' : ''}${x.toFixed(2)}%`
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1 bg-[#1e2329] border border-[#2b3139] rounded-lg px-3 py-2 text-xs">
      <span className="text-[9px] font-black px-1 py-0.5 rounded bg-[#2b3139] text-[#b7bdc6]">{SYSTEM_LABEL[focus.system] ?? focus.system}</span>
      {t ? (
        <>
          <span className={clsx('font-bold', s > 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]')}>{t.symbol} {t.direction === 'long' ? 'Long' : 'Short'}{t.leverage ? ` ${t.leverage}x` : ''}</span>
          <Cell k="Size" v={`${fmtUsd(t.notional, 0)}${t.qty != null ? ` · ${t.qty.toLocaleString(undefined, { maximumFractionDigits: 4 })}` : ''}`} />
          <Cell k="Entry" v={fp(t.entry)} />
          <Cell k={t.status === 'open' ? 'Mark' : 'Exit'} v={fp(mark)} />
          <Cell k={t.status === 'open' ? 'PnL (ROE)' : 'Result'} v={pnl == null ? '—' : `${fmtUsdSigned(pnl)}${roe != null ? ` (${pct(roe)})` : ''}`}
            tone={pnl == null ? undefined : pnl >= 0 ? 'up' : 'down'} />
          <Cell k="Stop" v={`${fp(t.stop)}${toStop != null ? ` (${pct(toStop)})` : ''}`} tone="down" />
          <Cell k="Target" v={t.tpSet ? `${fp(t.tp2)}${toTarget != null ? ` (${pct(toTarget)})` : ''}` : 'none'} tone="up" />
          {t.liq != null && <Cell k="Liq." v={fp(t.liq)} />}
          {t.locked != null && t.status === 'open' && <Cell k="Locked" v={fmtUsdSigned(t.locked)} tone="up" />}
          {t.status === 'closed' && <Cell k="Ended" v={(t.closeReason ?? '—').replace(/_/g, ' ')} />}
        </>
      ) : <span className="text-[#848e9c]">{err ?? 'Loading the trade…'}</span>}
      {note && <span className="text-[10px] text-[#f0b90b]">{note}</span>}
      <span className="ml-auto flex items-center gap-1">
        <button onClick={onRecord} title="The trade's full record" aria-label="Open the trade's full record" className="p-1 rounded text-[#848e9c] hover:text-white hover:bg-[#2b3139]"><FileText className="w-3.5 h-3.5" /></button>
        <button onClick={onClear} title="Stop following this trade" aria-label="Stop following this trade" className="p-1 rounded text-[#848e9c] hover:text-white hover:bg-[#2b3139]"><X className="w-3.5 h-3.5" /></button>
      </span>
    </div>
  )
}

function Cell({ k, v, tone }: { k: string; v: string; tone?: 'up' | 'down' }) {
  return (
    <span className="tabular-nums"><span className="text-[10px] text-[#848e9c] mr-1">{k}</span>
      <span className={tone === 'up' ? 'text-[#0ecb81]' : tone === 'down' ? 'text-[#f6465d]' : 'text-[#eaecef]'}>{v}</span></span>
  )
}
