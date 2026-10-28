/**
 * Evenly spaced ticks for a numeric (time) x-axis. Letting recharts derive ticks for a
 * time-scaled numeric axis can emit one label per data point, which renders as an
 * unreadable smear; explicit ticks keep it to a handful.
 */
export function evenTicks(values: number[], count = 5): number[] {
  const finite = values.filter(Number.isFinite)
  if (finite.length < 2) return finite
  const min = Math.min(...finite)
  const max = Math.max(...finite)
  if (max === min) return [min]
  const step = (max - min) / (count - 1)
  return Array.from({ length: count }, (_, i) => Math.round(min + i * step))
}
