import { useMemo, useState, type ReactNode } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import clsx from 'clsx'

/* Column sorting shared by every table in the app.

   useSort(rows, accessors)   client-side: pick the sort with toggle(key); nulls always sort last.
   <SortHeader …/>            the header cell: click to sort, click again to flip; shows the arrow. */

export type SortDir = 'asc' | 'desc'
export interface SortState { key: string; dir: SortDir }
export type SortValue = number | string | boolean | null | undefined
export type Accessors<T> = Record<string, (row: T) => SortValue>

function compare(a: SortValue, b: SortValue): number {
  if (a == null && b == null) return 0
  if (a == null) return 1                 // nulls last whatever the direction
  if (b == null) return -1
  if (typeof a === 'number' && typeof b === 'number') return a - b
  if (typeof a === 'boolean' && typeof b === 'boolean') return Number(a) - Number(b)
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' })
}

export function sortRows<T>(rows: T[], accessors: Accessors<T>, sort: SortState | null): T[] {
  if (!sort || !accessors[sort.key]) return rows
  const get = accessors[sort.key]
  const sign = sort.dir === 'desc' ? -1 : 1
  return rows
    .map((row, i) => ({ row, i, v: get(row) }))
    .sort((x, y) => {
      const c = compare(x.v, y.v)
      // Nulls stay last in both directions; ties keep their original order.
      if (x.v == null || y.v == null) return c || x.i - y.i
      return c * sign || x.i - y.i
    })
    .map(x => x.row)
}

/** Client-side sort state for one table. `initial` is the default order; `numericFirst` keys open descending. */
export function useSort<T>(rows: T[], accessors: Accessors<T>, initial: SortState | null = null) {
  const [sort, setSort] = useState<SortState | null>(initial)
  const toggle = (key: string) => setSort(s => {
    if (s?.key === key) return { key, dir: s.dir === 'desc' ? 'asc' : 'desc' }
    // Numbers open with the largest first, text with A first.
    const sample = rows.map(accessors[key]).find(v => v != null)
    return { key, dir: typeof sample === 'number' ? 'desc' : 'asc' }
  })
  const sorted = useMemo(() => sortRows(rows, accessors, sort), [rows, accessors, sort])  // eslint-disable-line react-hooks/exhaustive-deps
  return { rows: sorted, sort, toggle, setSort }
}

/** A sortable header label. Works for client-side (useSort) and server-side (page reload) sorting alike. */
export function SortHeader({ k, sort, onSort, children, className, title, align = 'left' }: {
  k: string; sort: SortState | null; onSort: (key: string) => void; children: ReactNode
  className?: string; title?: string; align?: 'left' | 'right' | 'center'
}) {
  const active = sort?.key === k
  const Icon = !active ? ArrowUpDown : sort!.dir === 'desc' ? ArrowDown : ArrowUp
  return (
    <button type="button" onClick={() => onSort(k)} title={title ?? 'Sort by this column'}
      aria-sort={active ? (sort!.dir === 'desc' ? 'descending' : 'ascending') : 'none'}
      className={clsx('group/sort inline-flex items-center gap-1 whitespace-nowrap hover:text-gray-200 transition-colors',
        align === 'right' && 'flex-row-reverse', active && 'text-gray-200', className)}>
      {children}
      <Icon className={clsx('w-3 h-3 shrink-0', active ? 'opacity-100' : 'opacity-0 group-hover/sort:opacity-60')} />
    </button>
  )
}
