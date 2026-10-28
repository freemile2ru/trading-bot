import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, ChevronDown, ChevronRight, ExternalLink, Newspaper, RefreshCw, Search } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../utils/api'
import type { NewsFilters, NewsItem, NewsResearch, NewsScope, NewsScores, NewsSort, NewsStatus, ReactionStats } from '../types/news'
import { EditionCard, ImportantNews, MoodCard, useLatestEdition } from './news/Briefing'

const SCOPES: [NewsScope | 'pending' | '', string][] = [
  ['', 'All'], ['coin', 'Coins'], ['market', 'Market'], ['macro', 'Macro'], ['pending', 'Waiting for Claude'], ['irrelevant', 'Ignored'],
]
const WINDOWS: [number, string][] = [[6, '6h'], [24, '24h'], [48, '48h'], [168, '7d']]
const REFRESH_MS = 60_000
const SORTS: [NewsSort, string, string][] = [
  ['newest', 'Newest', 'by publish time'],
  ['importance', 'Importance', "Claude's 1–5 rating, then newest"],
  ['impact', 'Impact', 'importance plus how clearly it points up or down'],
  ['weight', 'Score weight', 'what it counts for in the score right now (importance × recency)'],
  ['coverage', 'Coverage', 'how many outlets and similar headlines carried it'],
  ['reaction', 'Price reaction', 'biggest move after it was first seen'],
]
// Last answers per filter set: switching tabs and back renders at once, then refreshes.
const memo: { items: Record<string, NewsItem[]>; scores?: NewsScores; status?: NewsStatus } = { items: {} }

const ago = (ts: number | null | undefined) => {
  if (!ts) return '—'
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60))
  return m < 60 ? `${m}m ago` : m < 48 * 60 ? `${Math.floor(m / 60)}h ${m % 60}m ago` : `${Math.floor(m / 1440)}d ago`
}
const stamp = (ts: number | null | undefined) =>
  ts ? new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
