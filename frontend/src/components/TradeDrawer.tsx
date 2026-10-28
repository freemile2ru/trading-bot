import { useEffect, useState, type ReactNode } from 'react'
import { X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsd, fmtUsdSigned } from '../utils/format'
import { SYSTEM_LABEL, type SystemKey, type TradeRecord } from '../types/overview'
import { TradeDebriefBlock } from './DebriefViews'

type Rec = Record<string, any>   // a stored trade: shape varies by system and age

const px = (v: unknown) => (typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 2 : 6 }) : '—')
const when = (v: unknown) => {
  const ts = typeof v === 'number' ? v * 1000 : typeof v === 'string' ? Date.parse(v) : NaN
  return Number.isFinite(ts) ? new Date(ts).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
}
const hold = (min: number | null) => {
  if (min == null) return '—'
  if (min < 60) return `${min.toFixed(min < 10 ? 1 : 0)}m`
  const h = Math.floor(min / 60)
  return h < 24 ? `${h}h ${Math.round(min % 60)}m` : `${Math.floor(h / 24)}d ${h % 24}h`
}

// Shown in their own sections; everything else lands in "Other recorded fields".
const CURATED = new Set([
  'id', 'system', 'kind', 'symbol', 'direction', 'side', 'status', 'entry_price', 'current_price', 'exit_price', 'stop_loss', 'sl',
  'original_stop_loss', 'take_profit_1', 'take_profit_2', 'tp', 'quantity', 'qty', 'leverage', 'margin_usd', 'margin', 'notional_usd',
  'notional', 'position_value_usd', 'liquidation_price', 'liq_price', 'pnl_usd', 'net_pnl', 'gross_pnl', 'fees_usd', 'fees', 'funding_usd',
  'funding', 'risk_usd', 'r', 'opened_at', 'closed_at', 'entry_ts', 'exit_ts', 'hold_s', 'close_reason', 'exit_reason', 'exit_note',
  'timeframe', 'hold_duration', 'hold_days', 'claude_thesis', 'oracle_reasoning', 'invalidation', 'entry_reasons', 'setups_at_entry',
  'conviction_at_entry', 'claude_confidence', 'confirmed_by', 'oracle_confidence', 'entry_score', 'fa_at_entry', 'adjustments',
  'features', 'mfe_bps', 'mae_bps', 'peak_r', 'entry_type', 'limit_price', 'claude_entry', 'strategy', 'experiment', 'pnl_pct', 'roe_pct',
])

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex justify-between gap-3 py-0.5">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-200 tabular-nums text-right">{value ?? '—'}</span>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return <div><div className="font-semibold text-gray-300 mb-1">{title}</div>{children}</div>
}

