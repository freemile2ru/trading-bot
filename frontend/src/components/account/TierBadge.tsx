import clsx from 'clsx'
import { TIER_CLS } from '../../utils/pricing'
import type { TierId } from '../../types/auth'

const LABEL: Record<TierId, string> = { free: 'Free', pro: 'Pro', elite: 'Elite' }

/** The tier pill used in the header menu, the pricing cards and the admin table: free gray, pro blue, elite amber. */
export function TierBadge({ tier, className }: { tier: TierId | string | null | undefined; className?: string }) {
  const t = (tier && tier in TIER_CLS ? tier : 'free') as TierId
  return (
    <span className={clsx('inline-flex items-center px-1.5 py-0.5 rounded border text-[10px] font-bold uppercase tracking-wide', TIER_CLS[t], className)}>
      {tier && tier in LABEL ? LABEL[t] : tier ?? '—'}
    </span>
  )
}
