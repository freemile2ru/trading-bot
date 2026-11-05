import { useEffect, useMemo, useState } from 'react'
import clsx from 'clsx'
import { api } from '../utils/api'
import type { AuctionChartData } from '../types/auction'
import type { Candle } from '../types/trade'
import { DEFAULT_INDICATORS, TradeChart, type ChartMarker, type ChartSegment, type Indicators, type Level } from './trade/TradeChart'

// The study's colours: PDH/PDL white dashed, ONH/ONL orange dashed, PWH/PWL yellow dashed, ORB green/red, IB cyan, IB extensions dark green/red dashed.
const LEVEL_STYLE: Record<string, { color: string; dashed: boolean }> = {
  PDH: { color: '#e5e7eb', dashed: true }, PDL: { color: '#e5e7eb', dashed: true },
  ONH: { color: '#fb923c', dashed: true }, ONL: { color: '#fb923c', dashed: true },
  WKH: { color: '#facc15', dashed: true }, WKL: { color: '#facc15', dashed: true },
  ORBH: { color: '#22c55e', dashed: false }, ORBL: { color: '#ef4444', dashed: false },
  IBH: { color: '#22d3ee', dashed: false }, IBL: { color: '#22d3ee', dashed: false },
  IBH1: { color: '#166534', dashed: true }, IBL1: { color: '#7f1d1d', dashed: true },
  IBH2: { color: '#166534', dashed: true }, IBL2: { color: '#7f1d1d', dashed: true },
}
const IND_LABEL: Partial<Record<keyof Indicators, string>> = { ema20: 'EMA 20', ema50: 'EMA 50', ma7: 'MA 7', ma25: 'MA 25', ma99: 'MA 99', bb: 'Bollinger', volume: 'Volume', rsi: 'RSI' }
const SPANS: [number, string][] = [[300, '1 day'], [600, '2 days'], [1200, '4 days'], [2000, '7 days']]
const snap = (t: number, step: number) => Math.floor(t / step) * step

/** The mentor's chart, built on the Trade tab's chart: one big interactive candlestick chart with the levels as labelled
 * price lines (they never squash the candles), the study's signals as markers, and this book's trades as legs. */
