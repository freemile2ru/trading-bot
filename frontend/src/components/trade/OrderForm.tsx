import { useEffect, useMemo, useState } from 'react'
import clsx from 'clsx'
import { api } from '../../utils/api'
import { fmtUsd } from '../../utils/format'
import type { SymbolInfo, TradeAccount, Venue } from '../../types/trade'

const MMR = (symbol: string) => (symbol === 'BTCUSDT' || symbol === 'ETHUSDT' ? 0.004 : 0.01)
const LEVERAGES = [1, 2, 3, 5, 10, 20]
const SIZE_PCTS = [10, 25, 50, 75, 100]
const STOP_PCTS = [0.5, 1, 2, 3, 5]

const decimals = (tick: number) => Math.max(0, Math.round(-Math.log10(tick)))

export function OrderForm({ symbol, info, venue, account, mark, picked, onPlaced, onModeChange }: {
  symbol: string; info: SymbolInfo | undefined; venue: Venue | null; account: TradeAccount | null; mark: number | null
  picked: number | null; onPlaced: (msg: string) => void; onModeChange: () => void
}) {
  const [side, setSide] = useState<'long' | 'short'>('long')
  const [type, setType] = useState<'market' | 'limit'>('market')
  const [price, setPrice] = useState('')
  const [size, setSize] = useState('')
  const [lev, setLev] = useState(5)
  const [stop, setStop] = useState('')
  const [tp, setTp] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [confirmLive, setConfirmLive] = useState<string | null>(null)

  const tick = info?.tick ?? 0.01
  const dp = decimals(tick)
  const maxLev = venue?.max_leverage ?? 20
  const live = venue?.mode === 'live'

  // A price clicked in the order book → limit price.
  useEffect(() => {
    if (picked != null) { setType('limit'); setPrice(picked.toFixed(dp)) }
  }, [picked, dp])
  // New symbol: clear the levels (they belong to the old price).
  useEffect(() => { setStop(''); setTp(''); setPrice(''); setErr(null) }, [symbol])

  const entry = type === 'limit' ? Number(price) || 0 : mark ?? 0
  const s = side === 'long' ? 1 : -1
  const notional = Number(size) || 0
  const stopN = Number(stop) || 0
  const tpN = Number(tp) || 0
  const available = account?.balance.available ?? 0
  const equity = account?.balance.equity ?? 0

  const calc = useMemo(() => {
    if (!entry || !notional) return null
    const qty = notional / entry
    const margin = notional / lev
    const liq = entry * (1 - s / lev + s * MMR(symbol))
    const feeRate = type === 'market' ? (venue?.taker_fee ?? 0.0005) : (venue?.maker_fee ?? 0.0002)
    const fees = notional * feeRate + notional * (venue?.taker_fee ?? 0.0005)
    const stopDist = stopN ? (entry - stopN) * s : 0
    const loss = stopDist > 0 ? stopDist * qty + fees : null
    const gain = tpN && (tpN - entry) * s > 0 ? (tpN - entry) * s * qty - fees : null
    const liqBeforeStop = stopN > 0 && (stopN - liq) * s <= 0
    return { qty, margin, liq, fees, loss, gain, liqBeforeStop, stopPct: stopDist / entry * 100 }
  }, [entry, notional, lev, s, symbol, type, venue, stopN, tpN])

  const problems = [
    !notional && 'enter a size',
    type === 'limit' && !entry && 'enter a limit price',
    !stopN && 'a stop-loss is required',
    stopN && entry && (entry - stopN) * s <= 0 && `stop must be ${side === 'long' ? 'below' : 'above'} the entry`,
    tpN && entry && (tpN - entry) * s <= 0 && `take-profit must be ${side === 'long' ? 'above' : 'below'} the entry`,
    calc?.liqBeforeStop && 'liquidation would come before your stop — lower the leverage or tighten the stop',
    calc && calc.margin > available && `not enough available margin (${fmtUsd(available)})`,
    info && notional && notional < info.min_notional && `Binance minimum is ${fmtUsd(info.min_notional, 0)} for ${symbol}`,
  ].filter(Boolean) as string[]

  const stopAt = (pct: number) => entry && setStop((entry * (1 - s * pct / 100)).toFixed(dp))

  async function submit() {
    setErr(null)
    if (problems.length) { setErr(problems[0]); return }
    if (live && !confirm(`LIVE ORDER on Binance ${venue?.env}\n\n${side === 'long' ? 'BUY / LONG' : 'SELL / SHORT'} ${symbol}\n` +
        `${type.toUpperCase()}${type === 'limit' ? ` @ ${price}` : ''} · ${fmtUsd(notional)} at ${lev}x\nStop ${stop}${tp ? ` · Target ${tp}` : ''}\n\nSend it?`)) return
    setBusy(true)
    try {
      const r = await api.trade.order({ symbol, side, type, notional_usd: notional, leverage: lev,
        stop_loss: stopN, take_profit: tpN || null, price: type === 'limit' ? entry : null })
      if (r.warning) setErr(`⚠ ${r.warning}`)       // opened, but the exchange stop failed — say so loudly
      onPlaced(`${r.mode === 'live' ? 'LIVE' : 'Paper'} ${r.kind === 'order' ? 'limit order placed' : 'position opened'}: ${side} ${symbol}`)
      setSize('')
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Order failed')
    } finally {
      setBusy(false)
    }
  }

  async function switchMode(to: 'paper' | 'live') {
    setErr(null)
    if (to === 'paper' && !live) { setConfirmLive(null); return }         // just dismiss the live prompt
    if (to === 'live' && confirmLive === null) { setConfirmLive(''); return }
    try {
      await api.live.setMode('main', to, to === 'live' ? (confirmLive ?? '').trim().toUpperCase() : '')
      setConfirmLive(null)
      onModeChange()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Switch failed')
    }
  }

  const liveBlocked = !venue?.live_allowed ? 'LIVE_TRADING_ALLOWED is off in .env' : !venue?.has_keys ? 'no Binance API key' : null
  return (
    <div className="bg-[#161a1e] rounded-lg p-3 space-y-3 text-[#eaecef]">
      {/* Paper / Live */}
      <div className="space-y-2">
        <div className="grid grid-cols-2 gap-1 bg-[#0b0e11] p-1 rounded-md text-xs font-bold">
          <button onClick={() => switchMode('paper')}
            className={clsx('py-1.5 rounded', !live && confirmLive === null ? 'bg-[#2b3139] text-white' : 'text-[#848e9c] hover:text-white')}>Paper</button>
          <button onClick={() => !live && !liveBlocked && confirmLive === null && switchMode('live')} disabled={!!liveBlocked && !live}
            title={liveBlocked ?? `Real orders on Binance ${venue?.env}`}
            className={clsx('py-1.5 rounded', live ? 'bg-rose-600 text-white'
              : confirmLive !== null ? 'ring-1 ring-rose-500 text-rose-300' : 'text-[#848e9c] hover:text-white disabled:opacity-40')}>
            Live{live ? ` · ${venue?.env}` : ''}
          </button>
        </div>
        {confirmLive !== null && !live && (
          <div className="space-y-1.5">
            <p className="text-[11px] text-rose-300">Type <code className="text-white">{venue?.confirm}</code> to send real orders to Binance.</p>
            <div className="flex gap-1.5">
              <input value={confirmLive} onChange={e => setConfirmLive(e.target.value)} autoFocus spellCheck={false}
                className="flex-1 min-w-0 bg-[#2b3139] rounded px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-rose-500" />
              <button onClick={() => switchMode('live')} disabled={confirmLive.trim().toUpperCase() !== venue?.confirm}
                className="px-2 text-xs font-bold rounded bg-rose-600 disabled:opacity-40">Go live</button>
              <button onClick={() => { setConfirmLive(null); setErr(null) }} className="px-2 text-xs text-[#848e9c]">Cancel</button>
            </div>
          </div>
        )}
      </div>

      {/* Side */}
      <div className="grid grid-cols-2 gap-1 text-sm font-bold">
        <button onClick={() => setSide('long')} className={clsx('py-2 rounded-md', side === 'long' ? 'bg-[#0ecb81] text-white' : 'bg-[#2b3139] text-[#848e9c]')}>Buy / Long</button>
        <button onClick={() => setSide('short')} className={clsx('py-2 rounded-md', side === 'short' ? 'bg-[#f6465d] text-white' : 'bg-[#2b3139] text-[#848e9c]')}>Sell / Short</button>
      </div>

      <div className="flex gap-4 text-xs font-semibold">
        {(['market', 'limit'] as const).map(t => (
          <button key={t} onClick={() => setType(t)} className={clsx('capitalize pb-0.5 border-b-2', type === t ? 'text-[#f0b90b] border-[#f0b90b]' : 'text-[#848e9c] border-transparent')}>{t}</button>
        ))}
        <span className="ml-auto text-[#848e9c]">Avail <span className="text-[#eaecef] tabular-nums">{fmtUsd(available)}</span></span>
      </div>

      {type === 'limit' && (
        <div className="flex gap-1.5">
          <div className="flex-1"><Input label="Price" value={price} onChange={setPrice} suffix="USDT" /></div>
          <button onClick={() => mark && setPrice(mark.toFixed(dp))} className="px-2 text-[11px] rounded-md bg-[#2b3139] text-[#848e9c] hover:text-white">Last</button>
        </div>
      )}

      {/* Leverage */}
      <div className="space-y-1">
        <div className="flex justify-between text-[11px] text-[#848e9c]"><span>Leverage (isolated)</span><span className="text-[#f0b90b] font-bold">{lev}x</span></div>
        <input type="range" min={1} max={maxLev} value={lev} onChange={e => setLev(Number(e.target.value))} className="w-full accent-[#f0b90b]" />
        <div className="flex gap-1">
          {LEVERAGES.filter(l => l <= maxLev).map(l => (
            <button key={l} onClick={() => setLev(l)} className={clsx('flex-1 py-0.5 text-[10px] rounded', lev === l ? 'bg-[#f0b90b] text-black font-bold' : 'bg-[#2b3139] text-[#848e9c]')}>{l}x</button>
          ))}
        </div>
      </div>

      {/* Size */}
      <div className="space-y-1.5">
        <Input label="Size" value={size} onChange={setSize} suffix="USDT" placeholder="position value" />
        <div className="flex gap-1">
          {SIZE_PCTS.map(p => (
            <button key={p} onClick={() => setSize((available * lev * p / 100 * 0.98).toFixed(2))}
              className="flex-1 py-0.5 text-[10px] rounded bg-[#2b3139] text-[#848e9c] hover:text-white">{p}%</button>
          ))}
        </div>
        {calc && <p className="text-[10px] text-[#848e9c] text-right tabular-nums">≈ {calc.qty.toPrecision(4)} {symbol.replace('USDT', '')} · margin {fmtUsd(calc.margin)}</p>}
      </div>

      {/* Stop / target */}
      <div className="space-y-1.5">
        <Input label="Stop-loss" value={stop} onChange={setStop} suffix={calc?.stopPct ? `${calc.stopPct.toFixed(2)}%` : 'req.'} />
        <div className="flex gap-1">
          {STOP_PCTS.map(p => (
            <button key={p} onClick={() => stopAt(p)} className="flex-1 py-0.5 text-[10px] rounded bg-[#2b3139] text-[#848e9c] hover:text-white">{p}%</button>
          ))}
        </div>
        <Input label="Take-profit" value={tp} onChange={setTp} placeholder="optional" />
      </div>

      {/* Summary */}
      {calc && (
        <dl className="text-[11px] space-y-0.5 tabular-nums">
          <Line k="Est. liquidation" v={calc.liq.toFixed(dp)} warn={calc.liqBeforeStop} />
          <Line k="Loss at stop" v={calc.loss != null ? `${fmtUsd(calc.loss)} (${equity ? (calc.loss / equity * 100).toFixed(2) : '—'}% of equity)` : '—'} />
          {calc.gain != null && <Line k="Profit at target" v={`${fmtUsd(calc.gain)}${calc.loss ? ` · ${(calc.gain / calc.loss).toFixed(2)}R` : ''}`} />}
          <Line k="Fees (in + out)" v={fmtUsd(calc.fees)} />
        </dl>
      )}

      {err && <p className="text-xs text-[#f6465d]">{err}</p>}
      <button onClick={submit} disabled={busy}
        className={clsx('w-full py-2.5 rounded-md text-sm font-bold text-white disabled:opacity-50',
          side === 'long' ? 'bg-[#0ecb81] hover:bg-[#0ecb81]/90' : 'bg-[#f6465d] hover:bg-[#f6465d]/90')}>
        {busy ? 'Sending…' : `${side === 'long' ? 'Buy / Long' : 'Sell / Short'} ${symbol.replace('USDT', '')}${live ? ' · LIVE' : ''}`}
      </button>
      <p className="text-[10px] text-[#5e6673] leading-snug">
        {live ? `Real order on Binance ${venue?.env}. The stop is placed on the exchange immediately.`
              : 'Paper trade in the main wallet, filled at Binance mainnet prices with fees and slippage.'}
      </p>
    </div>
  )
}

/** Defined at module level: a component declared inside the form would remount on every
 * keystroke and the field would lose focus. */
function Input({ label, value, onChange, suffix, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; suffix?: string; placeholder?: string
}) {
  return (
    <label className="flex items-center bg-[#2b3139] rounded-md px-3 py-2 text-xs focus-within:ring-1 focus-within:ring-[#f0b90b]">
      <span className="text-[#848e9c] w-20 shrink-0">{label}</span>
      <input value={value} onChange={e => onChange(e.target.value.replace(/[^0-9.]/g, ''))} inputMode="decimal"
        placeholder={placeholder} className="flex-1 min-w-0 bg-transparent text-right text-[#eaecef] tabular-nums outline-none placeholder:text-[#5e6673]" />
      {suffix && <span className="ml-2 text-[#848e9c]">{suffix}</span>}
    </label>
  )
}

function Line({ k, v, warn }: { k: string; v: string; warn?: boolean }) {
  return <div className="flex justify-between"><dt className="text-[#848e9c]">{k}</dt><dd className={warn ? 'text-[#f6465d] font-bold' : 'text-[#eaecef]'}>{v}</dd></div>
}
