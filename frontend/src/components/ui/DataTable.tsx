import type { ReactNode } from 'react'
import clsx from 'clsx'

export interface Column<T> {
  key: string
  header: ReactNode
  align?: 'left' | 'right' | 'center'
  /** Tailwind width class, e.g. "w-24" — keeps columns from drifting between tables. */
  width?: string
  title?: string
  render: (row: T, index: number) => ReactNode
}

/**
 * One table style for the whole app: quiet uppercase headers, right-aligned tabular
 * numbers, row hover, and a real empty state. Wraps horizontally on narrow screens.
 */
export function DataTable<T>({
  columns, rows, rowKey, empty = 'Nothing here yet.', dense = false, onRowClick, footer,
}: {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T, index: number) => string
  empty?: ReactNode
  dense?: boolean
  onRowClick?: (row: T) => void
  footer?: ReactNode
}) {
  const pad = dense ? 'px-3 py-1.5' : 'px-3 py-2.5'
  const align = (a?: string) => (a === 'right' ? 'text-right' : a === 'center' ? 'text-center' : 'text-left')
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-separate border-spacing-0">
        <thead>
          <tr>
            {columns.map(c => (
              <th
                key={c.key}
                title={c.title}
                className={clsx(
                  pad, align(c.align), c.width,
                  'sticky top-0 bg-gray-900 font-semibold text-[10px] uppercase tracking-wider text-gray-500',
                  'border-b border-gray-800 whitespace-nowrap',
                  c.title && 'cursor-help',
                )}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
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
          {rows.length === 0 && (
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