export function AuctionChart({ symbols, symbol, onSymbol }: { symbols: string[]; symbol: string; onSymbol: (s: string) => void }) {
  const [span, setSpan] = useState(600)
  const [d, setD] = useState<AuctionChartData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [levelsOn, setLevelsOn] = useState<Record<string, boolean>>(() => Object.fromEntries(Object.keys(LEVEL_STYLE).map(k => [k, !k.endsWith('2')])))
  const [ind, setInd] = useState<Indicators>({ ...DEFAULT_INDICATORS, ma7: false, ma25: false, ma99: false, ema20: true, ema50: true, rsi: false })
  const [showStudy, setShowStudy] = useState(true)
  const [showTrades, setShowTrades] = useState(true)

  useEffect(() => {
    let live = true
    setD(null)
    const load = () => api.auction.chart(symbol, span).then(x => { if (live) { setD(x); setErr(null) } }).catch(e => { if (live) setErr(String(e?.message ?? e)) })
    load()
    const id = setInterval(load, 60_000)
    return () => { live = false; clearInterval(id) }
  }, [symbol, span])

  const candles = useMemo<Candle[]>(() => (d?.bars ?? []).map(b => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c, volume: b.v })), [d])
  const last = candles[candles.length - 1]?.close ?? 1
  const precision = last >= 1000 ? 1 : last >= 100 ? 2 : last >= 1 ? 3 : 5
  const levels = useMemo<Level[]>(() => (d?.current_levels ?? []).filter(l => levelsOn[l.name]).map(l => ({ price: l.price, color: LEVEL_STYLE[l.name]?.color ?? '#9ca3af', title: l.name, dashed: LEVEL_STYLE[l.name]?.dashed })), [d, levelsOn])
  const { markers, segments } = useMemo(() => {
    const ms: ChartMarker[] = []
    const sg: ChartSegment[] = []
    if (!d) return { markers: ms, segments: sg }
    const step = d.bar_s
    const lastT = d.bars[d.bars.length - 1]?.t ?? 0
    if (showStudy) {
      for (const e of d.study_events) {
        const t = snap(e.t, step)
        if (e.kind === 'accept') ms.push({ time: t, position: e.direction === 'long' ? 'belowBar' : 'aboveBar', color: e.direction === 'long' ? '#22d3ee' : '#e5e7eb', shape: e.direction === 'long' ? 'arrowUp' : 'arrowDown', text: `accept ${e.level}` })
        else if (e.kind === 'confirm') ms.push({ time: t, position: e.direction === 'long' ? 'belowBar' : 'aboveBar', color: '#facc15', shape: 'circle', text: `confirm ${e.grade}/8` })
        else if (e.kind === 'entry') ms.push({ time: t, position: e.direction === 'long' ? 'belowBar' : 'aboveBar', color: e.taken ? '#fb923c' : '#6b7280', shape: 'square', text: e.taken ? `entry → ${e.target_name}` : 'entry (gated)' })
        else if (e.kind === 'exit') ms.push({ time: t, position: 'inBar', color: '#e879f9', shape: 'circle', text: `${(e.reason ?? '').replace(/_/g, ' ')}${e.pnl_pct != null ? ` ${e.pnl_pct > 0 ? '+' : ''}${e.pnl_pct.toFixed(2)}%` : ''}` })
      }
    }
    if (showTrades) {
      for (const t of d.trades) {
        if (!t.entry_ts) continue
        const a = snap(t.entry_ts, step), b = t.exit_ts ? snap(t.exit_ts, step) : lastT
        const pnl = t.pnl_usd ?? 0
        ms.push({ time: a, position: t.direction === 'long' ? 'belowBar' : 'aboveBar', color: '#f59e0b', shape: t.direction === 'long' ? 'arrowUp' : 'arrowDown', text: `BOOK ${t.direction.toUpperCase()} ${t.level ?? ''} @ ${t.entry.toFixed(precision)}` })
        if (t.exit_ts && t.exit != null) ms.push({ time: b, position: t.direction === 'long' ? 'aboveBar' : 'belowBar', color: pnl >= 0 ? '#34d399' : '#f87171', shape: 'square',
          text: `BOOK OUT ${(t.reason ?? '').replace(/_/g, ' ')} @ ${t.exit.toFixed(precision)} ${pnl >= 0 ? '+' : '−'}$${Math.abs(pnl).toFixed(0)}` })
        if (b > a) {
          sg.push({ a: { time: a, price: t.entry }, b: { time: b, price: t.exit ?? t.entry }, color: (t.pnl_usd ?? 0) >= 0 ? '#34d399' : '#f87171', width: 2 })
          if (t.stop != null) sg.push({ a: { time: a, price: t.stop }, b: { time: b, price: t.stop }, color: '#f87171', dashed: true })
          if (t.target != null) sg.push({ a: { time: a, price: t.target }, b: { time: b, price: t.target }, color: '#34d399', dashed: true })
        }
      }
    }
    return { markers: ms, segments: sg }
  }, [d, showStudy, showTrades, precision])
  const bookTrades = useMemo(() => (d?.trades ?? []).filter(t => t.entry_ts).sort((x, y) => (y.entry_ts ?? 0) - (x.entry_ts ?? 0)), [d])
  const firstT = d?.bars[0]?.t ?? 0

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-gray-500">Instrument</span>
        {symbols.map(sy => <button key={sy} onClick={() => onSymbol(sy)} className={clsx('rounded border px-2.5 py-1 font-semibold', symbol === sy ? 'border-amber-700 bg-amber-950/40 text-amber-200' : 'border-gray-800 text-gray-400 hover:text-gray-200')}>{sy.replace('USDT', '')}</button>)}
        <span className="text-gray-500 ml-3">Span</span>
        {SPANS.map(([n, l]) => <button key={n} onClick={() => setSpan(n)} className={clsx('rounded border px-2 py-1', span === n ? 'border-gray-600 text-gray-100' : 'border-gray-800 text-gray-400 hover:text-gray-200')}>{l}</button>)}
        <span className="ml-auto text-[11px] text-gray-500">5-minute bars · {d ? `${d.bars.length} bars` : 'loading'}{err ? ` · ${err}` : ''}</span>
      </div>
      <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
        <span className="text-gray-600">Levels</span>
        {Object.keys(LEVEL_STYLE).map(k => {
          const has = d?.current_levels.some(l => l.name === k)
          return <button key={k} onClick={() => setLevelsOn(s => ({ ...s, [k]: !s[k] }))} disabled={!has} title={has ? (d?.current_levels.find(l => l.name === k)?.label ?? k) : 'not available right now'}
            className={clsx('rounded border px-1.5 py-0.5', levelsOn[k] && has ? 'border-gray-600' : 'border-gray-800 text-gray-600', !has && 'opacity-40')} style={levelsOn[k] && has ? { color: LEVEL_STYLE[k].color } : undefined}>{k}</button>
        })}
        <span className="text-gray-600 ml-2">Indicators</span>
        {(Object.keys(IND_LABEL) as (keyof Indicators)[]).map(k => <button key={k} onClick={() => setInd(s => ({ ...s, [k]: !s[k] }))} className={clsx('rounded border px-1.5 py-0.5', ind[k] ? 'border-gray-600 text-gray-200' : 'border-gray-800 text-gray-600')}>{IND_LABEL[k]}</button>)}
        <button onClick={() => setShowStudy(v => !v)} className={clsx('rounded border px-1.5 py-0.5 ml-2', showStudy ? 'border-amber-800 text-amber-200' : 'border-gray-800 text-gray-600')}>study signals</button>
        <button onClick={() => setShowTrades(v => !v)} className={clsx('rounded border px-1.5 py-0.5', showTrades ? 'border-amber-800 text-amber-200' : 'border-gray-800 text-gray-600')}>book trades</button>
      </div>
      <div className="card p-0 overflow-hidden h-[62vh] min-h-[420px] xl:h-[70vh] xl:min-h-[560px]">
        {candles.length ? (
          <TradeChart candles={candles} live={null} indicators={ind} levels={levels} precision={precision} tool="cursor" drawings={[]} onDraw={() => {}} markers={markers} segments={segments} />
        ) : <div className="h-full flex items-center justify-center text-sm text-gray-500">{err ?? 'Loading the chart…'}</div>}
      </div>
      {showTrades && bookTrades.length > 0 && (
        <div className="card py-2 text-[11px]">
          <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">This book's trades on {symbol.replace('USDT', '')} — amber arrow in, square out, the leg between them</div>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {bookTrades.map(t => {
              const pnl = t.pnl_usd ?? 0
              const inWindow = (t.entry_ts ?? 0) >= firstT
              const mins = t.entry_ts ? Math.round(((t.exit_ts ?? Date.now() / 1000) - t.entry_ts) / 60) : null
              return (
                <span key={t.id} className={clsx('whitespace-nowrap', !inWindow && 'text-gray-600')} title={inWindow ? 'drawn on the chart' : 'before the chart window — widen the span to see it'}>
                  <span className={t.direction === 'long' ? 'text-emerald-300' : 'text-rose-300'}>{t.direction}</span> {t.level ?? '—'} → {t.target_name ?? '—'} · {t.entry_ts ? new Date(t.entry_ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}
                  {mins != null ? ` · ${mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h ${mins % 60}m`}` : ''} · {t.status === 'open' ? <span className="text-amber-300">open</span> : (t.reason ?? '').replace(/_/g, ' ')} · <span className={pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{pnl >= 0 ? '+' : '−'}${Math.abs(pnl).toFixed(0)}</span>{!inWindow && ' · off-screen'}
                </span>
              )
            })}
          </div>
        </div>
      )}
      <p className="text-[10px] text-gray-600">Levels are drawn as price lines with their value on the axis, so a far-away level never squashes the candles — scroll and zoom as on the Trade tab. Markers: acceptance (arrow), confirmation (yellow dot with the grade), the retest entry (orange square; grey when the book's gates would have skipped it), the exit (magenta). Amber arrows and the green/red legs are this book's real trades, the square marks the exit with its reason and P&L, with the stop (red dotted) and target (green dotted) alongside.</p>
    </div>
  )
}
