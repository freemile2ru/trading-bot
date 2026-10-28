import { useEffect, useMemo, useRef, useState } from 'react'
import { CalendarClock, ChevronLeft, ChevronRight, Landmark, LineChart, Building2, X, ShieldOff, Tag } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import { fmtUsdSigned } from '../utils/format'
import type { CalendarPage as Page, CalendarPageEvent, NearEventSplit } from '../types/ops'

const DAY = 86400

// Display time zones. Central (Chicago) is the default — CST in winter, CDT in summer, the label follows the date.
const ZONES = [
  { id: 'America/Chicago', label: 'Central' },
  { id: 'UTC', label: 'UTC' },
  { id: Intl.DateTimeFormat().resolvedOptions().timeZone, label: 'Local' },
] as const
const ZONE_KEY = 'calendar.zone'
type View = 'day' | 'week' | 'month'
const VIEW_KEY = 'calendar.view'
const HOUR_PX = 48                                    // one hour of the time grid
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

// ── time helpers (everything is bucketed in the display zone) ──────────────────
function parts(ts: number, tz: string) {
  const f = new Intl.DateTimeFormat('en-US', { timeZone: tz, year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short', weekday: 'short' })
  const o: Record<string, string> = {}
  for (const p of f.formatToParts(new Date(ts * 1000))) o[p.type] = p.value
  return { y: +o.year, m: +o.month, d: +o.day, hh: o.hour === '24' ? '00' : o.hour, mm: o.minute, zone: o.timeZoneName, wd: o.weekday }
}
const dayKey = (ts: number, tz: string) => { const p = parts(ts, tz); return `${p.y}-${String(p.m).padStart(2, '0')}-${String(p.d).padStart(2, '0')}` }
const clock = (ts: number, tz: string) => { const p = parts(ts, tz); return `${p.hh}:${p.mm}` }
const fmtTime = (ts: number, tz: string) => { const p = parts(ts, tz); return `${p.hh}:${p.mm} ${p.zone}` }
const fmtFull = (ts: number, tz: string) => { const p = parts(ts, tz); return `${p.wd} ${MONTHS[p.m - 1].slice(0, 3)} ${p.d}, ${p.hh}:${p.mm} ${p.zone}` }
const hourOf = (ts: number, tz: string) => { const q = parts(ts, tz); return +q.hh + +q.mm / 60 }
const fmtIn = (s: number) => {
  if (s <= 0) return 'now'
  const m = Math.round(s / 60)
  if (m < 60) return `${m} min`
  if (m < 48 * 60) return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
  return `${Math.round(m / 1440)} days`
}
const pct = (n: number) => `${n > 0 ? '+' : ''}${n.toFixed(2)}%`

// Day keys are y-m-d in the display zone; shifting by whole days never crosses a zone boundary.
const keyToUtc = (k: string) => { const [y, m, d] = k.split('-').map(Number); return Date.UTC(y, m - 1, d) }
const shiftKey = (k: string, days: number) => new Date(keyToUtc(k) + days * DAY * 1000).toISOString().slice(0, 10)
const weekOf = (k: string) => { const sun = shiftKey(k, -new Date(keyToUtc(k)).getUTCDay()); return Array.from({ length: 7 }, (_, i) => shiftKey(sun, i)) }
const shiftMonth = (k: string, n: number) => { const [y, m] = k.split('-').map(Number); return new Date(Date.UTC(y, m - 1 + n, 1)).toISOString().slice(0, 10) }
const keyLabel = (k: string, long = false) => {
  const [y, m, d] = k.split('-').map(Number)
  const wd = WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]
  return long ? `${wd}, ${MONTHS[m - 1]} ${d}, ${y}` : `${wd} ${d}`
}
/** Calendar days of a month, padded to full weeks (Sunday first). */
function monthGrid(y: number, m: number): { key: string; inMonth: boolean; d: number; dow: number }[] {
  const start = new Date(Date.UTC(y, m - 1, 1)).getUTCDay()
  const days = new Date(Date.UTC(y, m, 0)).getUTCDate()
  const total = Math.ceil((start + days) / 7) * 7
  return Array.from({ length: total }, (_, i) => {
    const dt = new Date(Date.UTC(y, m - 1, 1 + i - start))
    return { key: dt.toISOString().slice(0, 10), inMonth: dt.getUTCMonth() === m - 1, d: dt.getUTCDate(), dow: dt.getUTCDay() }
  })
}

// ── event kinds ────────────────────────────────────────────────────────────────
const KIND: Record<string, { icon: typeof Landmark; name: string; chip: string; bar: string; dot: string; who: string }> = {
  FOMC: { icon: Landmark, name: 'FOMC', chip: 'bg-rose-500/15 text-rose-200 hover:bg-rose-500/25', bar: 'bg-rose-400', dot: 'bg-rose-400', who: 'Crypto entries paused' },
  CPI: { icon: LineChart, name: 'CPI', chip: 'bg-amber-500/15 text-amber-200 hover:bg-amber-500/25', bar: 'bg-amber-400', dot: 'bg-amber-400', who: 'Crypto entries paused' },
  earnings: { icon: Building2, name: 'Earnings', chip: 'bg-sky-500/15 text-sky-200 hover:bg-sky-500/25', bar: 'bg-sky-400', dot: 'bg-sky-400', who: 'Stock trend: no new position' },
}
const kindOf = (e: { kind: string }) => KIND[e.kind] ?? KIND.earnings
const short = (e: CalendarPageEvent) => e.kind === 'earnings' ? e.ticker ?? e.symbol ?? '' : e.kind
const evKey = (e: CalendarPageEvent) => `${e.kind}-${e.ts}-${e.symbol ?? ''}`
const isActive = (e: CalendarPageEvent, now: number, rules: Page['rules']) =>
  e.window ? now >= e.window[0] && now <= e.window[1] : e.kind === 'earnings' && e.ts - now >= 0 && e.ts - now <= rules.earnings_days * DAY

type Hover = { e: CalendarPageEvent; x: number; y: number; below: boolean }
type HoverFn = (e: CalendarPageEvent | null, el?: HTMLElement) => void
const hoverAt = (set: (h: Hover | null) => void): HoverFn => (e, el) => {
  if (!e || !el) return set(null)
  const r = el.getBoundingClientRect()
  set({ e, x: r.left + r.width / 2, y: r.top < 220 ? r.bottom + 8 : r.top - 8, below: r.top < 220 })
}

/** The hover card for an event: when (in the display zone, UTC and New York), the rule, the blackout window, the countdown. */
function HoverCard({ h, tz, now, rules }: { h: Hover; tz: string; now: number; rules: Page['rules'] }) {
  const { e } = h
  const k = kindOf(e)
  const Icon = k.icon
  const active = isActive(e, now, rules)
  const x = Math.min(Math.max(h.x, 150), window.innerWidth - 150)
  return (
    <div className="fixed z-50 w-[18.5rem] pointer-events-none" style={{ left: x, top: h.y, transform: `translate(-50%, ${h.below ? '0' : '-100%'})` }}>
      <div className="rounded-lg border border-gray-700 bg-gray-900 shadow-xl shadow-black/50 p-3 text-xs space-y-2">
        <div className="flex items-start gap-2">
          <span className={clsx('w-1 self-stretch rounded-full shrink-0', k.bar)} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 text-gray-100 font-semibold"><Icon className="w-3.5 h-3.5 text-gray-400 shrink-0" /><span className="truncate">{e.label}</span></div>
            <div className="text-gray-400 font-mono mt-0.5">{fmtFull(e.ts, tz)}</div>
            <div className="text-gray-500 font-mono text-[11px]">{fmtFull(e.ts, 'UTC')} · {fmtFull(e.ts, 'America/New_York')}</div>
          </div>
          <span className={clsx('shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold tabular-nums', e.ts < now - 3600 ? 'bg-gray-800 text-gray-400' : active ? 'bg-rose-900/70 text-rose-200' : 'bg-gray-800 text-gray-200')}>
            {e.ts < now - 3600 ? 'done' : e.ts <= now ? 'now' : `in ${fmtIn(e.ts - now)}`}
          </span>
        </div>
        <div className="flex gap-1.5 text-gray-300"><ShieldOff className="w-3.5 h-3.5 shrink-0 mt-0.5 text-gray-500" /><span>{e.rule}.</span></div>
        {e.window && (
          <div className="text-gray-400"><b className="text-gray-300">Blackout</b> {fmtTime(e.window[0], tz)} → {fmtTime(e.window[1], tz)}
            {active && <span className="ml-1 text-rose-300 font-semibold">· on now</span>}</div>
        )}
        {e.kind === 'earnings' && (
          <div className="text-gray-400"><b className="text-gray-300">Block</b> from {fmtFull(e.ts - rules.earnings_days * DAY, tz)} until it reports
            {active && <span className="ml-1 text-rose-300 font-semibold">· on now</span>}</div>
        )}
        <div className="text-[10px] text-gray-600">{k.who} · click for details in the side panel</div>
      </div>
    </div>
  )
}

/** A Google-style event pill: colour bar, time, name. */
function Pill({ e, tz, now, rules, onClick, onHover }: { e: CalendarPageEvent; tz: string; now: number; rules: Page['rules']; onClick: () => void; onHover: HoverFn }) {
  const k = kindOf(e)
  return (
    <button onClick={e2 => { e2.stopPropagation(); onClick() }}
      onMouseEnter={ev => onHover(e, ev.currentTarget)} onMouseLeave={() => onHover(null)}
      className={clsx('w-full flex items-center gap-1.5 rounded-md pl-1.5 pr-2 py-[3px] text-left text-xs leading-4 transition-colors',
        k.chip, e.ts < now - 3600 && 'opacity-45', isActive(e, now, rules) && 'ring-1 ring-rose-400/80')}>
      <span className={clsx('w-1 self-stretch rounded-full shrink-0', k.bar)} />
      <span className="font-mono text-[11px] opacity-80 shrink-0">{clock(e.ts, tz)}</span>
      <span className="truncate font-medium">{short(e)}</span>
    </button>
  )
}

/** Events whose pills would overlap on the time grid (within 30 minutes of each other), as side-by-side slots. */
function slots(evs: CalendarPageEvent[]): { e: CalendarPageEvent; i: number; n: number }[] {
  const sorted = [...evs].sort((a, b) => a.ts - b.ts)
  const groups: CalendarPageEvent[][] = []
  for (const e of sorted) {
    const g = groups[groups.length - 1]
    if (g && e.ts - g[g.length - 1].ts < 1800) g.push(e); else groups.push([e])
  }
  return groups.flatMap(g => g.map((e, i) => ({ e, i, n: g.length })))
}

/** Day and week views: one column per day, hours down the side, events placed at their time. */
function TimeGrid({ days, byDay, tz, now, rules, todayKey, onPick, onPickDay, onHover }: {
  days: string[]; byDay: Map<string, CalendarPageEvent[]>; tz: string; now: number; rules: Page['rules']; todayKey: string
  onPick: (day: string, e: CalendarPageEvent) => void; onPickDay: (day: string) => void; onHover: HoverFn
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => { if (ref.current) ref.current.scrollTop = 6 * HOUR_PX }, [days.join()])     // open on the morning
  const single = days.length === 1
  const cols = `3.5rem repeat(${days.length}, minmax(0, 1fr))`
  return (
    <div className="rounded-xl border border-gray-800 overflow-hidden bg-gray-950">
      <div className="grid border-b border-gray-800" style={{ gridTemplateColumns: cols }}>
        <div />
        {days.map(k => (
          <button key={k} onClick={() => onPickDay(k)} className={clsx('border-l border-gray-800 px-2 py-2 hover:bg-gray-900', single ? 'text-left flex items-center gap-2' : 'text-center')}>
            <div className="text-[10px] uppercase tracking-wider text-gray-500">{keyLabel(k).split(' ')[0]}</div>
            <div className={clsx('inline-flex items-center justify-center text-base font-semibold rounded-full w-8 h-8', k === todayKey ? 'bg-blue-600 text-white' : 'text-gray-200')}>{+k.slice(8)}</div>
          </button>
        ))}
      </div>
      <div ref={ref} className="overflow-y-auto max-h-[36rem]">
        <div className="grid" style={{ gridTemplateColumns: cols, height: 24 * HOUR_PX }}>
          <div className="relative">
            {Array.from({ length: 24 }, (_, h) => h > 0 && (
              <div key={h} className="absolute right-2 -translate-y-1/2 text-[10px] text-gray-500 font-mono" style={{ top: h * HOUR_PX }}>{String(h).padStart(2, '0')}:00</div>
            ))}
          </div>
          {days.map(k => (
            <div key={k} className={clsx('relative border-l border-gray-800', k === todayKey && 'bg-blue-500/[0.04]')}>
              {Array.from({ length: 24 }, (_, h) => <div key={h} className="absolute inset-x-0 border-t border-gray-800/60" style={{ top: h * HOUR_PX }} />)}
              {slots(byDay.get(k) ?? []).map(({ e, i, n }) => {
                const kd = kindOf(e)
                const Icon = kd.icon
                const top = hourOf(e.ts, tz) * HOUR_PX
                // Week columns fit three pills across; beyond that the last slot becomes "+N" and opens the day.
                const max = single ? 6 : 3
                const shown = Math.min(n, max)
                const overflow = n > max && i === max - 1
                if (i >= max) return null
                const w = `calc((100% - 8px) / ${shown} - 2px)`
                const left = `calc(4px + (100% - 8px) * ${i} / ${shown})`
                return (
                  <div key={evKey(e)}>
                    {e.window && (
                      <div className="absolute inset-x-1 rounded-md bg-rose-500/10 border border-dashed border-rose-600/50 pointer-events-none"
                           style={{ top: top - (e.ts - e.window[0]) / 3600 * HOUR_PX, height: (e.window[1] - e.window[0]) / 3600 * HOUR_PX }} />
                    )}
                    {overflow ? (
                      <button onClick={() => onPickDay(k)} style={{ top: top - 1, left, width: w, minHeight: 24 }}
                        className="absolute z-10 rounded-md bg-gray-800 text-gray-300 hover:bg-gray-700 text-xs font-semibold border border-gray-950"
                        title={`${n - max + 1} more at ${clock(e.ts, tz)} — click for the day`}>+{n - max + 1}</button>
                    ) : (
                      <button onClick={() => onPick(k, e)} onMouseEnter={ev => onHover(e, ev.currentTarget)} onMouseLeave={() => onHover(null)}
                        className={clsx('absolute z-10 flex items-start gap-1 rounded-md pl-1 pr-1.5 py-1 text-left text-xs leading-4 border border-gray-950 overflow-hidden',
                          kd.chip, e.ts < now - 3600 && 'opacity-50', isActive(e, now, rules) && 'ring-1 ring-rose-400/80')}
                        style={{ top: top - 1, left, width: w, minHeight: 24 }}>
                        <span className={clsx('w-1 self-stretch rounded-full shrink-0', kd.bar)} />
                        <span className="min-w-0">
                          <span className="flex items-center gap-1 truncate">
                            {(single || shown === 1) && <Icon className="w-3 h-3 shrink-0" />}
                            {(single || shown < 3) && <span className="font-mono text-[11px] opacity-80">{clock(e.ts, tz)}</span>}
                            <span className="font-medium truncate">{single ? e.label : short(e)}</span>
                          </span>
                          {single && <span className="block text-[11px] opacity-75 truncate">{e.rule}</span>}
                        </span>
                      </button>
                    )}
                  </div>
                )
              })}
              {k === todayKey && (
                <div className="absolute inset-x-0 z-20 border-t-2 border-red-500 pointer-events-none" style={{ top: hourOf(now, tz) * HOUR_PX }}>
                  <span className="absolute -left-[5px] -top-[5px] w-2 h-2 rounded-full bg-red-500" />
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function MonthGrid({ cells, byDay, tz, now, rules, todayKey, selected, onPick, onPickDay, onOpenDay, onHover }: {
  cells: ReturnType<typeof monthGrid>; byDay: Map<string, CalendarPageEvent[]>; tz: string; now: number; rules: Page['rules']
  todayKey: string; selected: string | null; onPick: (day: string, e: CalendarPageEvent) => void; onPickDay: (day: string) => void; onOpenDay: (day: string) => void
  onHover: HoverFn
}) {
  return (
    <div className="rounded-xl border border-gray-800 overflow-hidden bg-gray-950">
      <div className="grid grid-cols-7 border-b border-gray-800">
        {WEEKDAYS.map(w => <div key={w} className="px-2 py-2 text-center text-[10px] uppercase tracking-wider text-gray-500">{w}</div>)}
      </div>
      <div className="grid grid-cols-7">
        {cells.map((c, i) => {
          const evs = byDay.get(c.key) ?? []
          const isToday = c.key === todayKey
          return (
            <div key={c.key} onClick={() => onPickDay(c.key)} onDoubleClick={() => onOpenDay(c.key)}
              className={clsx('min-h-[6rem] lg:min-h-[6.75rem] p-1.5 cursor-pointer flex flex-col gap-1 border-gray-800/80 transition-colors hover:bg-gray-900/60',
                i % 7 !== 0 && 'border-l', i >= 7 && 'border-t', !c.inMonth && 'bg-gray-900/40', (c.dow === 0 || c.dow === 6) && c.inMonth && 'bg-gray-900/20',
                c.key === selected && 'ring-2 ring-inset ring-blue-500/80')}>
              <div className="flex items-center justify-between">
                <span className={clsx('text-xs w-6 h-6 inline-flex items-center justify-center rounded-full',
                  isToday ? 'bg-blue-600 text-white font-bold' : c.inMonth ? 'text-gray-300' : 'text-gray-600')}>{c.d}</span>
                {evs.length > 0 && <span className="md:hidden flex gap-0.5">{evs.slice(0, 4).map((e, j) => <span key={j} className={clsx('w-1.5 h-1.5 rounded-full', kindOf(e).dot)} />)}</span>}
              </div>
              <div className="hidden md:flex flex-col gap-0.5">
                {evs.slice(0, 3).map(e => <Pill key={evKey(e)} e={e} tz={tz} now={now} rules={rules} onClick={() => onPick(c.key, e)} onHover={onHover} />)}
                {evs.length > 3 && <button onClick={e => { e.stopPropagation(); onOpenDay(c.key) }} className="text-[11px] text-gray-400 hover:text-gray-200 text-left px-1.5">+{evs.length - 3} more</button>}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function Split({ label, s, help }: { label: string; s: NearEventSplit; help: string }) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900/60 p-3" title={help}>
      <div className="text-[10px] uppercase tracking-wider text-gray-500">{label}</div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="text-xl font-bold text-white">{s.trades}</span>
        <span className="text-[11px] text-gray-500">trades · {s.closed} closed</span>
      </div>
      <div className="mt-1 text-xs text-gray-300">
        {s.closed ? <>avg <b className={clsx(s.avg_r! >= 0 ? 'text-emerald-300' : 'text-rose-300')}>{s.avg_r! > 0 ? '+' : ''}{s.avg_r}R</b>
          {' · '}{s.win_pct}% won · <span className={s.pnl_usd >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{fmtUsdSigned(s.pnl_usd)}</span></>
          : <span className="text-gray-500">nothing closed yet</span>}
      </div>
    </div>
  )
}

/** Scheduled releases the books trade around — FOMC, CPI and the stock perps' earnings — on a day / week / month calendar. */
export function CalendarPage() {
  const [p, setP] = useState<Page | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now() / 1000)
  const [tz, setTz] = useState<string>(() => { try { return localStorage.getItem(ZONE_KEY) || ZONES[0].id } catch { return ZONES[0].id } })
  const [view, setView] = useState<View>(() => { try { return (localStorage.getItem(VIEW_KEY) as View) || 'month' } catch { return 'month' } })
  const [anchor, setAnchor] = useState(() => dayKey(Date.now() / 1000, tz))      // the day the view is centred on
  const [selected, setSelected] = useState<string | null>(null)                  // day shown in the sidebar
  const [picked, setPicked] = useState<string | null>(null)                      // event key expanded in the sidebar
  const [hover, setHover] = useState<Hover | null>(null)
  const onHover = useMemo(() => hoverAt(setHover), [])

  useEffect(() => {
    let live = true
    const load = () => api.ops.calendarPage().then(x => { if (live) { setP(x); setErr(null) } }).catch(e => { if (live) setErr(String(e?.message ?? e)) })
    load()
    const id = setInterval(load, 5 * 60_000)
    const tick = setInterval(() => setNow(Date.now() / 1000), 30_000)
    return () => { live = false; clearInterval(id); clearInterval(tick) }
  }, [])
  const pickZone = (z: string) => { setTz(z); try { localStorage.setItem(ZONE_KEY, z) } catch { /* fine */ } }
  const pickView = (v: View) => { setView(v); try { localStorage.setItem(VIEW_KEY, v) } catch { /* fine */ } }

  const todayKey = dayKey(now, tz)
  const byDay = useMemo(() => {
    const m = new Map<string, CalendarPageEvent[]>()
    for (const e of p?.events ?? []) { const k = dayKey(e.ts, tz); m.set(k, [...(m.get(k) ?? []), e]) }
    return m
  }, [p, tz])
  const [ay, am] = anchor.split('-').map(Number)
  const cells = useMemo(() => monthGrid(ay, am), [ay, am])
  const upcoming = (p?.events ?? []).filter(e => e.ts >= now - 3600)
  const next = upcoming[0]
  const go = (delta: number) => setAnchor(a => view === 'month' ? shiftMonth(a, delta) : shiftKey(a, delta * (view === 'week' ? 7 : 1)))
  const goToday = () => { setAnchor(todayKey); setSelected(todayKey) }
  const pickEvent = (day: string, e: CalendarPageEvent) => { setSelected(day); setPicked(evKey(e)); setHover(null) }
  const openDay = (day: string) => { setAnchor(day); setSelected(day); pickView('day') }
  const title = view === 'month' ? `${MONTHS[am - 1]} ${ay}`
    : view === 'week' ? (() => { const w = weekOf(anchor); return `${keyLabel(w[0], true).replace(/^\w+, /, '')} – ${keyLabel(w[6], true).replace(/^\w+, /, '')}` })()
    : keyLabel(anchor, true)
  const inView = view === 'month' ? cells.filter(c => c.inMonth).map(c => c.key) : view === 'week' ? weekOf(anchor) : [anchor]

  if (err && !p) return <div className="card text-sm text-rose-300">Calendar unavailable: {err}</div>
  if (!p) return <div className="card text-sm text-gray-500">Loading the calendar…</div>

  const viewCount = inView.reduce((n, k) => n + (byDay.get(k)?.length ?? 0), 0)
  const dayEvents = selected ? byDay.get(selected) ?? [] : []
  const zoneLabel = ZONES.find(z => z.id === tz)?.label ?? tz

  return (
    <div className="max-w-[1600px] mx-auto space-y-4">
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        {/* ── Calendar ── */}
        <section className="card space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <button onClick={goToday} className="px-3 py-1.5 text-xs font-semibold rounded-md border border-gray-700 text-gray-200 hover:bg-gray-800">Today</button>
            <div className="flex items-center">
              <button onClick={() => go(-1)} className="p-1.5 rounded-full hover:bg-gray-800 text-gray-400" aria-label={`Previous ${view}`}><ChevronLeft className="w-4 h-4" /></button>
              <button onClick={() => go(1)} className="p-1.5 rounded-full hover:bg-gray-800 text-gray-400" aria-label={`Next ${view}`}><ChevronRight className="w-4 h-4" /></button>
            </div>
            <h3 className="text-lg font-semibold text-white">{title}</h3>
            <span className="text-xs text-gray-500">{viewCount} release{viewCount === 1 ? '' : 's'}</span>
            <div className="ml-auto flex items-center gap-2">
              <div className="flex rounded-md border border-gray-700 overflow-hidden text-xs">
                {(['day', 'week', 'month'] as View[]).map(v => (
                  <button key={v} onClick={() => pickView(v)}
                    className={clsx('px-3 py-1.5 capitalize', view === v ? 'bg-gray-700 text-white' : 'bg-gray-900 text-gray-400 hover:text-gray-200')}>{v}</button>
                ))}
              </div>
              <div className="flex rounded-md border border-gray-700 overflow-hidden text-xs">
                {ZONES.map(z => (
                  <button key={z.id + z.label} onClick={() => pickZone(z.id)}
                    className={clsx('px-3 py-1.5', tz === z.id ? 'bg-blue-600 text-white' : 'bg-gray-900 text-gray-400 hover:text-gray-200')}>{z.label}</button>
                ))}
              </div>
            </div>
          </div>

          {view === 'month'
            ? <MonthGrid cells={cells} byDay={byDay} tz={tz} now={now} rules={p.rules} todayKey={todayKey} selected={selected} onPick={pickEvent} onPickDay={setSelected} onOpenDay={openDay} onHover={onHover} />
            : <TimeGrid days={inView} byDay={byDay} tz={tz} now={now} rules={p.rules} todayKey={todayKey} onPick={pickEvent} onPickDay={setSelected} onHover={onHover} />}
          {hover && <HoverCard h={hover} tz={tz} now={now} rules={p.rules} />}

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-gray-500">
            {Object.values(KIND).map(v => <span key={v.name} className="flex items-center gap-1.5"><span className={clsx('w-2.5 h-2.5 rounded-sm', v.dot)} />{v.name} <span className="text-gray-600">· {v.who.toLowerCase()}</span></span>)}
            <span className="flex items-center gap-1.5"><span className="w-4 h-2.5 rounded-sm border border-dashed border-rose-600/70 bg-rose-500/10" />blackout window</span>
            <span className="ml-auto">Times in {zoneLabel} ({parts(now, tz).zone})</span>
          </div>
        </section>

        {/* ── Sidebar ── */}
        <aside className="space-y-4">
          {/* Next / blackout */}
          <section className={clsx('card', p.blackout ? 'border-rose-800/70 bg-rose-950/30' : '')}>
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-gray-500">
              {p.blackout ? <ShieldOff className="w-3.5 h-3.5 text-rose-400" /> : <CalendarClock className="w-3.5 h-3.5" />}
              {p.blackout ? 'Entry blackout on' : 'Next release'}
            </div>
            {p.blackout ? (
              <>
                <div className="mt-1 text-xl font-bold text-rose-200">{p.blackout.split(' — ')[0]}</div>
                <div className="mt-1 text-xs text-rose-300/80">Autotrader and Claude swing open nothing new until {p.next_macro ? fmtTime(p.next_macro.ts + p.rules.macro_after_min * 60, tz) : '—'}. Open positions keep their stops; hedged, event and cascade books carry on.</div>
              </>
            ) : next ? (
              <>
                <div className="mt-1 text-3xl font-bold text-white tabular-nums">{fmtIn(next.ts - now)}</div>
                <div className="mt-1 text-sm font-semibold text-gray-100">{next.label}</div>
                <div className="text-xs text-gray-400">{fmtFull(next.ts, tz)}</div>
                <div className="mt-2 text-[11px] text-gray-500 flex items-start gap-1.5"><ShieldOff className="w-3.5 h-3.5 shrink-0 mt-0.5" />{next.rule}</div>
              </>
            ) : <div className="mt-1 text-sm text-gray-400">Nothing scheduled.</div>}
          </section>

          {/* Selected day / event, or the agenda */}
          <section className="card space-y-2">
            {selected ? (
              <>
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-semibold text-white">{keyLabel(selected, true)}</h4>
                  <button onClick={() => { setSelected(null); setPicked(null) }} className="text-gray-500 hover:text-gray-300" aria-label="Close"><X className="w-4 h-4" /></button>
                </div>
                {dayEvents.length === 0 ? <div className="text-xs text-gray-500">No scheduled releases.</div> : (
                  <ul className="space-y-1.5">
                    {dayEvents.map(e => {
                      const k = kindOf(e)
                      const Icon = k.icon
                      const open = picked === evKey(e)
                      return (
                        <li key={evKey(e)} className={clsx('rounded-lg border p-2.5 text-xs', open ? 'border-gray-700 bg-gray-900' : 'border-gray-800')}>
                          <button className="w-full flex items-center gap-2 text-left" onClick={() => setPicked(open ? null : evKey(e))}>
                            <span className={clsx('w-1 self-stretch rounded-full shrink-0', k.bar)} />
                            <Icon className="w-3.5 h-3.5 text-gray-400 shrink-0" />
                            <span className="flex-1 min-w-0">
                              <span className="block text-gray-100 font-medium truncate">{e.label}</span>
                              <span className="block text-gray-500 font-mono">{fmtTime(e.ts, tz)}</span>
                            </span>
                            <span className="text-gray-400 tabular-nums">{e.ts < now - 3600 ? 'done' : `in ${fmtIn(e.ts - now)}`}</span>
                          </button>
                          {open && (
                            <div className="mt-2 pl-3 text-[11px] text-gray-400 space-y-1.5 border-l border-gray-800">
                              <div className="flex gap-1.5"><ShieldOff className="w-3.5 h-3.5 shrink-0 mt-0.5 text-gray-500" /><span><b className="text-gray-300">Rule.</b> {e.rule}.</span></div>
                              {e.window && <div><b className="text-gray-300">Blackout window.</b> {fmtTime(e.window[0], tz)} → {fmtTime(e.window[1], tz)}<br /><span className="text-gray-500">{fmtTime(e.window[0], 'UTC')} → {fmtTime(e.window[1], 'UTC')}</span></div>}
                              {e.kind === 'earnings' && <div><b className="text-gray-300">Earnings block.</b> From {fmtFull(e.ts - p.rules.earnings_days * DAY, tz)} until it reports; a held {e.symbol} position is kept.</div>}
                              <div className="text-gray-500">{fmtFull(e.ts, 'UTC')} · {fmtFull(e.ts, 'America/New_York')}</div>
                            </div>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                )}
              </>
            ) : (
              <>
                <h4 className="text-sm font-semibold text-white">Up next</h4>
                {upcoming.length === 0 ? <div className="text-xs text-gray-500">Nothing scheduled.</div> : (
                  <ul className="space-y-1">
                    {upcoming.slice(0, 10).map(e => {
                      const k = kindOf(e)
                      const q = parts(e.ts, tz)
                      return (
                        <li key={evKey(e)}>
                          <button onClick={() => { setAnchor(dayKey(e.ts, tz)); pickEvent(dayKey(e.ts, tz), e) }}
                            onMouseEnter={ev => onHover(e, ev.currentTarget)} onMouseLeave={() => onHover(null)}
                            className="w-full flex items-center gap-2 rounded-md px-1.5 py-1 text-left text-xs hover:bg-gray-900">
                            <span className={clsx('w-1 self-stretch rounded-full shrink-0', k.bar)} />
                            <span className="w-24 shrink-0 text-gray-500 font-mono">{q.wd} {q.m}/{q.d} {q.hh}:{q.mm}</span>
                            <span className="flex-1 truncate text-gray-200">{e.label}</span>
                            <span className="text-gray-500 tabular-nums">{fmtIn(e.ts - now)}</span>
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                )}
                <div className="text-[10px] text-gray-600">Click a day or an event on the calendar for details; double-click a day to open it.</div>
              </>
            )}
          </section>

          {/* Rules */}
          <section className="card space-y-2 text-[11px] text-gray-400">
            <h4 className="text-sm font-semibold text-white">What the books do</h4>
            <div className="flex gap-2"><span className={clsx('w-1 rounded-full shrink-0', KIND.FOMC.bar)} /><span><b className="text-gray-200">FOMC / CPI.</b> No new short-term crypto entries from {p.rules.macro_before_min} min before to {p.rules.macro_after_min} min after the release.</span></div>
            <div className="flex gap-2"><span className={clsx('w-1 rounded-full shrink-0', KIND.earnings.bar)} /><span><b className="text-gray-200">Earnings.</b> The stock trend book opens no new position in a name reporting within {p.rules.earnings_days} days; held positions ride through.</span></div>
            <div className="flex gap-2"><Tag className="w-3.5 h-3.5 shrink-0 mt-0.5 text-gray-500" /><span><b className="text-gray-200">Tagging.</b> Trades opened within {p.settings.near_h}h of FOMC/CPI or {p.settings.near_earnings_days} days of a stock's earnings are marked so the rules can be judged.</span></div>
            <div className="pt-1 text-[10px] text-gray-600">
              FOMC/CPI: published 2026–27 schedules (2027 FOMC tentative), 14:00 and 08:30 New York. Earnings: Nasdaq's calendar for {p.earnings_tickers.length} US-listed stock perps, {p.settings.lookahead_days} days ahead,
              refreshed {p.earnings_refreshed_at ? `${Math.round((now - p.earnings_refreshed_at) / 3600)}h ago` : 'not yet'}. Samsung, SK Hynix and Tencent aren't covered.
            </div>
          </section>
        </aside>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-white">Did the rules help?</h3>
            <p className="text-[11px] text-gray-500 mt-0.5">Trades opened since tagging began (28 Oct 2026) by the autotrader, Claude swing and the stock trend book, split by whether they opened near a release. Judge after a month or so.</p>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Split label="Near a release" s={p.trades.near} help="opened within 24h of FOMC/CPI or 3 days of that stock's earnings (outside the blackout window)" />
            <Split label="Clear of releases" s={p.trades.clear} help="every other tagged trade from those books" />
          </div>
          {p.trades.rows.length > 0 && (
            <ul className="space-y-1 max-h-64 overflow-y-auto">
              {p.trades.rows.map((t, i) => (
                <li key={i} className="flex items-center gap-2 text-[11px] text-gray-300">
                  <span className="text-gray-500 w-20 shrink-0">{t.book}</span>
                  <span className={clsx('w-10 shrink-0 font-semibold', t.direction === 'long' ? 'text-emerald-300' : 'text-rose-300')}>{t.direction}</span>
                  <span className="w-24 shrink-0 font-mono">{t.symbol.replace('USDT', '')}</span>
                  <span className="flex-1 truncate text-gray-400">{t.event}</span>
                  <span className={clsx('w-14 text-right font-mono', t.r == null ? 'text-gray-500' : t.r >= 0 ? 'text-emerald-300' : 'text-rose-300')}>{t.r == null ? 'open' : `${t.r > 0 ? '+' : ''}${t.r}R`}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card space-y-2">
          <div>
            <h3 className="text-sm font-semibold text-white">BTC at recent releases</h3>
            <p className="text-[11px] text-gray-500 mt-0.5">The release hour's move, the two-hour high-to-low range, and the day after — why the window exists.</p>
          </div>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-gray-500">
                <th className="text-left font-semibold pb-1">Release</th>
                <th className="text-right font-semibold pb-1">Hour</th>
                <th className="text-right font-semibold pb-1">2h range</th>
                <th className="text-right font-semibold pb-1">24h</th>
              </tr>
            </thead>
            <tbody>
              {p.past.map(e => (
                <tr key={e.ts} className="border-t border-gray-800/60">
                  <td className="py-1.5 text-gray-300"><span className={clsx('inline-block w-1.5 h-1.5 rounded-full mr-2', kindOf(e).dot)} />{e.label} <span className="text-gray-500">{fmtFull(e.ts, tz)}</span></td>
                  {e.btc ? (
                    <>
                      <td className={clsx('py-1.5 text-right font-mono', e.btc.hour_pct >= 0 ? 'text-emerald-300' : 'text-rose-300')}>{pct(e.btc.hour_pct)}</td>
                      <td className="py-1.5 text-right font-mono text-gray-300">{e.btc.range_2h_pct.toFixed(2)}%</td>
                      <td className={clsx('py-1.5 text-right font-mono', e.btc.day_pct >= 0 ? 'text-emerald-300' : 'text-rose-300')}>{pct(e.btc.day_pct)}</td>
                    </>
                  ) : <td colSpan={3} className="py-1.5 text-right text-gray-500">no data</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  )
}
