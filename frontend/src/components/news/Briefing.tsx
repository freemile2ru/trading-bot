import { useCallback, useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, ExternalLink, History, RefreshCw, Sparkles } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../utils/api'
import type { Edition, EditionHeadline, EditionSummary, Mood, MoodComponent, StoryBrief } from '../../types/news'

// Last answers survive tab switches: the page renders them at once and refreshes behind them.
const memo: { edition?: Edition; mood?: Mood; important?: { stories: StoryBrief[]; held: string[] } } = {}

const ago = (ts: number | null | undefined) => {
  if (!ts) return '—'
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60))
  return m < 60 ? `${m}m ago` : m < 48 * 60 ? `${Math.floor(m / 60)}h ${m % 60}m ago` : `${Math.floor(m / 1440)}d ago`
}
const clock = (ts?: number) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '')
const signed = (v: number | null | undefined, d = 2) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}`)
const tone = (v: number | null | undefined, dead = 0.05) =>
  v == null || Math.abs(v) <= dead ? 'text-gray-300' : v > 0 ? 'text-emerald-400' : 'text-red-400'

const STANCE: Record<EditionHeadline['stance'], string> = {
  bullish: 'bg-emerald-900/50 text-emerald-300', bearish: 'bg-red-900/50 text-red-300',
  neutral: 'bg-gray-800 text-gray-300', caution: 'bg-amber-900/50 text-amber-300',
}
const RELEVANCE: Record<EditionHeadline['relevance'], string> = {
  position: 'your position', signal: 'live signal', watchlist: 'watchlist', market: 'market', macro: 'macro',
}
const TRIGGER: Record<string, string> = {
  first: 'first edition', scheduled: 'scheduled — the picture changed', important: 'important news arrived', manual: 'written on request',
}
const LABEL_TONE = (label?: string | null) =>
  !label ? 'text-gray-300' : label.includes('bull') || label === 'Risk-on' ? 'text-emerald-400'
    : label.includes('bear') || label === 'Risk-off' ? 'text-red-400' : 'text-gray-200'

/** Diverging −1…+1 bar. */
function Gauge({ v, tall }: { v: number | null; tall?: boolean }) {
  const w = v == null ? 0 : Math.min(50, Math.abs(v) * 50)
  return (
    <div className={clsx('relative w-full bg-gray-800 rounded-full overflow-hidden', tall ? 'h-2.5' : 'h-1.5')}>
      <div className="absolute top-0 bottom-0 left-1/2 w-px bg-gray-500" />
      {v != null && <div className={clsx('absolute top-0 bottom-0', v >= 0 ? 'bg-emerald-500' : 'bg-red-500')}
        style={v >= 0 ? { left: '50%', width: `${w}%` } : { right: '50%', width: `${w}%` }} />}
    </div>
  )
}

export function EditionCard({ onOpenStory }: { onOpenStory: (story: string) => void }) {
  const [ed, setEd] = useState<Edition | null>(memo.edition ?? null)
  const [viewing, setViewing] = useState<number | null>(null)          // null = follow the latest
  const [timeline, setTimeline] = useState<EditionSummary[] | null>(null)
  const [budget, setBudget] = useState<{ used: number; total: number } | null>(null)
  const [showTimeline, setShowTimeline] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async (id: number | null) => {
    try {
      const e = await api.news.edition(id)
      setEd(e)
      if (id == null) memo.edition = e
      setErr(null)
    } catch (e) { setErr(e instanceof Error ? e.message : 'Could not load the headlines') }
  }, [])
  const loadTimeline = useCallback(() => api.news.editions()
    .then(r => { setTimeline(r.editions); setBudget({ used: r.calls_today, total: r.daily_calls }) }).catch(() => {}), [])

  useEffect(() => { load(viewing) }, [viewing, load])
  useEffect(() => {
    loadTimeline()
    // Following the latest: pick up new editions as they're written. An old edition stays put.
    const id = setInterval(() => { if (viewing == null) load(null); loadTimeline() }, 60_000)
    return () => clearInterval(id)
  }, [viewing, load, loadTimeline])

  async function writeNow() {
    setBusy(true); setErr(null)
    try { const e = await api.news.newEdition(); memo.edition = e; setViewing(null); setEd(e); loadTimeline() }
    catch (e) { setErr(e instanceof Error ? e.message : 'Could not write an edition') }
    finally { setBusy(false) }
  }
  const go = (id: number | null) => { setShowTimeline(false); setViewing(id) }
  const behind = ed && !ed.latest && timeline ? timeline.findIndex(t => t.id === ed.id) : -1

  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Sparkles className="w-4 h-4 text-blue-400" />
        <h3 className="text-sm font-semibold text-white">App headlines</h3>
        <span className="text-[11px] text-gray-500">
          {ed?.id ? <>Edition #{ed.id} · {clock(ed.ts)} ({ago(ed.ts)}) · {TRIGGER[ed.trigger ?? ''] ?? ed.trigger}</> : 'Relative to trading — written by Claude from everything below'}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <button onClick={() => ed?.prev_id && go(ed.prev_id)} disabled={!ed?.prev_id} title="Previous edition"
            className="p-1.5 rounded bg-gray-800 hover:bg-gray-700 disabled:opacity-30"><ChevronLeft className="w-4 h-4" /></button>
          <button onClick={() => ed?.next_id && go(ed.next_id)} disabled={!ed?.next_id} title="Next edition"
            className="p-1.5 rounded bg-gray-800 hover:bg-gray-700 disabled:opacity-30"><ChevronRight className="w-4 h-4" /></button>
          <button onClick={() => setShowTimeline(s => !s)} title="All editions"
            className={clsx('p-1.5 rounded hover:bg-gray-700', showTimeline ? 'bg-gray-700' : 'bg-gray-800')}><History className="w-4 h-4" /></button>
          <button onClick={writeNow} disabled={busy} title="Ask Claude for a fresh edition now"
            className="flex items-center gap-1 px-2 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-xs disabled:opacity-50">
            <RefreshCw className={clsx('w-3.5 h-3.5', busy && 'animate-spin')} /> {busy ? 'Writing…' : 'New edition'}
          </button>
        </div>
      </div>

      {ed && !ed.latest && (
        <div className="flex items-center gap-2 text-xs rounded-md bg-amber-900/20 border border-amber-800/50 px-3 py-1.5 text-amber-200">
          Viewing an earlier edition{behind > 0 ? ` (${behind} newer)` : ''}.
          <button onClick={() => go(null)} className="ml-auto font-semibold hover:underline">Back to latest</button>
        </div>
      )}
      {err && <p className="text-xs text-red-400">{err}</p>}
      {ed && !ed.latest && ed.verdict && (
        <div className="rounded-md bg-gray-800/50 px-3 py-2 text-xs">
          <span className="text-gray-500">Verdict at the time: </span>
          <span className={clsx('font-semibold', LABEL_TONE(ed.verdict.label))}>{ed.verdict.label}</span>
          {ed.mood && <span className="text-gray-500"> (gauge {signed(ed.mood.score)})</span>}
          <p className="text-gray-300 mt-0.5">{ed.verdict.summary}</p>
        </div>
      )}

      {showTimeline && timeline && (
        <ol className="max-h-64 overflow-y-auto divide-y divide-gray-800 rounded-md bg-gray-900/60 border border-gray-800 text-xs">
          {timeline.map(t => (
            <li key={t.id}>
              <button onClick={() => go(t.id === timeline[0]?.id ? null : t.id)}
                className={clsx('w-full text-left px-3 py-1.5 hover:bg-gray-800 flex gap-3', ed?.id === t.id && 'bg-gray-800')}>
                <span className="text-gray-500 shrink-0 w-28">{clock(t.ts)}</span>
                <span className={clsx('shrink-0 w-28', LABEL_TONE(t.label))}>{t.label}</span>
                <span className="text-gray-300 truncate">{t.lead}</span>
              </button>
            </li>
          ))}
          {!timeline.length && <li className="px-3 py-2 text-gray-500">No editions yet.</li>}
        </ol>
      )}

      {!ed ? <div className="h-32 animate-pulse bg-gray-800/40 rounded" /> : !ed.headlines.length ? (
        <p className="text-sm text-gray-500 py-4">The first edition is written once headlines have been classified — usually within a few minutes of start-up.</p>
      ) : (
        <ol className="space-y-2.5">
          {ed.headlines.map((h, i) => (
            <li key={i} className={clsx('rounded-lg px-3 py-2.5', i === 0 ? 'bg-gray-800/70' : 'bg-gray-800/30')}>
              <div className="flex flex-wrap items-center gap-2">
                <span className={clsx('px-1.5 py-0.5 rounded text-[10px] font-bold uppercase', STANCE[h.stance])}>{h.stance}</span>
                <span className="text-[10px] uppercase tracking-wide text-gray-500">{RELEVANCE[h.relevance]}</span>
                {h.coins.map(c => <span key={c} className="px-1.5 rounded bg-gray-900 text-[11px] font-semibold text-gray-200">{c}</span>)}
              </div>
              <div className={clsx('mt-1 font-semibold text-white', i === 0 ? 'text-base' : 'text-sm')}>{h.title}</div>
              {h.body && <p className="text-xs text-gray-300 mt-0.5">{h.body}</p>}
              {!!h.story_briefs?.length && (
                <div className="flex flex-wrap gap-1.5 mt-1.5">
                  {h.story_briefs.map(s => (
                    <button key={s.story} onClick={() => onOpenStory(s.story)} title="Show in the headlines below"
                      className="text-[11px] text-blue-300 hover:underline truncate max-w-[22rem]">↳ {s.title}</button>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
      {budget && <p className="text-[10px] text-gray-600">A new edition is written when the picture changes (checked every minute, at least 30 min apart unless important news lands). Claude today: {budget.used}/{budget.total}.</p>}
    </section>
  )
}

const PART_LABEL: Record<string, string> = { news: 'News', claude: "Claude's calls", regime: 'BTC regime', fa: 'FA score' }

function Part({ name, c }: { name: string; c: MoodComponent }) {
  const scored = c.weight > 0
  return (
    <div className={clsx('space-y-1', !scored && 'opacity-60')}>
      <div className="flex justify-between text-xs">
        <span className="text-gray-300">{PART_LABEL[name] ?? name} <span className="text-gray-600">· {scored ? `${Math.round(c.weight * 100)}%` : 'context, not scored'}</span></span>
        <span className={clsx('tabular-nums', tone(c.score))}>{signed(c.score)}</span>
      </div>
      <Gauge v={c.score} />
      <div className="text-[10px] text-gray-500">{c.detail}</div>
    </div>
  )
}

export function MoodCard({ edition }: { edition?: Edition | null }) {
  const [m, setM] = useState<Mood | null>(memo.mood ?? null)
  useEffect(() => {
    const load = () => api.news.mood().then(x => { memo.mood = x; setM(x) }).catch(() => {})
    load()
    const id = setInterval(load, 60_000)
    return () => clearInterval(id)
  }, [])
  if (!m) return <div className="card h-64 animate-pulse" />
  const verdict = edition?.verdict ?? m.verdict
  const swing = m.components.claude?.swing_view
  return (
    <section className="card space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-white">How are we feeling?</h3>
        <p className="text-[11px] text-gray-500">News, Claude's calls and the BTC regime, weighted — recomputed every minute. FA is shown for context, not scored.</p>
      </div>
      <div className="space-y-1.5">
        <div className="flex items-baseline justify-between">
          <span className={clsx('text-xl font-bold', LABEL_TONE(m.label))}>{m.label}</span>
          <span className={clsx('text-lg tabular-nums font-semibold', tone(m.score))}>{signed(m.score)}</span>
        </div>
        <Gauge v={m.score} tall />
        <div className="flex justify-between text-[10px] text-gray-500"><span>Risk-off</span><span>{m.agreement}</span><span>Risk-on</span></div>
      </div>
      <div className="space-y-2.5">
        {(['news', 'claude', 'regime', 'fa'] as const).map(k => m.components[k] && <Part key={k} name={k} c={m.components[k]} />)}
      </div>
      {m.fear_greed && <p className="text-[11px] text-gray-500">Fear & Greed {m.fear_greed.value} ({m.fear_greed.classification}) — shown, not scored.</p>}
      {verdict && (
        <div className="rounded-lg bg-gray-800/60 px-3 py-2 space-y-1">
          <div className="text-[11px] text-gray-500">Claude's verdict{edition?.ts ? ` · ${ago(edition.ts)}` : ''}:
            <span className={clsx('ml-1 font-semibold', LABEL_TONE(verdict.label))}>{verdict.label}</span>
            {verdict.label !== m.label && <span className="text-amber-300"> (differs from the gauge)</span>}
          </div>
          <p className="text-xs text-gray-200">{verdict.summary}</p>
          {verdict.watch && <p className="text-[11px] text-gray-400"><span className="text-gray-500">What would change it: </span>{verdict.watch}</p>}
        </div>
      )}
      {swing?.text && (
        <p className="text-[11px] text-gray-400"><span className="text-gray-500">Claude swing's market view ({ago(swing.ts)}): </span>“{swing.text}”</p>
      )}
    </section>
  )
}

export function ImportantNews({ onOpenStory }: { onOpenStory: (story: string) => void }) {
  const [d, setD] = useState(memo.important ?? null)
  const [hours, setHours] = useState(24)
  useEffect(() => {
    const load = () => api.news.important(hours).then(x => { if (hours === 24) memo.important = x; setD(x) }).catch(() => {})
    load()
    const id = setInterval(load, 60_000)
    return () => clearInterval(id)
  }, [hours])
  const held = new Set(d?.held ?? [])
  return (
    <section className="card space-y-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-white">Important news</h3>
        <span className="text-[11px] text-gray-500">The biggest distinct stories — each event once, however many outlets ran it.</span>
        <div className="ml-auto flex gap-1 text-xs">
          {[6, 24, 72].map(h => (
            <button key={h} onClick={() => setHours(h)}
              className={clsx('px-2 py-0.5 rounded', hours === h ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-300')}>{h}h</button>
          ))}
        </div>
      </div>
      {!d ? <div className="h-24 animate-pulse bg-gray-800/40 rounded" /> : !d.stories.length ? (
        <p className="text-xs text-gray-500 py-3">Nothing of importance 3 or more in the last {hours} h.</p>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
          {d.stories.map(s => {
            const mine = s.coins.filter(c => held.has(c))
            return (
              <div key={s.story} className={clsx('rounded-lg px-3 py-2 bg-gray-800/40 space-y-1', mine.length && 'ring-1 ring-amber-600/60')}>
                <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
                  <span className={clsx('px-1.5 py-0.5 rounded font-bold', s.importance >= 4 ? 'bg-amber-900/60 text-amber-300' : 'bg-gray-800 text-gray-300')}>
                    {s.importance}/5</span>
                  <span className={clsx('font-semibold', tone(s.direction, 0.19))}>{signed(s.direction, 1)}</span>
                  <span className="text-gray-400">{(s.event_type ?? '').replace(/_/g, ' ')}</span>
                  {s.coins.map(c => <span key={c} className={clsx('px-1 rounded font-semibold', held.has(c) ? 'bg-blue-900/60 text-blue-200' : 'bg-gray-900 text-gray-200')}>{c}</span>)}
                  <span className="ml-auto text-gray-500">{ago(Date.now() / 1000 - s.age_h * 3600)}{s.similar ? ` · ${s.similar + 1} headlines` : ''}{s.outlets > 1 ? ` · ${s.outlets} outlets` : ''}</span>
                </div>
                <button onClick={() => onOpenStory(s.story)} className="text-left text-sm text-gray-100 hover:underline">{s.title}</button>
                {s.note && <p className="text-[11px] text-gray-400">{s.note}</p>}
                {s.url && <a href={s.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[11px] text-blue-300 hover:underline">
                  {s.source} <ExternalLink className="w-3 h-3" /></a>}
                {mine.length > 0 && <p className="text-[11px] text-amber-300">You hold {mine.join(', ')}.</p>}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

/** Shared by the page so the mood card can show the verdict of whichever edition is on screen. */
export function useLatestEdition() {
  const [ed, setEd] = useState<Edition | null>(memo.edition ?? null)
  useEffect(() => {
    const load = () => api.news.edition().then(e => { memo.edition = e; setEd(e) }).catch(() => {})
    load()
    const id = setInterval(load, 60_000)
    return () => clearInterval(id)
  }, [])
  return ed
}
