/**
 * A virtualised table: only visible rows mount, so a 500-row page scrolls without layout cost.
 * Sorting and paging are the backend's job (DuckDB); this only asks.
 */

import { useVirtualizer } from '@tanstack/react-virtual'
import { ArrowDownIcon, ArrowUpIcon, ChevronLeftIcon, ChevronRightIcon } from 'lucide-react'
import { type ReactNode, useRef } from 'react'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { Button, Empty, ErrorNotice, Skeleton } from './kit'

export interface Column<T> {
  key: string
  header: ReactNode
  cell: (row: T) => ReactNode
  /** CSS grid track, e.g. `120px` or `minmax(240px,2fr)`. */
  width?: string
  align?: 'left' | 'right'
  sortable?: boolean
}

export interface Sort {
  key: string
  desc: boolean
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  onRowClick,
  rowClass,
  sort,
  onSort,
  selected,
  onSelect,
  loading,
  error,
  empty = 'Nothing to show.',
  maxHeight = '70vh',
  rowHeight = 34,
  label,
}: {
  rows: T[]
  columns: Column<T>[]
  rowKey: (row: T) => string
  onRowClick?: (row: T) => void
  /** Extra classes for one row, so a table can mark rows that mean something. */
  rowClass?: (row: T) => string | undefined
  sort?: Sort
  onSort?: (sort: Sort) => void
  /** With `onSelect`, adds a checkbox column. */
  selected?: ReadonlySet<string>
  onSelect?: (key: string, on: boolean) => void
  loading?: boolean
  /** A failed query: shown in place of the empty state, which would claim there is nothing. */
  error?: unknown
  empty?: ReactNode
  maxHeight?: string
  rowHeight?: number
  label: string
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const virtual = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => rowHeight,
    overscan: 12,
  })
  const selectable = selected !== undefined && onSelect !== undefined
  const template = [
    selectable ? '36px' : null,
    ...columns.map((c) => c.width ?? 'minmax(96px,1fr)'),
  ]
    .filter(Boolean)
    .join(' ')

  const header = (
    <div role="rowgroup" className="sticky top-0 z-10">
      <div
        role="row"
        aria-rowindex={1}
        className="grid border-b border-hairline-strong bg-surface-1 select-none"
        style={{ gridTemplateColumns: template }}
      >
        {selectable && <div role="columnheader" aria-label="Select" />}
        {columns.map((column) => {
          const active = sort?.key === column.key
          const content = (
            <>
              <span className="truncate">{column.header}</span>
              {active &&
                (sort.desc ? (
                  <ArrowDownIcon className="size-3 shrink-0 text-primary" />
                ) : (
                  <ArrowUpIcon className="size-3 shrink-0 text-primary" />
                ))}
            </>
          )
          return (
            <div
              key={column.key}
              role="columnheader"
              aria-sort={active ? (sort.desc ? 'descending' : 'ascending') : undefined}
              className={cn(
                'flex h-8 items-center px-3 text-caption font-medium uppercase tracking-wide text-ink-subtle',
                column.align === 'right' && 'justify-end',
              )}
            >
              {column.sortable && onSort ? (
                <button
                  type="button"
                  className={cn(
                    'inline-flex items-center gap-1 transition-colors hover:text-ink',
                    active && 'text-ink',
                  )}
                  onClick={() => onSort({ key: column.key, desc: active ? !sort.desc : true })}
                >
                  {content}
                </button>
              ) : (
                content
              )}
            </div>
          )
        })}
      </div>
    </div>
  )

  return (
    <div
      ref={scroller}
      role="table"
      aria-label={label}
      aria-rowcount={rows.length + 1}
      className="min-w-0 overflow-auto"
      style={{ maxHeight }}
    >
      {/*
        fit-content: fills the panel, and only scrolls when the columns' minimum widths do not fit.
        Presentational, so the rowgroups below stay owned by the table rather than by a plain div.
      */}
      <div role="presentation" className="w-fit min-w-full">
        {header}
        {loading && rows.length === 0 ? (
          <div className="flex flex-col gap-1 p-2">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} className="h-6" />
            ))}
          </div>
        ) : rows.length === 0 && error ? (
          <ErrorNotice error={error} className="m-2" />
        ) : rows.length === 0 ? (
          <Empty title={empty} />
        ) : (
          <div role="rowgroup" className="relative" style={{ height: virtual.getTotalSize() }}>
            {virtual.getVirtualItems().map((item) => {
              const row = rows[item.index]
              if (row === undefined) return null
              const key = rowKey(row)
              const isSelected = selectable && selected.has(key)
              return (
                <div
                  key={key}
                  role="row"
                  aria-rowindex={item.index + 2}
                  aria-selected={selectable ? isSelected : undefined}
                  tabIndex={onRowClick ? 0 : undefined}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  onKeyDown={
                    onRowClick
                      ? (e) => {
                          if (e.key !== 'Enter' && e.key !== ' ') return
                          e.preventDefault()
                          onRowClick(row)
                        }
                      : undefined
                  }
                  className={cn(
                    'absolute inset-x-0 grid border-b border-hairline-subtle bg-surface-1 text-body-compact transition-colors',
                    onRowClick &&
                      'cursor-pointer hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:-outline-offset-2',
                    isSelected && 'bg-surface-2',
                    rowClass?.(row),
                  )}
                  style={{
                    gridTemplateColumns: template,
                    height: rowHeight,
                    transform: `translateY(${item.start}px)`,
                  }}
                >
                  {selectable && (
                    <div
                      role="cell"
                      className="flex items-center justify-center"
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => e.stopPropagation()}
                    >
                      <input
                        type="checkbox"
                        aria-label="Select row"
                        className="size-3.5"
                        checked={isSelected}
                        onChange={(e) => onSelect(key, e.target.checked)}
                      />
                    </div>
                  )}
                  {columns.map((column) => {
                    const content = column.cell(row)
                    return (
                      <div
                        key={column.key}
                        role="cell"
                        className={cn(
                          'flex min-w-0 items-center overflow-hidden px-3',
                          column.align === 'right' && 'mono-metric justify-end',
                        )}
                      >
                        {/* A flex box clips without an ellipsis; plain text gets a block that truncates. */}
                        {typeof content === 'string' || typeof content === 'number' ? (
                          <span className="truncate">{content}</span>
                        ) : (
                          content
                        )}
                      </div>
                    )
                  })}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

export function Pager({
  total,
  offset,
  limit,
  onChange,
}: {
  total: number
  offset: number
  limit: number
  onChange: (offset: number) => void
}) {
  const end = Math.min(total, offset + limit)
  return (
    <div className="flex items-center justify-end gap-2 text-body-compact text-ink-subtle">
      <span className="num font-medium">
        {total === 0 ? 0 : fmt.int(offset + 1)}–{fmt.int(end)} of {fmt.int(total)}
      </span>
      <div className="flex items-center gap-1 rounded-sm border border-hairline bg-surface-1 p-0.5">
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Previous page"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          <ChevronLeftIcon />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Next page"
          disabled={end >= total}
          onClick={() => onChange(offset + limit)}
        >
          <ChevronRightIcon />
        </Button>
      </div>
    </div>
  )
}
