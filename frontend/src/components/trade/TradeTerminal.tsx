import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, Maximize2, Minimize2, MousePointer2, Search, Trash2, Undo2 } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../utils/api'
import { useBinanceStream } from '../../hooks/useBinanceStream'
import { C, DEFAULT_INDICATORS, TradeChart, type Drawing, type Indicators, type Level, type NewDrawing, type Tool } from './TradeChart'
import { OrderBook, type Book, type Tape } from './OrderBook'
import { OrderForm } from './OrderForm'
import { AccountPanel } from './AccountPanel'
import type { Candle, SymbolInfo, Ticker, TradeAccount, Venue } from '../../types/trade'

const INTERVALS = ['1m', '5m', '15m', '1h', '4h', '1d']
const IND_LABEL: Record<keyof Indicators, string> = {
  ma7: 'MA 7', ma25: 'MA 25', ma99: 'MA 99', ema20: 'EMA 20', ema50: 'EMA 50', bb: 'BB 20', volume: 'Vol', rsi: 'RSI 14',
}
const IND_COLOR: Record<keyof Indicators, string> = {
  ma7: C.ma7, ma25: C.ma25, ma99: C.ma99, ema20: C.ema20, ema50: C.ema50, bb: C.bb, volume: '#848e9c', rsi: C.rsi,
}

/** Binance-style drawing-tool icons: a diagonal segment with end points, and horizontal lines with handles. */
const TrendIcon = () => (
  <svg viewBox="0 0 20 20" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.6">
    <line x1="5" y1="15" x2="15" y2="5" /><circle cx="4.5" cy="15.5" r="1.8" /><circle cx="15.5" cy="4.5" r="1.8" />
  </svg>
)
const HLineIcon = () => (
  <svg viewBox="0 0 20 20" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.6">
    <line x1="6" y1="5" x2="18" y2="5" /><line x1="6" y1="10" x2="18" y2="10" /><line x1="6" y1="15" x2="18" y2="15" />
    <circle cx="3.5" cy="10" r="1.8" />
  </svg>
)

function stored<T>(key: string, fallback: T): T {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) as T : fallback } catch { return fallback }
}
function store(key: string, v: unknown) { try { localStorage.setItem(key, JSON.stringify(v)) } catch { /* private mode */ } }

const decimals = (tick: number) => Math.max(0, Math.round(-Math.log10(tick)))

