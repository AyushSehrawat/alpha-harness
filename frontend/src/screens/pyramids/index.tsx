/**
 * Sync with BRAIN: the sync matrix that downloads every market's Data Fields, above BRAIN's
 * Pyramid Multiplier for every Region · Delay · Dataset Category, with ✓ where 3 or more of your
 * Alphas formulate the pyramid this quarter.
 */

import { useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { catalog } from '@/api/catalog'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { useScope } from '@/lib/scope'
import { Empty, ErrorNotice, Page, PageHeader, Panel, Skeleton } from '@/ui/kit'
import { SyncHero } from './sync-matrix'

const REFRESH_MS = 10 * 60 * 1000

/** A neutral fill that deepens with the multiplier, ×1.0 → step 0 … ×2.0 → step 5; ink figures stay ≥ 6:1. */
const TINT = [
  'bg-pyramid-0',
  'bg-pyramid-1',
  'bg-pyramid-2',
  'bg-pyramid-3',
  'bg-pyramid-4',
  'bg-pyramid-5',
] as const
const tint = (multiplier: number | null) =>
  TINT[
    multiplier == null
      ? 0
      : Math.max(0, Math.min(TINT.length - 1, Math.round((multiplier - 1) / 0.2)))
  ]

const key = (categoryId: string, region: string, delay: number) =>
  `${categoryId}|${region}|${delay}`
const times = (m: number | null) => (m == null ? DASH : `×${fmt.ratio(m, 1)}`)

export function PyramidsScreen() {
  const query = useQuery({
    queryKey: ['catalog', 'pyramids'],
    queryFn: catalog.pyramids,
    refetchInterval: REFRESH_MS,
    staleTime: REFRESH_MS / 2,
  })
  const data = query.data
  const cells = new Map((data?.cells ?? []).map((c) => [key(c.categoryId, c.region, c.delay), c]))
  const navigate = useNavigate()
  const [scope, update] = useScope('data')

  return (
    <Page>
      <PageHeader
        title="Sync with BRAIN"
        description="Download every market's Data Fields from BRAIN, and see each Pyramid Multiplier."
      />
      <SyncHero
        scope={scope}
        onPick={(change) => {
          update(change)
          void navigate({ to: '/data/$tab', params: { tab: 'fields' } })
        }}
      />
      <Panel title="Pyramid Multiplier Map">
        {query.isError ? (
          <ErrorNotice error={query.error} title="Could not load pyramids from BRAIN" />
        ) : !data ? (
          <Skeleton className="h-96" />
        ) : data.categories.length === 0 ? (
          <Empty title="No Dataset Categories">BRAIN returned no pyramids to show.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-256 table-fixed border-separate border-spacing-1 text-body-compact">
              <thead>
                <tr>
                  <th scope="col" className="w-32 pr-3 text-left font-medium text-ink-subtle">
                    Category
                  </th>
                  {data.columns.map((column) => (
                    <th
                      key={`${column.region}-${column.delay}`}
                      scope="col"
                      className="num px-1 font-medium whitespace-nowrap text-ink-subtle"
                    >
                      {column.region} D{column.delay}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.categories.map((category) => (
                  <tr key={category.id}>
                    <th
                      scope="row"
                      className="truncate pr-3 text-left font-normal text-ink-muted"
                      title={category.name}
                    >
                      {category.name}
                    </th>
                    {data.columns.map((column) => {
                      const id = `${column.region}-${column.delay}`
                      const cell = cells.get(key(category.id, column.region, column.delay))
                      if (!cell) {
                        return (
                          <td key={id} className="num text-center text-ink-subtle">
                            {DASH}
                          </td>
                        )
                      }
                      return (
                        <td key={id}>
                          <span
                            title={`${category.name} · ${column.region} D${column.delay} · Pyramid Multiplier ${times(cell.multiplier)} · ${fmt.int(cell.alphaCount)} of your Alphas this quarter${cell.lit ? ' · formulated' : ''}`}
                            className={cn(
                              'num flex h-8 items-center justify-center rounded-sm border border-hairline-strong whitespace-nowrap text-ink',
                              tint(cell.multiplier),
                            )}
                          >
                            {times(cell.multiplier)}
                            {cell.lit && ' ✓'}
                          </span>
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </Page>
  )
}
