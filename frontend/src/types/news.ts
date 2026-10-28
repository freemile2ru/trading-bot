export type NewsScope = 'coin' | 'market' | 'macro' | 'irrelevant'

export interface NewsOutlet { source: string; via: string; url: string | null; published: number | null }
export interface NewsReaction { symbol: string; r_1h: number | null; r_4h: number | null; r_24h: number | null }

export interface NewsItem {
  id: string
  title: string
  url: string | null
  source: string
  summary: string
  published: number | null
  seen: number
  event_type: string | null
  note: string | null
  sources: NewsOutlet[]
  mentions: string[]
  coins: string[]
  scope: NewsScope
  direction: number
  importance: number
  provisional: boolean
  weight: number
  age_h: number
  reactions: NewsReaction[]
  story: string
  similar: SimilarHeadline[]
}

export interface NewsBrief {
  id: string; title: string; url: string | null; source: string; age_h: number; event_type: string | null
  direction: number; importance: number; note: string | null; provisional: boolean; weight: number; outlets: number
}

export interface CoinNewsScore {
  coin: string; score: number; coin_score: number; stories: number
  /** null until collection has run 3 days (no baseline to compare with yet) */
  burst: number | null; stories_6h: number; top: NewsBrief[]
}

export interface NewsScores {
  market: { score: number; stories: number; top: NewsBrief[] }
  coins: CoinNewsScore[]
  computed_at: number
}

export interface NewsSourceHealth {
  name: string; kind: string; every_s: number; last_poll: number | null; last_ok: number | null
  last_error: string | null; items_total: number; new_last: number; stale_dropped: number; newest: number | null
}

export interface NewsStatus {
  enabled: boolean; tick_s: number; last_tick: number; last_new: number; pending: number
  claude_calls_today: number; claude_daily_calls: number; half_life_h: number; max_age_h: number
  sources: NewsSourceHealth[]; held: string[]
}

export interface ReactionStats {
  n: number
  r_1h: HorizonStats; r_4h: HorizonStats; r_24h: HorizonStats
}
export interface HorizonStats { n: number; avg_signed_pct: number | null; hit_pct: number | null; avg_abs_move_pct: number | null }

export interface NewsResearch {
  overall: ReactionStats
  by_event_type: (ReactionStats & { event_type: string })[]
  by_importance: (ReactionStats & { importance: number })[]
  min_directional: number
  note: string
}

export interface NewsFilters {
  coin?: string; scope?: NewsScope | 'pending'; min_importance?: number; q?: string; hours?: number; include_irrelevant?: boolean
  sort?: NewsSort; group?: boolean
}

export type NewsSort = 'newest' | 'importance' | 'impact' | 'weight' | 'coverage' | 'reaction'

export interface SimilarHeadline {
  id: string; title: string; url: string | null; source: string; published: number | null; seen: number
  importance: number; direction: number
}

export interface StoryBrief extends NewsBrief {
  story: string; coins: string[]; scope: NewsScope; similar: number; published: number | null; seen: number
}

export interface MoodComponent {
  score: number | null; weight: number; detail: string
  swing_view?: { text: string; ts: number } | null
  latest?: { symbol: string; action: string; confidence: number | null; fired_at: number }[]
}

export interface Verdict { label: string; summary: string; watch: string }

export interface Mood {
  score: number; label: string; agreement: string
  components: Record<'news' | 'claude' | 'regime' | 'fa', MoodComponent>
  fear_greed: { value: number; classification: string } | null
  computed_at: number
  verdict: Verdict | null; verdict_at: number | null
}

export interface EditionHeadline {
  title: string; body: string; stance: 'bullish' | 'bearish' | 'neutral' | 'caution'
  relevance: 'position' | 'signal' | 'watchlist' | 'market' | 'macro'; coins: string[]; stories: string[]
  story_briefs?: { story: string; id: string; title: string; url: string | null; importance: number; direction: number; event_type: string | null }[]
}

export interface Edition {
  id: number | null; ts?: number; trigger?: string
  mood?: { score: number; label: string; agreement: string } | null
  verdict: Verdict | null; headlines: EditionHeadline[]
  prev_id: number | null; next_id: number | null; latest: boolean
}

export interface EditionSummary { id: number; ts: number; trigger: string; label: string | null; lead: string | null }
