/** Non-component pieces the Data Explorer tabs share: the Fields filter store and helpers. */

import { create } from 'zustand'
import type { FieldFilter, FieldSortKey } from '@/api/catalog'
import type { Scope } from '@/api/types'
import { cn } from '@/lib/cn'
import { DASH, fmt, isNum } from '@/lib/format'
import { useDatasetPick } from '@/screens/data/dataset-pick'
import { STATUS } from '@/ui/kit'
import type { Sort } from '@/ui/table'

/** The kit's STATUS box, one size down for the dense Data Explorer rows. */
export const STAT = cn(STATUS, 'inline-flex h-6 px-2')

export type FieldFilterState = Omit<FieldFilter, 'sort_by' | 'sort_desc' | 'limit' | 'offset'>

interface FieldFilterStore {
  filter: FieldFilterState
  sort: Sort
  offset: number
  set: (change: Partial<FieldFilterState>) => void
  replace: (filter: FieldFilterState) => void
  setSort: (sort: Sort) => void
  page: (offset: number) => void
}

/** Lives outside the Fields tab so the filter survives leaving the Data Explorer and coming back. */
export const useFieldFilter = create<FieldFilterStore>()((set) => ({
  filter: {},
  sort: { key: 'alpha_count', desc: true },
  offset: 0,
  set: (change) => set((s) => ({ filter: { ...s.filter, ...change }, offset: 0 })),
  replace: (filter) => set({ filter, offset: 0 }),
  setSort: (sort) =>
    set({
      sort: { key: sort.key as FieldSortKey, desc: sort.desc },
      offset: 0,
    }),
  page: (offset) => set({ offset }),
}))

const NONE: string[] = []

/**
 * The datasets the Fields tab filters on, and how to change them: while a lab picks datasets,
 * that pick (kept across a reload); otherwise the Fields filter's own.
 */
export function useDatasetChoice(): [string[], (ids: string[]) => void] {
  const picking = useDatasetPick((s) => s.active)
  const picked = useDatasetPick((s) => s.ids)
  const filtered = useFieldFilter((s) => s.filter.dataset_ids ?? NONE)
  if (picking) {
    return [
      picked,
      (ids) => {
        useDatasetPick.getState().setIds(ids)
        useFieldFilter.getState().page(0)
      },
    ]
  }
  return [filtered, (ids) => useFieldFilter.getState().set({ dataset_ids: ids })]
}

export const isActive = (v: unknown) =>
  v != null && v !== '' && !(Array.isArray(v) && v.length === 0)

export const sameScope = (a: Scope, b: Scope) =>
  a.region === b.region &&
  a.delay === b.delay &&
  a.universe === b.universe &&
  a.instrumentType === b.instrumentType

/** `×1.4` */
export const multiplier = (v: number | null | undefined) =>
  isNum(v) ? `×${fmt.ratio(v, 1)}` : DASH

/** Client-side sort for tables the backend returns whole. Absent values last. */
export function sortRows<T>(rows: T[], sort: Sort): T[] {
  const key = sort.key as keyof T
  return [...rows].sort((a, b) => {
    const x = a[key]
    const y = b[key]
    if (x == null) return y == null ? 0 : 1
    if (y == null) return -1
    const c = x < y ? -1 : x > y ? 1 : 0
    return sort.desc ? -c : c
  })
}

/** `themes` is JSON array text of strings or `{id,name}` objects. */
export function parseThemes(raw: string | null): string[] {
  if (!raw) return []
  try {
    const list: unknown = JSON.parse(raw)
    if (!Array.isArray(list)) return []
    return list.map((t) =>
      typeof t === 'string'
        ? t
        : ((t as { name?: string; id?: string })?.name ??
          (t as { id?: string })?.id ??
          JSON.stringify(t)),
    )
  } catch {
    return [raw]
  }
}