/** The story of one trade: why it was taken, how it was managed, how it ended. */
function Story({ t }: { t: Rec }) {
  const scalp = t.kind === 'scalp'
  const entry = t.entry_price, exit = scalp ? t.exit_price : t.status === 'closed' ? t.current_price : null
  const stop = scalp ? t.sl : t.stop_loss
  const target = scalp ? t.tp : t.take_profit_2
  const net = scalp ? t.net_pnl : t.pnl_usd
  const r = scalp ? t.r : t.risk_usd ? t.pnl_usd / t.risk_usd : null
  const opened = scalp ? t.entry_ts : t.opened_at
  const closed = scalp ? t.exit_ts : t.closed_at
  const held = scalp ? (t.hold_s != null ? t.hold_s / 60 : null)
    : t.opened_at ? ((t.closed_at ? Date.parse(t.closed_at) : Date.now()) - Date.parse(t.opened_at)) / 60000 : null
  const thesis = t.claude_thesis || t.oracle_reasoning
  const extra = Object.entries(t).filter(([k, v]) => !CURATED.has(k) && v !== null && v !== undefined && v !== '' &&
    !(typeof v === 'object' && Object.keys(v as object).length === 0))

  return (
    <div className="space-y-5 text-xs">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
        <Section title="Levels & size">
          <Row label="Entry" value={`${px(entry)}${t.entry_type ? ` (${t.entry_type})` : ''}`} />
          {t.limit_price != null && <Row label="Limit price" value={px(t.limit_price)} />}
          {t.claude_entry != null && <Row label="Claude's entry" value={px(t.claude_entry)} />}
          <Row label={t.status === 'open' ? 'Mark' : 'Exit'} value={px(t.status === 'open' ? t.current_price : exit)} />
          <Row label="Stop" value={t.original_stop_loss != null ? `${px(t.original_stop_loss)} → ${px(stop)}` : px(stop)} />
          <Row label="Target" value={t.tp_set === false ? 'none' : px(target)} />
          <Row label="Size" value={`${fmtUsd(t.notional_usd ?? t.notional ?? t.position_value_usd, 0)} · ${px(t.quantity ?? t.qty)} units`} />
          <Row label="Leverage · margin" value={`${t.leverage ?? '—'}x · ${fmtUsd(t.margin_usd ?? t.margin)}`} />
          <Row label="Liquidation" value={px(t.liquidation_price ?? t.liq_price)} />
          <Row label="Risk at entry" value={fmtUsd(t.risk_usd)} />
        </Section>
        <Section title="Result">
          <Row label="Net P&L" value={<span className={clsx(net > 0 ? 'text-emerald-400' : net < 0 ? 'text-red-400' : '')}>{fmtUsdSigned(net)}</span>} />
          <Row label="R" value={r == null ? '—' : `${r > 0 ? '+' : ''}${r.toFixed(2)}R`} />
          {scalp && <Row label="Gross" value={fmtUsdSigned(t.gross_pnl)} />}
          <Row label="Fees · funding" value={`${fmtUsd(t.fees_usd ?? t.fees)} · ${fmtUsd(t.funding_usd ?? t.funding)}`} />
          {(t.mfe_bps != null || t.peak_r != null) && (
            <Row label="Best move" value={t.peak_r != null ? `${t.peak_r.toFixed(2)}R` : `${t.mfe_bps.toFixed(1)} bps`} />
          )}
          <Row label="Opened" value={when(opened)} />
          <Row label="Closed" value={t.status === 'open' ? 'still open' : when(closed)} />
          <Row label="Held" value={hold(held)} />
          <Row label="How it ended" value={String(t.close_reason ?? t.exit_reason ?? (t.status === 'open' ? '—' : '')).replace(/_/g, ' ')} />
          {t.exit_note && <Row label="Exit note" value={t.exit_note} />}
        </Section>
      </div>

      {(thesis || t.invalidation) && (
        <Section title="Thesis">
          {thesis && <p className="text-gray-300 leading-relaxed">{thesis}</p>}
          {t.invalidation && <p className="text-gray-500 mt-1">Wrong if: {t.invalidation}</p>}
        </Section>
      )}

      {(t.confirmed_by || t.conviction_at_entry != null || t.setups_at_entry?.length || t.fa_at_entry || t.entry_score != null) && (
        <Section title="Decision">
          {t.strategy && <Row label="Opened by" value={String(t.strategy).replace(/_/g, ' ')} />}
          {t.confirmed_by && <Row label="Confirmed by" value={t.confirmed_by === 'claude' ? `Claude (${t.claude_confidence}%)` : String(t.confirmed_by).replace(/_/g, ' ')} />}
          {t.entry_score != null && <Row label="Timing score" value={t.entry_score} />}
          {t.conviction_at_entry != null && <Row label="Conviction" value={Number(t.conviction_at_entry).toFixed(2)} />}
          {!!t.setups_at_entry?.length && <Row label="Setups" value={t.setups_at_entry.join(', ').replace(/_/g, ' ')} />}
          {t.fa_at_entry && <Row label="FA at entry" value={`${t.fa_at_entry.score >= 0 ? '+' : ''}${Number(t.fa_at_entry.score).toFixed(2)} ${t.fa_at_entry.label}`} />}
          {!!t.entry_reasons?.length && (
            <ul className="list-disc list-inside text-gray-400 mt-1 space-y-0.5">{t.entry_reasons.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul>
          )}
        </Section>
      )}

      {!!t.adjustments?.length && (
        <Section title="What happened while open">
          <table className="w-full">
            <tbody>
              {t.adjustments.map((a: Rec, i: number) => (
                <tr key={i} className="border-t border-gray-800/60 align-top">
                  <td className="py-1 pr-3 text-gray-500 whitespace-nowrap">{when(a.ts)}</td>
                  <td className="py-1 pr-3 text-white whitespace-nowrap">{a.action}{a.kind ? ` · ${String(a.kind).replace(/_/g, ' ')}` : ''}</td>
                  <td className="py-1 pr-3 tabular-nums text-gray-300 whitespace-nowrap">
                    {a.quantity ? `${a.quantity} @ ${px(a.price)}` : ''}{a.new_stop != null ? ` stop → ${px(a.new_stop)}` : ''}{a.new_target != null ? ` target → ${px(a.new_target)}` : ''}
                  </td>
                  <td className="py-1 text-gray-500">{a.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}

      {scalp && t.features && Object.keys(t.features).length > 0 && (
        <Section title="Market at entry">
          <div className="grid grid-cols-2 gap-x-6">
            {Object.entries(t.features as Rec).map(([k, v]) => <Row key={k} label={k.replace(/_/g, ' ')} value={typeof v === 'number' ? Number(v.toFixed(4)) : String(v)} />)}
          </div>
        </Section>
      )}

      {extra.length > 0 && (
        <details>
          <summary className="cursor-pointer font-semibold text-gray-400">Other recorded fields ({extra.length})</summary>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 mt-2">
            {extra.map(([k, v]) => <Row key={k} label={k} value={<span className="font-mono">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>} />)}
          </div>
        </details>
      )}
    </div>
  )
}

/** Side drawer with one trade's full record — opened from the Trades and History pages. */
export function TradeDrawer({ system, id, onClose }: { system: SystemKey; id: string; onClose: () => void }) {
  const [t, setT] = useState<TradeRecord | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let stale = false                 // clicking through trades quickly: only the last one may render
    setT(null); setErr(null)
    api.positions.detail(system, id)
      .then(x => { if (!stale) setT(x) })
      .catch(e => { if (!stale) setErr(e instanceof Error ? e.message : 'Could not load the trade') })
    return () => { stale = true }
  }, [system, id])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const r = t as Rec | null
  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <aside className="relative w-full max-w-2xl h-full overflow-y-auto bg-gray-900 border-l border-gray-800 p-5 shadow-2xl">
        <div className="flex items-start gap-3 mb-4">
          <div>
            <div className="text-lg font-bold text-white">
              {r ? `${String(r.symbol).replace('USDT', '')} ${(r.direction ?? r.side) === 'long' ? 'long' : 'short'}` : 'Trade'}
            </div>
            <div className="text-xs text-gray-500">{SYSTEM_LABEL[system]} · <span className="font-mono">{id}</span>{r?.status === 'open' ? ' · open' : ''}</div>
          </div>
          <button onClick={onClose} className="ml-auto p-1.5 rounded hover:bg-gray-800 text-gray-400" aria-label="Close"><X className="w-4 h-4" /></button>
        </div>
        {err && <p className="text-sm text-red-400">{err}</p>}
        {!t && !err && <p className="text-sm text-gray-500">Loading…</p>}
        {r && r.status !== 'open' && (
          <div className="mb-5">
            <div className="font-semibold text-gray-300 text-xs mb-1.5">Debrief</div>
            <TradeDebriefBlock system={system} id={id} status={String(r.status ?? '')} />
          </div>
        )}
        {r && <Story t={r} />}
      </aside>
    </div>
  )
}