const pct = (v: number | null | undefined, d = 2) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`)
const tone = (v: number | null | undefined, dead = 0) =>
  v == null || Math.abs(v) <= dead ? 'text-gray-400' : v > 0 ? 'text-emerald-400' : 'text-red-400'
const typeLabel = (t: string | null) => (t ? t.replace(/_/g, ' ') : 'unclassified')

function Importance({ n, provisional }: { n: number; provisional?: boolean }) {
  return (
    <span className="inline-flex gap-0.5" title={provisional ? 'Not read by Claude yet — counted at the lowest weight' : `Importance ${n} of 5`}>
      {[1, 2, 3, 4, 5].map(i => (
        <span key={i} className={clsx('w-1.5 h-1.5 rounded-full',
          i <= n ? (provisional ? 'bg-gray-500' : n >= 4 ? 'bg-amber-400' : 'bg-gray-300') : 'bg-gray-700')} />
      ))}
    </span>
  )
}

function Direction({ d, provisional }: { d: number; provisional?: boolean }) {
  const label = Math.abs(d) < 0.2 ? 'neutral' : d > 0 ? 'bullish' : 'bearish'
  return (
    <span className={clsx('text-[11px] font-semibold tabular-nums', provisional ? 'text-gray-500' : tone(d, 0.19))}
      title={provisional ? 'Dictionary sentiment (VADER), halved — until Claude reads it' : 'Claude: expected short-term price impact, −1 to +1'}>
      {label} {d > 0 ? '+' : ''}{d.toFixed(1)}
    </span>
  )
}

/** Diverging bar: red left of centre, green right, for a −1…+1 score. */
function ScoreBar({ v }: { v: number }) {
  const w = Math.min(50, Math.abs(v) * 50)
  return (
    <div className="relative h-1.5 w-full bg-gray-800 rounded-full overflow-hidden">
      <div className="absolute top-0 bottom-0 left-1/2 w-px bg-gray-600" />
      <div className={clsx('absolute top-0 bottom-0', v >= 0 ? 'bg-emerald-500' : 'bg-red-500')}
        style={v >= 0 ? { left: '50%', width: `${w}%` } : { right: '50%', width: `${w}%` }} />
    </div>
  )
}

function ScoreStrip({ scores, active, onPick }: { scores: NewsScores | null; active?: string; onPick: (c?: string) => void }) {
  if (!scores) return <div className="card h-24 animate-pulse" />
  const withNews = scores.coins.filter(c => c.stories || c.stories_6h)
  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <h3 className="text-sm font-semibold text-white">News score by coin</h3>
        <p className="text-[11px] text-gray-500">What goes into each coin's sentiment component now — importance × direction, fading with age. Click a coin to filter.
          {withNews.length > 0 && withNews.every(c => c.burst == null) && ' News bursts appear once 3 days of headlines give a baseline.'}</p>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-2">
        <button onClick={() => onPick(undefined)}
          className={clsx('text-left rounded-lg px-3 py-2 bg-gray-800/50 hover:bg-gray-800 space-y-1.5', !active && 'ring-1 ring-blue-500')}>
          <div className="flex justify-between text-xs"><span className="font-semibold text-gray-200">Market</span>
            <span className={clsx('tabular-nums', tone(scores.market.score, 0.02))}>{scores.market.score >= 0 ? '+' : ''}{scores.market.score.toFixed(2)}</span></div>
          <ScoreBar v={scores.market.score} />
          <div className="text-[10px] text-gray-500">{scores.market.stories} stories · every coin</div>
        </button>
        {withNews.map(c => (
          <button key={c.coin} onClick={() => onPick(active === c.coin ? undefined : c.coin)}
            className={clsx('text-left rounded-lg px-3 py-2 bg-gray-800/50 hover:bg-gray-800 space-y-1.5', active === c.coin && 'ring-1 ring-blue-500')}>
            <div className="flex justify-between items-center text-xs gap-1">
              <span className="font-semibold text-gray-200">{c.coin}</span>
              {c.burst != null && c.burst >= 2 && c.stories_6h >= 3 && (
                <span className="px-1 rounded bg-amber-900/50 text-amber-300 text-[9px] font-bold" title={`${c.stories_6h} stories in 6 h — ${c.burst}× its usual rate`}>BURST {c.burst.toFixed(1)}×</span>
              )}
              <span className={clsx('ml-auto tabular-nums', tone(c.score, 0.02))}>{c.score >= 0 ? '+' : ''}{c.score.toFixed(2)}</span>
            </div>
            <ScoreBar v={c.score} />
            <div className="text-[10px] text-gray-500">{c.stories} weighted · {c.stories_6h} in 6h</div>
          </button>
        ))}
      </div>
    </section>
  )
}

function Reactions({ item }: { item: NewsItem }) {
  if (!item.reactions.length) {
    return <p className="text-[11px] text-gray-500">Price reaction is measured 1 h, 4 h and 24 h after the app first saw it (checked every 30 min).</p>
  }
  const called = Math.abs(item.direction) >= 0.2 ? Math.sign(item.direction) : 0
  return (
    <table className="text-[11px] tabular-nums">
      <thead><tr className="text-gray-500"><th className="text-left pr-4 font-normal">Since first seen</th>
        <th className="pr-4 font-normal">1 h</th><th className="pr-4 font-normal">4 h</th><th className="font-normal">24 h</th></tr></thead>
      <tbody>
        {item.reactions.map(r => (
          <tr key={r.symbol}>
            <td className="pr-4 text-gray-300">{r.symbol.replace('USDT', '')}</td>
            {[r.r_1h, r.r_4h, r.r_24h].map((v, i) => (
              <td key={i} className={clsx('pr-4 text-center', v == null ? 'text-gray-600' : called ? tone(v * called, 0) : 'text-gray-300')}
                title={called ? 'green = price moved the way Claude called it' : 'no directional call'}>{v == null ? 'pending' : pct(v)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Row({ item, open, onToggle, held, focused }: { item: NewsItem; open: boolean; onToggle: () => void; held: Set<string>; focused?: boolean }) {
  const heldHit = item.coins.filter(c => held.has(c))
  const outlets = [...new Map(item.sources.map(s => [s.source, s])).values()]
  const ref = useRef<HTMLLIElement>(null)
  useEffect(() => { if (focused) ref.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }, [focused])
  return (
    <li ref={ref} className={clsx('border-b border-gray-800/80 last:border-0', item.scope === 'irrelevant' && 'opacity-60', focused && 'bg-blue-900/20 rounded')}>
      <div className="flex items-start gap-2 py-2.5 cursor-pointer hover:bg-gray-800/30 px-2 rounded" onClick={onToggle}>
        {open ? <ChevronDown className="w-4 h-4 mt-0.5 text-gray-500 shrink-0" /> : <ChevronRight className="w-4 h-4 mt-0.5 text-gray-500 shrink-0" />}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="text-sm text-gray-100 leading-snug">
            {heldHit.length > 0 && item.importance >= 4 && <AlertTriangle className="inline w-3.5 h-3.5 text-amber-400 mr-1 -mt-0.5" />}
            {item.title}
          </div>
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[11px] text-gray-500">
            <Importance n={item.importance} provisional={item.provisional} />
            <Direction d={item.direction} provisional={item.provisional} />
            <span className="px-1.5 rounded bg-gray-800 text-gray-300">{item.provisional ? 'waiting for Claude' : typeLabel(item.event_type)}</span>
            {item.coins.map(c => (
              <span key={c} className={clsx('px-1.5 rounded font-semibold', held.has(c) ? 'bg-blue-900/50 text-blue-300' : 'bg-gray-800 text-gray-200')}
                title={held.has(c) ? 'you hold this coin' : undefined}>{c}</span>
            ))}
            <span>{item.source}{outlets.length > 1 ? ` +${outlets.length - 1}` : ''}</span>
            {item.similar.length > 0 && (
              <span className="px-1.5 rounded bg-gray-800 text-gray-300" title="Other headlines about the same event — counted once in the score">
                +{item.similar.length} similar</span>
            )}
            <span title={`published ${stamp(item.published)} · first seen ${stamp(item.seen)}`}>{ago(item.published ?? item.seen)}</span>
            {item.weight > 0 && item.scope !== 'irrelevant' && <span title="Current weight in the score (importance × recency)">w {item.weight.toFixed(2)}</span>}
          </div>
        </div>
      </div>
      {open && (
        <div className="pl-8 pr-2 pb-3 space-y-2.5 text-xs">
          {item.note && <p className="text-gray-200"><span className="text-gray-500">Claude: </span>{item.note}</p>}
          {item.summary && <p className="text-gray-400 leading-relaxed">{item.summary}</p>}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px]">
            <div><div className="text-gray-500">Published</div><div className="text-gray-200">{stamp(item.published)}</div></div>
            <div><div className="text-gray-500">First seen here</div><div className="text-gray-200">{stamp(item.seen)}</div></div>
            <div><div className="text-gray-500">Scope</div><div className="text-gray-200">{item.scope}</div></div>
            <div><div className="text-gray-500">Matched coins</div><div className="text-gray-200">{item.mentions.join(', ') || '—'}</div></div>
          </div>
          <div className="space-y-1">
            <div className="text-[11px] text-gray-500">Carried by</div>
            <div className="flex flex-wrap gap-1.5">
              {outlets.map(o => o.url ? (
                <a key={o.source} href={o.url} target="_blank" rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 text-blue-300">
                  {o.source}{o.via !== o.source ? <span className="text-gray-500">via {o.via}</span> : null}<ExternalLink className="w-3 h-3" />
                </a>
              ) : <span key={o.source} className="px-2 py-0.5 rounded bg-gray-800 text-gray-300">{o.source}</span>)}
            </div>
          </div>
          {item.similar.length > 0 && (
            <div className="space-y-1">
              <div className="text-[11px] text-gray-500">Same story — {item.similar.length} more headline{item.similar.length > 1 ? 's' : ''} (the story counts once in the score)</div>
              <ul className="space-y-0.5">
                {item.similar.map(m => (
                  <li key={m.id} className="text-[11px] text-gray-300 flex gap-2">
                    <span className="text-gray-600 shrink-0 w-24">{ago(m.published ?? m.seen)}</span>
                    {m.url ? <a href={m.url} target="_blank" rel="noopener noreferrer" className="hover:underline hover:text-blue-300">{m.title}</a> : m.title}
                    <span className="text-gray-600 shrink-0">{m.source}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <Reactions item={item} />
        </div>
      )}
    </li>
  )
}

function StatCells({ s }: { s: ReactionStats }) {
  return (
    <>
      {(['r_1h', 'r_4h', 'r_24h'] as const).map(h => (
        <td key={h} className={clsx('px-2 py-1.5 text-right tabular-nums', tone(s[h].avg_signed_pct, 0))}
          title={`${s[h].n} measured · avg move either way ${pct(s[h].avg_abs_move_pct)}`}>
          {pct(s[h].avg_signed_pct)} <span className="text-gray-500">{s[h].hit_pct == null ? '' : `${s[h].hit_pct.toFixed(0)}%`}</span>
        </td>
      ))}
    </>
  )
}

function ResearchPanel({ r }: { r: NewsResearch | null }) {
  if (!r) return <div className="card h-32 animate-pulse" />
  const head = (
    <tr className="text-[10px] uppercase tracking-wider text-gray-500">
      <th className="px-2 py-1.5 text-left font-normal">Group</th><th className="px-2 py-1.5 text-right font-normal">Calls</th>
      <th className="px-2 py-1.5 text-right font-normal">1 h</th><th className="px-2 py-1.5 text-right font-normal">4 h</th><th className="px-2 py-1.5 text-right font-normal">24 h</th>
    </tr>
  )
  return (
    <section className="card space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-white">Does the news call the move?</h3>
        <p className="text-[11px] text-gray-500 mt-0.5">
          For every headline Claude gave a direction (|direction| ≥ {r.min_directional}): the average price move in the called
          direction — positive means right — and how often it was right. {r.note} Coin news is measured on that coin's perpetual,
          market and macro news on BTC. Needs a few days of headlines before it says much.
        </p>
      </div>
      {!r.overall.n ? <p className="text-xs text-gray-500">No labelled headlines yet — the first 1-hour reactions appear an hour after collection starts.</p> : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 overflow-x-auto">
          <table className="w-full text-xs"><thead>{head}</thead><tbody>
            <tr className="border-t border-gray-800 font-semibold"><td className="px-2 py-1.5 text-gray-100">All calls</td>
              <td className="px-2 py-1.5 text-right tabular-nums text-gray-300">{r.overall.n}</td><StatCells s={r.overall} /></tr>
            {r.by_event_type.map(g => (
              <tr key={g.event_type} className="border-t border-gray-800"><td className="px-2 py-1.5 text-gray-300">{typeLabel(g.event_type)}</td>
                <td className="px-2 py-1.5 text-right tabular-nums text-gray-400">{g.n}</td><StatCells s={g} /></tr>
            ))}
          </tbody></table>
          <table className="w-full text-xs self-start"><thead>{head}</thead><tbody>
            {r.by_importance.map(g => (
              <tr key={g.importance} className="border-t border-gray-800"><td className="px-2 py-1.5 text-gray-300"><Importance n={g.importance} /> importance {g.importance}</td>
                <td className="px-2 py-1.5 text-right tabular-nums text-gray-400">{g.n}</td><StatCells s={g} /></tr>
            ))}
          </tbody></table>
        </div>
      )}
    </section>
  )
}

function SourcesPanel({ status }: { status: NewsStatus }) {
  return (
    <section className="card space-y-2 overflow-x-auto">
      <h3 className="text-sm font-semibold text-white">Sources</h3>
      <table className="w-full text-xs">
        <thead><tr className="text-[10px] uppercase tracking-wider text-gray-500 text-left">
          <th className="py-1.5 pr-3 font-normal">Source</th><th className="py-1.5 pr-3 font-normal">Every</th><th className="py-1.5 pr-3 font-normal">Last OK</th>
          <th className="py-1.5 pr-3 font-normal">Newest item</th><th className="py-1.5 pr-3 font-normal text-right">Kept</th>
          <th className="py-1.5 pr-3 font-normal text-right" title="Items older than 48 h when first read">Too old</th><th className="py-1.5 font-normal">Problem</th>
        </tr></thead>
        <tbody>
          {status.sources.map(s => {
            const stale = s.newest && Date.now() / 1000 - s.newest > 3 * 86400
            return (
              <tr key={s.name} className="border-t border-gray-800">
                <td className="py-1.5 pr-3 text-gray-200">{s.name}{s.kind === 'google' ? <span className="text-gray-500"> · per coin</span> : null}</td>
                <td className="py-1.5 pr-3 text-gray-400">{Math.round(s.every_s / 60)} min</td>
                <td className="py-1.5 pr-3 text-gray-400">{ago(s.last_ok)}</td>
                <td className={clsx('py-1.5 pr-3', stale ? 'text-amber-300' : 'text-gray-400')}>{ago(s.newest)}{stale ? ' — feed looks stale' : ''}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums text-gray-300">{s.items_total}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums text-gray-500">{s.stale_dropped}</td>
                <td className="py-1.5 text-red-300 max-w-xs truncate" title={s.last_error ?? ''}>{s.last_error && (!s.last_ok || (s.last_poll ?? 0) > s.last_ok) ? s.last_error : ''}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </section>
  )
}

export function NewsPage() {
  const [filters, setFilters] = useState<NewsFilters>({ hours: 48, min_importance: 1, sort: 'newest', group: true })
  const [q, setQ] = useState('')
  const [items, setItems] = useState<NewsItem[] | null>(null)
  const [scores, setScores] = useState<NewsScores | null>(memo.scores ?? null)
  const [status, setStatus] = useState<NewsStatus | null>(memo.status ?? null)
  const [focus, setFocus] = useState<string | null>(null)
  const edition = useLatestEdition()
  const [research, setResearch] = useState<NewsResearch | null>(null)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [panel, setPanel] = useState<'feed' | 'research' | 'sources'>('feed')
  const req = useRef(0)

  const loadFeed = useCallback(async (f: NewsFilters) => {
    const token = ++req.current                      // a slow reply for older filters must not win
    const key = JSON.stringify(f)
    if (memo.items[key]) setItems(memo.items[key])   // show the last answer for these filters at once
    setLoading(true)
    try {
      const r = await api.news.feed(f)
      memo.items[key] = r.items
      if (token === req.current) { setItems(r.items); setError(null) }
    } catch (e) {
      if (token === req.current) setError(e instanceof Error ? e.message : 'Could not load news')
    } finally {
      if (token === req.current) setLoading(false)
    }
  }, [])
  const loadSide = useCallback(() => {
    api.news.scores().then(x => { memo.scores = x; setScores(x) }).catch(() => {})
    api.news.status().then(x => { memo.status = x; setStatus(x) }).catch(() => {})
  }, [])

  useEffect(() => {
    const f = { ...filters, q: q.trim() || undefined }
    const t = setTimeout(() => loadFeed(f), q ? 300 : 0)
    return () => clearTimeout(t)
  }, [filters, q, loadFeed])
  useEffect(() => {
    loadSide()
    const id = setInterval(() => { loadSide(); loadFeed({ ...filters, q: q.trim() || undefined }) }, REFRESH_MS)
    return () => clearInterval(id)
  }, [loadSide, loadFeed, filters, q])
  useEffect(() => { if (panel === 'research' && !research) api.news.research().then(setResearch).catch(() => {}) }, [panel, research])

  const held = useMemo(() => new Set(status?.held ?? []), [status])
  const alerts = (items ?? []).filter(i => !i.provisional && i.importance >= 4 && i.coins.some(c => held.has(c)) && i.age_h <= 24)
  const set = (patch: Partial<NewsFilters>) => setFilters(f => ({ ...f, ...patch }))
  const toggle = (id: string) => setOpen(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  // From App headlines / Important news: show that story in the feed, opened.
  const openStory = (story: string) => {
    setPanel('feed'); setQ(''); setFocus(story)
    setFilters(f => ({ ...f, coin: undefined, scope: undefined, min_importance: 1, hours: Math.max(f.hours ?? 48, 72), group: true }))
  }
  useEffect(() => {
    if (!focus || !items) return
    const hit = items.find(i => i.story === focus)
    if (hit) setOpen(s => new Set(s).add(hit.id))
  }, [focus, items])

  async function refreshNow() {
    setRefreshing(true)
    try { setStatus(await api.news.refresh()); loadSide(); await loadFeed({ ...filters, q: q.trim() || undefined }) }
    catch (e) { setError(e instanceof Error ? e.message : 'Refresh failed') }
    finally { setRefreshing(false) }
  }

  return (
    <div className="space-y-4">
      <section className="card flex flex-wrap items-center gap-x-5 gap-y-2">
        <div className="flex items-center gap-2">
          <Newspaper className="w-5 h-5 text-blue-400" />
          <div>
            <div className="text-sm font-semibold text-white">News</div>
            <div className="text-[11px] text-gray-500">
              {status ? `${status.sources.length} sources, each on its own schedule (2–15 min) · headlines older than ${status.max_age_h} h are dropped · weight halves every ${status.half_life_h} h` : 'Loading…'}
            </div>
          </div>
        </div>
        {status && (
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-400">
            <span>Last poll <span className="text-gray-200">{ago(status.last_tick)}</span></span>
            <span>Claude today <span className="text-gray-200 tabular-nums">{status.claude_calls_today}/{status.claude_daily_calls}</span> calls</span>
            <span>Waiting for Claude <span className={clsx('tabular-nums', status.pending > 50 ? 'text-amber-300' : 'text-gray-200')}>{status.pending}</span></span>
          </div>
        )}
        <button onClick={refreshNow} disabled={refreshing}
          className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 disabled:opacity-50">
          <RefreshCw className={clsx('w-3.5 h-3.5', refreshing && 'animate-spin')} /> {refreshing ? 'Polling…' : 'Poll now'}
        </button>
      </section>

      {alerts.length > 0 && (
        <section className="rounded-lg border border-amber-700/60 bg-amber-900/20 px-4 py-3 space-y-1.5">
          <div className="flex items-center gap-2 text-sm font-semibold text-amber-300"><AlertTriangle className="w-4 h-4" /> Important news on coins you hold</div>
          {alerts.slice(0, 5).map(a => (
            <button key={a.id} onClick={() => { setPanel('feed'); setOpen(s => new Set(s).add(a.id)) }} className="block text-left text-xs text-amber-100 hover:underline">
              <span className="font-semibold">{a.coins.filter(c => held.has(c)).join(', ')}</span> · <span className={tone(a.direction, 0.19)}>{a.direction > 0 ? '+' : ''}{a.direction.toFixed(1)}</span> · {a.title} <span className="text-amber-200/60">({ago(a.published ?? a.seen)})</span>
            </button>
          ))}
        </section>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)] gap-4 items-start">
        <EditionCard onOpenStory={openStory} />
        <MoodCard edition={edition} />
      </div>

      <ImportantNews onOpenStory={openStory} />

      <ScoreStrip scores={scores} active={filters.coin} onPick={c => { set({ coin: c }); setPanel('feed') }} />

      <div className="flex gap-4 border-b border-gray-800 text-sm">
        {([['feed', 'Headlines'], ['research', 'Price reactions'], ['sources', 'Sources']] as const).map(([k, label]) => (
          <button key={k} onClick={() => setPanel(k)}
            className={clsx('pb-2 border-b-2 -mb-px font-semibold', panel === k ? 'border-blue-500 text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-300')}>{label}</button>
        ))}
      </div>

      {panel === 'research' && <ResearchPanel r={research} />}
      {panel === 'sources' && (status ? <SourcesPanel status={status} /> : <div className="card h-40 animate-pulse" />)}

      {panel === 'feed' && (
        <section className="card space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <div className="flex flex-wrap gap-1">
              {SCOPES.map(([k, label]) => (
                <button key={label} onClick={() => set({ scope: (k || undefined) as NewsFilters['scope'] })}
                  className={clsx('px-2.5 py-1 rounded-md', (filters.scope ?? '') === k ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-300 hover:bg-gray-700')}>{label}</button>
              ))}
            </div>
            <div className="flex items-center gap-1" title="Minimum importance">
              <span className="text-gray-500">Importance ≥</span>
              {[1, 2, 3, 4, 5].map(n => (
                <button key={n} onClick={() => set({ min_importance: n })}
                  className={clsx('w-6 py-1 rounded-md', filters.min_importance === n ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-300 hover:bg-gray-700')}>{n}</button>
              ))}
            </div>
            <div className="flex gap-1">
              {WINDOWS.map(([h, label]) => (
                <button key={h} onClick={() => set({ hours: h })}
                  className={clsx('px-2 py-1 rounded-md', filters.hours === h ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-300 hover:bg-gray-700')}>{label}</button>
              ))}
            </div>
            <label className="flex items-center gap-1 text-gray-500" title={SORTS.find(x => x[0] === filters.sort)?.[2]}>
              Sort
              <select value={filters.sort} onChange={e => set({ sort: e.target.value as NewsSort })}
                className="bg-gray-800 text-gray-200 rounded-md px-1.5 py-1 outline-none">
                {SORTS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-1 text-gray-400 cursor-pointer" title="One row per story; other headlines about the same event go under it">
              <input type="checkbox" checked={filters.group ?? true} onChange={e => set({ group: e.target.checked })} className="accent-blue-500" />
              Group similar
            </label>
            {filters.coin && (
              <button onClick={() => set({ coin: undefined })} className="px-2 py-1 rounded-md bg-blue-900/50 text-blue-200">{filters.coin} ✕</button>
            )}
            <label className="ml-auto flex items-center gap-1.5 bg-gray-800 rounded-md px-2 py-1">
              <Search className="w-3.5 h-3.5 text-gray-500" />
              <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search headlines"
                className="bg-transparent outline-none w-40 text-gray-200 placeholder:text-gray-600" />
            </label>
          </div>
          <div className={clsx('h-0.5 rounded-full overflow-hidden', loading ? 'bg-gray-800' : 'bg-transparent')}>
            {loading && <div className="h-full w-1/3 bg-blue-500 animate-pulse" />}
          </div>
          {error && <p className="text-xs text-red-400">{error}</p>}
          {items === null ? <div className="h-40 animate-pulse bg-gray-800/40 rounded" /> : !items.length ? (
            <p className="text-sm text-gray-500 py-8 text-center">No headlines match. Collection runs every minute — widen the window or lower the importance filter.</p>
          ) : (
            <ul>{items.map(i => <Row key={i.id} item={i} open={open.has(i.id)} onToggle={() => toggle(i.id)} held={held}
                                    focused={focus != null && i.story === focus} />)}</ul>
          )}
        </section>
      )}
    </div>
  )
}