/** Binance-style futures terminal: chart, book, order form, positions — paper or live (main wallet). */
export function TradeTerminal() {
  const [venue, setVenue] = useState<Venue | null>(null)
  const [symbols, setSymbols] = useState<SymbolInfo[]>([])
  const [symbol, setSymbol] = useState<string>(() => stored('trade.symbol', 'BTCUSDT'))
  const [interval, setIntervalState] = useState<string>(() => stored('trade.interval', '15m'))
  // Saved choices from before MA 7/25/99 existed get the new defaults filled in.
  const [ind, setInd] = useState<Indicators>(() => ({ ...DEFAULT_INDICATORS, ...stored<Partial<Indicators>>('trade.ind', {}) }))
  const [tool, setTool] = useState<Tool>('cursor')
  // Drawings are kept per symbol and timeframe (a trend line's points are candle times).
  const [allDrawings, setAllDrawings] = useState<Record<string, Drawing[]>>(() => stored('trade.drawings', {}))
  const [candles, setCandles] = useState<Candle[]>([])
  const [live, setLive] = useState<Candle | null>(null)
  const [ticker, setTicker] = useState<Ticker | null>(null)
  const [mark, setMark] = useState<{ p: number; r: number; T: number } | null>(null)
  const [book, setBook] = useState<Book | null>(null)
  const [tape, setTape] = useState<Tape[]>([])
  const [account, setAccount] = useState<TradeAccount | null>(null)
  const [picked, setPicked] = useState<number | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const lastPrice = useRef<number | null>(null)
  // Real browser fullscreen for the terminal (Esc or the button to leave).
  const root = useRef<HTMLDivElement>(null)
  const [full, setFull] = useState(false)
  useEffect(() => {
    const onChange = () => setFull(document.fullscreenElement === root.current)
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement
      if (!typing && (e.key === 'f' || e.key === 'F') && !e.metaKey && !e.ctrlKey) toggleFull()
      if (e.key === 'Escape') setTool('cursor')
    }
    document.addEventListener('fullscreenchange', onChange)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('fullscreenchange', onChange); document.removeEventListener('keydown', onKey) }
  }, [])
  const toggleFull = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {})
    else root.current?.requestFullscreen().catch(() => {})
  }

  useEffect(() => store('trade.symbol', symbol), [symbol])
  useEffect(() => store('trade.interval', interval), [interval])
  useEffect(() => store('trade.ind', ind), [ind])
  useEffect(() => store('trade.drawings', allDrawings), [allDrawings])
  const drawKey = `${symbol}|${interval}`
  const drawings = allDrawings[drawKey] ?? []
  const addDrawing = (d: NewDrawing) => {
    setAllDrawings(all => ({ ...all, [drawKey]: [...(all[drawKey] ?? []), { ...d, id: `${Date.now()}` } as Drawing] }))
    setTool('cursor')                                   // like Binance: one line per pick of the tool
  }
  const undoDrawing = () => setAllDrawings(all => ({ ...all, [drawKey]: (all[drawKey] ?? []).slice(0, -1) }))
  const clearDrawings = () => setAllDrawings(all => { const { [drawKey]: _, ...rest } = all; return rest })

  const loadVenue = useCallback(async () => {
    try { setVenue(await api.trade.venue()) } catch (e) { setError(e instanceof Error ? e.message : 'Backend unreachable') }
  }, [])
  const loadAccount = useCallback(async () => {
    try { setAccount(await api.trade.account()) } catch { /* keep the last view */ }
  }, [])
  useEffect(() => { loadVenue() }, [loadVenue])
  useEffect(() => {
    loadAccount()
    const id = setInterval(loadAccount, 5000)
    return () => clearInterval(id)
  }, [loadAccount])

  // Symbols and data belong to the venue (mainnet data on paper, the Binance env when live).
  useEffect(() => {
    if (!venue) return
    api.trade.symbols().then(setSymbols).catch(() => setSymbols([]))
  }, [venue?.mode, venue?.env])
  const info = symbols.find(s => s.symbol === symbol)
  const dp = decimals(info?.tick ?? 0.01)

  useEffect(() => {
    if (!venue) return
    let cancelled = false
    setCandles([]); setLive(null); setBook(null); setTape([]); setMark(null)
    lastPrice.current = null          // else the old coin's last trade shows (and prices orders) until a new one ticks
    api.trade.klines(symbol, interval, interval === '1d' ? 365 : 90)   // 90 daily bars can't draw MA 99.then(c => { if (!cancelled) { setCandles(c); setError(null) } })
      .catch(e => !cancelled && setError(e instanceof Error ? e.message : 'No candles'))
    return () => { cancelled = true }
  }, [symbol, interval, venue?.mode, venue?.env])

  useEffect(() => {
    if (!venue) return
    const load = () => api.trade.ticker(symbol).then(setTicker).catch(() => {})
    load()
    const id = setInterval(load, 10_000)
    return () => clearInterval(id)
  }, [symbol, venue?.mode, venue?.env])

  // Live data straight from Binance.
  const lower = symbol.toLowerCase()
  useBinanceStream(venue?.ws_book, [`${lower}@depth20@500ms`], (stream, d) => {
    if (!stream.startsWith(`${lower}@`)) return
    setBook({ bids: (d.b ?? []).map((x: string[]) => [+x[0], +x[1]]), asks: (d.a ?? []).map((x: string[]) => [+x[0], +x[1]]) })
  })
  useBinanceStream(venue?.ws_market, [`${lower}@aggTrade`, `${lower}@kline_${interval}`, `${lower}@markPrice@1s`], (stream, d) => {
    // The old socket can deliver a few frames after a symbol or timeframe switch — drop them.
    if (!stream.startsWith(`${lower}@`) || (stream.includes('@kline_') && d.k?.i !== interval)) return
    if (stream.endsWith('@aggTrade')) {
      const t = { price: +d.p, qty: +d.q, time: d.T, buy: !d.m }
      lastPrice.current = t.price
      setTape(prev => [t, ...prev].slice(0, 60))
    } else if (stream.includes('@kline_')) {
      const k = d.k
      setLive({ time: Math.floor(k.t / 1000), open: +k.o, high: +k.h, low: +k.l, close: +k.c, volume: +k.v })
    } else if (stream.includes('@markPrice')) {
      setMark({ p: +d.p, r: +d.r, T: d.T })
    }
  })

  const last = live?.close ?? lastPrice.current ?? ticker?.last ?? null
  const marks = useMemo(() => (mark ? { [symbol]: mark.p } : {}), [mark, symbol])

  const levels = useMemo<Level[]>(() => {
    const out: Level[] = []
    for (const p of account?.positions ?? []) {
      if (p.symbol !== symbol) continue
      const tag = p.mode === 'live' ? 'LIVE ' : ''
      out.push({ price: p.entry_price, color: '#848e9c', title: `${tag}${p.direction} entry` })
      out.push({ price: p.stop_loss, color: C.down, title: `${tag}stop` })
      if (p.tp_set !== false) out.push({ price: p.take_profit_2, color: C.up, title: `${tag}target` })
      out.push({ price: p.liquidation_price, color: '#f0b90b', title: 'liq.', dashed: true })
    }
    // Positions other systems hold on the Binance account (the main wallet's are drawn above).
    for (const x of account?.exchange ?? []) {
      if (x.symbol !== symbol || x.owner === 'main') continue
      const who = { auto: 'auto', unmanaged: 'UNMANAGED' }[x.owner as 'auto']
      out.push({ price: x.entry, color: '#848e9c', title: `${who} ${x.side}` })
      if (x.stop) out.push({ price: x.stop, color: C.down, title: `${who} stop` })
      if (x.liquidation) out.push({ price: x.liquidation, color: '#f0b90b', title: 'liq.', dashed: true })
    }
    for (const o of account?.orders ?? []) {
      if (o.symbol === symbol) out.push({ price: o.limit_price, color: '#f0b90b', title: `${o.mode === 'live' ? 'LIVE ' : ''}limit ${o.direction}`, dashed: true })
    }
    return out
  }, [account, symbol])

  const flash = (msg?: string) => {
    loadAccount()
    if (msg) { setToast(msg); setTimeout(() => setToast(null), 4000) }
  }
  const change = ticker?.change_pct ?? 0

  return (
    <div ref={root} className={clsx('space-y-2 text-[#eaecef]', full && 'h-screen overflow-y-auto p-2')} style={{ background: C.bg }}>
      {/* Symbol bar */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 bg-[#161a1e] rounded-lg px-3 py-2">
        <SymbolPicker symbols={symbols} value={symbol} onChange={s => { setSymbol(s); setPicked(null) }} />
        <div>
          <div className={clsx('text-lg font-bold tabular-nums', change >= 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]')}>
            {last != null ? last.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp }) : '—'}
          </div>
          <div className="text-[10px] text-[#848e9c]">Mark {mark ? mark.p.toFixed(dp) : ticker?.mark.toFixed(dp) ?? '—'}</div>
        </div>
        <Stat k="24h change" v={ticker ? `${change >= 0 ? '+' : ''}${change.toFixed(2)}%` : '—'} tone={change >= 0 ? 'up' : 'down'} />
        <Stat k="24h high" v={ticker ? ticker.high.toFixed(dp) : '—'} />
        <Stat k="24h low" v={ticker ? ticker.low.toFixed(dp) : '—'} />
        <Stat k="24h volume" v={ticker ? `$${(ticker.volume_usd / 1e6).toFixed(1)}M` : '—'} />
        <Stat k="Funding / countdown" v={mark || ticker ? `${(((mark?.r ?? ticker?.funding_rate) ?? 0) * 100).toFixed(4)}% · ${countdown(mark?.T ?? ticker?.next_funding)}` : '—'} />
        <span className={clsx('ml-auto text-[10px] font-black tracking-wider px-2 py-1 rounded',
          venue?.mode === 'live' ? 'bg-rose-600 text-white animate-pulse' : 'bg-[#2b3139] text-[#848e9c]')}>
          {venue?.mode === 'live' ? `LIVE · BINANCE ${venue.env.toUpperCase()}` : 'PAPER · MAINNET PRICES'}
        </span>
        <button onClick={toggleFull} title={full ? 'Exit full screen (Esc)' : 'Full screen (F)'}
          className="p-1.5 rounded hover:bg-[#2b3139] text-[#848e9c] hover:text-white">
          {full ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
        </button>
      </div>

      {error && <div className="text-xs px-3 py-2 rounded bg-[#f6465d]/10 text-[#f6465d]">{error}</div>}
      {toast && <div className="text-xs px-3 py-2 rounded bg-[#0ecb81]/10 text-[#0ecb81]">{toast}</div>}

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_280px_330px] gap-2">
        {/* Chart */}
        <div className={clsx('bg-[#161a1e] rounded-lg flex flex-col',
          full ? 'h-[calc(100vh-280px)] min-h-[420px]' : 'h-[62vh] min-h-[400px] xl:h-[68vh] xl:min-h-[560px]')}>
          <div className="flex flex-wrap items-center gap-1 px-2 sm:px-3 py-1.5 border-b border-[#2b3139] text-xs">
            {INTERVALS.map(i => (
              <button key={i} onClick={() => setIntervalState(i)}
                className={clsx('px-2 py-0.5 rounded', interval === i ? 'text-[#f0b90b] bg-[#2b3139]' : 'text-[#848e9c] hover:text-white')}>{i}</button>
            ))}
            <span className="mx-2 h-4 w-px bg-[#2b3139]" />
            {(Object.keys(IND_LABEL) as (keyof Indicators)[]).map(k => (
              <button key={k} onClick={() => setInd({ ...ind, [k]: !ind[k] })}
                className={clsx('px-2 py-0.5 rounded', ind[k] ? 'text-[#eaecef]' : 'text-[#5e6673]')}>
                <span className="inline-block w-2 h-2 rounded-full mr-1 align-middle"
                  style={{ background: IND_COLOR[k], opacity: ind[k] ? 1 : 0.3 }} />
                {IND_LABEL[k]}
              </button>
            ))}
          </div>
          <div className="flex-1 min-h-0 flex">
            {/* Drawing tools, like Binance's left toolbar */}
            <div className="flex flex-col items-center gap-1 py-2 px-1 border-r border-[#2b3139] shrink-0">
              {([['cursor', <MousePointer2 key="c" className="w-4 h-4" />, 'Cursor (Esc)'],
                 ['trend', <TrendIcon key="t" />, 'Trend line — click two points'],
                 ['hline', <HLineIcon key="h" />, 'Horizontal line — click a price']] as const).map(([t, icon, title]) => (
                <button key={t} onClick={() => setTool(tool === t ? 'cursor' : t)} title={title} aria-label={title}
                  className={clsx('p-1.5 rounded', tool === t ? 'bg-[#2b3139] text-[#f0b90b]' : 'text-[#848e9c] hover:text-white hover:bg-[#2b3139]')}>
                  {icon}
                </button>
              ))}
              <span className="w-5 h-px bg-[#2b3139] my-1" />
              <button onClick={undoDrawing} disabled={!drawings.length} title="Undo last drawing" aria-label="Undo last drawing"
                className="p-1.5 rounded text-[#848e9c] hover:text-white hover:bg-[#2b3139] disabled:opacity-30"><Undo2 className="w-4 h-4" /></button>
              <button onClick={clearDrawings} disabled={!drawings.length} title="Remove all drawings on this chart" aria-label="Remove all drawings"
                className="p-1.5 rounded text-[#848e9c] hover:text-[#f6465d] hover:bg-[#2b3139] disabled:opacity-30"><Trash2 className="w-4 h-4" /></button>
            </div>
            <div className="flex-1 min-w-0">
              <TradeChart candles={candles} live={live} indicators={ind} levels={levels} precision={dp}
                tool={tool} drawings={drawings} onDraw={addDrawing} />
            </div>
          </div>
        </div>

        {/* Phones: the order form comes straight after the chart, the book after it. */}
        <div className={clsx('order-3 xl:order-none', full ? 'h-[calc(100vh-280px)] min-h-[420px]' : 'h-[440px] xl:h-[68vh] xl:min-h-[560px]')}>
          <OrderBook book={book} tape={tape} last={last} precision={dp} onPick={p => setPicked(p)} />
        </div>

        <div className="order-2 xl:order-none">
          <OrderForm symbol={symbol} info={info} venue={venue} account={account} mark={mark?.p ?? last}
            picked={picked} onPlaced={flash} onModeChange={() => { loadVenue(); loadAccount() }} />
        </div>
      </div>

      <AccountPanel account={account} marks={marks} onChanged={flash} onPickSymbol={s => setSymbol(s)} />
    </div>
  )
}

