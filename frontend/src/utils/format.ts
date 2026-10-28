/** Shared number formatting — one convention across every tab. */

const MINUS = '−'

/** +$1,234.56 / −$12.65 (sign always shown, proper minus sign). */
export function fmtUsdSigned(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return '—'
  const abs = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
  if (Number(abs.replace(/,/g, '')) === 0) return `$${abs}`
  return `${n > 0 ? '+' : MINUS}$${abs}`
}

/** $1,234.56 (no sign; for balances and amounts that are never negative). */
export function fmtUsd(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return '—'
  return `${n < 0 ? MINUS : ''}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
}

/** +1.23% / −0.40% — and 0.00% rather than "+0%" or "-0%". */
export function fmtPctSigned(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return '—'
  const abs = Math.abs(n).toFixed(digits)
  if (Number(abs) === 0) return `${(0).toFixed(digits)}%`
  return `${n > 0 ? '+' : MINUS}${abs}%`
}
