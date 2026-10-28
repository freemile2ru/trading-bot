import { useMemo, useState, type ReactNode } from 'react'
import clsx from 'clsx'
import { SortHeader, sortRows, type Accessors, type SortState, type SortValue } from './Sortable'

export interface Column<T> {
  key: string
  header: ReactNode
  align?: 'left' | 'right' | 'center'
  /** Tailwind width class, e.g. "w-24" — keeps columns from drifting between tables. */
  width?: string
  title?: string
  /** Client-side sorting: how to read this column's value. */
  sortValue?: (row: T) => SortValue
  /** Server-side sorting: the key sent back through `onSort` (the API sorts the page). */
  sortKey?: string
  render: (row: T, index: number) => ReactNode
}

/**
 * One table style for the whole app: quiet uppercase headers, right-aligned tabular
 * numbers, row hover, and a real empty state. Wraps horizontally on narrow screens.
 *
 * Sorting: a column with `sortValue` sorts in the browser; with `sort`/`onSort` given, a
 * column with `sortKey` asks the caller (the API) to sort instead.
 */
export function DataTable<T>({
  columns, rows, rowKey, empty = 'Nothing here yet.', dense = false, onRowClick, footer, sort, onSort,
}: {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T, index: number) => string
  empty?: ReactNode
  dense?: boolean
  onRowClick?: (row: T) => void
  footer?: ReactNode
  sort?: SortState | null
  onSort?: (key: string) => void
}) {
  const controlled = !!onSort
  const [local, setLocal] = useState<SortState | null>(null)
  const accessors = useMemo<Accessors<T>>(
    () => Object.fromEntries(columns.filter(c => c.sortValue).map(c => [c.key, c.sortValue!])),
    [columns],
  )
  const shown = useMemo(() => (controlled ? rows : sortRows(rows, accessors, local)), [controlled, rows, accessors, local])
  const toggle = (key: string) => setLocal(s => {
    if (s?.key === key) return { key, dir: s.dir === 'desc' ? 'asc' : 'desc' }
    const sample = rows.map(accessors[key]).find(v => v != null)
    return { key, dir: typeof sample === 'number' ? 'desc' : 'asc' }
  })
  const state = controlled ? (sort ?? null) : local

  const pad = dense ? 'px-3 py-1.5' : 'px-3 py-2.5'
  const align = (a?: string) => (a === 'right' ? 'text-right' : a === 'center' ? 'text-center' : 'text-left')
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-separate border-spacing-0">
        <thead>
          <tr>
            {columns.map(c => {
              const key = controlled ? c.sortKey : (c.sortValue ? c.key : undefined)
              return (
                <th
                  key={c.key}
                  title={key ? undefined : c.title}
                  className={clsx(
                    pad, align(c.align), c.width,
                    'sticky top-0 bg-gray-900 font-semibold text-[10px] uppercase tracking-wider text-gray-500',
                    'border-b border-gray-800 whitespace-nowrap',
                    c.title && !key && 'cursor-help',
                  )}
                >
                  {key
                    ? <SortHeader k={key} sort={state} onSort={controlled ? onSort! : toggle} title={c.title} align={c.align}>{c.header}</SortHeader>
                    : c.header}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, i) => (
            <tr
              key={rowKey(row, i)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={clsx('group', onRowClick && 'cursor-pointer')}
            >
              {columns.map(c => (
                <td
                  key={c.key}
                  className={clsx(
                    pad, align(c.align), c.width,
                    'border-b border-gray-800/60 group-last:border-b-0 group-hover:bg-gray-800/40 transition-colors',
                    c.align === 'right' && 'tabular-nums whitespace-nowrap',
                  )}
                >
                  {c.render(row, i)}
                </td>
              ))}
            </tr>
          ))}
          {shown.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="px-3 py-8 text-center text-gray-500">{empty}</td>
            </tr>
          )}
        </tbody>
      </table>
      {footer}
    </div>
  )
}