function countdown(ms?: number) {
  if (!ms) return '—'
  const s = Math.max(0, Math.floor((ms - Date.now()) / 1000))
  return `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

function Stat({ k, v, tone }: { k: string; v: string; tone?: 'up' | 'down' }) {
  return (
    <div className="text-xs">
      <div className="text-[10px] text-[#848e9c]">{k}</div>
      <div className={clsx('tabular-nums', tone === 'up' ? 'text-[#0ecb81]' : tone === 'down' ? 'text-[#f6465d]' : 'text-[#eaecef]')}>{v}</div>
    </div>
  )
}

function SymbolPicker({ symbols, value, onChange }: { symbols: SymbolInfo[]; value: string; onChange: (s: string) => void }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])
  const list = symbols.filter(s => s.symbol.includes(q.toUpperCase())).slice(0, 80)
  return (
    <div ref={box} className="relative">
      <button onClick={() => setOpen(o => !o)} className="flex items-center gap-1.5 text-base font-bold hover:text-[#f0b90b]">
        {value} <span className="text-[10px] font-semibold text-[#848e9c] bg-[#2b3139] px-1 rounded">Perp</span>
        <ChevronDown className="w-4 h-4 text-[#848e9c]" />
      </button>
      {open && (
        <div className="absolute z-20 mt-2 w-72 bg-[#1e2329] border border-[#2b3139] rounded-lg shadow-xl">
          <label className="flex items-center gap-2 px-3 py-2 border-b border-[#2b3139]">
            <Search className="w-3.5 h-3.5 text-[#848e9c]" />
            <input autoFocus value={q} onChange={e => setQ(e.target.value)} placeholder="Search"
              className="flex-1 bg-transparent text-sm outline-none placeholder:text-[#5e6673]" />
          </label>
          <div className="max-h-80 overflow-y-auto">
            {list.map(s => (
              <button key={s.symbol} onClick={() => { onChange(s.symbol); setOpen(false); setQ('') }}
                className={clsx('w-full flex justify-between px-3 py-1.5 text-xs hover:bg-[#2b3139]', s.symbol === value && 'text-[#f0b90b]')}>
                <span className="font-semibold">{s.symbol}</span>
                <span className="tabular-nums text-[#848e9c]">${(s.volume / 1e6).toFixed(0)}M
                  <span className={clsx('ml-2', s.change >= 0 ? 'text-[#0ecb81]' : 'text-[#f6465d]')}>{s.change >= 0 ? '+' : ''}{s.change.toFixed(2)}%</span></span>
              </button>
            ))}
            {!list.length && <p className="px-3 py-4 text-xs text-[#848e9c]">No match.</p>}
          </div>
        </div>
      )}
    </div>
  )
}
