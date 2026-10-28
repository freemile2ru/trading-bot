export interface ScalperEvidence {
  paper: { trades: number; win_rate: number; gross: number; fees: number; net: number; gross_bps: number; cost_bps: number
           net_bps: number; avg_mfe_bps: number; avg_mae_bps: number; won_before_costs: number }
  live: ScalperEvidence['paper'] | null
  by_exit: { exit: string; trades: number; net: number; win_rate: number }[]
  curve: { ts: number; pnl: number }[]
  signal_forward: { horizon_s: number; set: 'entered' | 'all candidates'; n: number; avg_bps: number; t: number | null; share_up_pct: number }[]
  signals_logged: number; round_trip_bps?: number
  period: [number, number] | null
}

export interface FailureScore {
  score: number
  performance: number
  certainty: number
  partial: boolean
  grade: 'severe' | 'clear failure' | 'mild' | 'harmless'
}

export interface ScoreInputs {
  result_r: number | null
  result_note?: string
  t: number | null
  t_note?: string
  n: number | null
  n_note?: string
  lost_pct: number
}

/** A rejected study from the Lab's research shelf (same shape as the Lab shows it). */
export interface RejectedResearch {
  key: string; title: string; script: string; file: string | null; question: string; finding: string; decision: string
  book: string | null; ran_at: number | null; status: 'promoted' | 'rejected' | 'mixed' | 'pending'
}

export interface RetiredExperiment {
  key: string
  rank: number
  score: FailureScore
  score_inputs: ScoreInputs
  title: string
  category: string
  active: string
  retired: string
  hypothesis: string
  method: string
  numbers: { label: string; value: string; note: string }[]
  why: string[]
  kept: string
  removed: string
  evidence: string
  data?: ScalperEvidence | null
}
