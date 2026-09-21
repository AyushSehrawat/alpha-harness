/**
 * The Dashboard's one click (CLAUDE.md §1.3): a focused run of today's simulations, now, over
 * the datasets last chosen in Search Lab or else the pyramids not yet formulated.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { PlayIcon } from 'lucide-react'
import { useState } from 'react'
import type { Today } from '@/api/types'
import { fmt } from '@/lib/format'
import { DEFAULT_SCOPE, isRegionAgnostic } from '@/lib/scope'
import { searchLab } from '@/screens/research-labs/search/api'
import { useSearchLab } from '@/screens/research-labs/search/state'
import { Button, Disclosure, ErrorNotice, Input, LINK, QuotaGauge } from '@/ui/kit'

/** The backend's DEFAULT_RUN: finishes in about half an hour over eight cores. */
const DEFAULT_RUN = 500

export function RunToday({ today }: { today: Today | undefined }) {
  const queryClient = useQueryClient()
  // The typed text, not the number sent: binding the field to the clamped value would rewrite
  // it mid-keystroke.
  const [typed, setTyped] = useState<string | null>(null)
  const run = useMutation({
    mutationFn: (simulations: number) => {
      const pick = useSearchLab.getState()
      // All regions at once is a Search Lab choice: its alphas need two regions to hold up
      // before any of them can be submitted, which no first run should be steered into.
      return searchLab.quick(
        pick.datasetIds.length > 0 && !isRegionAgnostic(pick)
          ? {
              region: pick.region,
              delay: pick.delay,
              universe: pick.universe,
              dataset_ids: pick.datasetIds,
              simulations,
            }
          : {
              region: DEFAULT_SCOPE.region,
              delay: DEFAULT_SCOPE.delay,
              universe: DEFAULT_SCOPE.universe,
              simulations,
            },
      )
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['today'] })
      void queryClient.invalidateQueries({ queryKey: ['lab-tasks'] })
    },
  })

  // Getting Started owns the one action until something is synced.
  if (!today?.catalog.anySynced) return null
  const left = today.simulations.unspoken
  const typedNumber = typed === null ? null : Number.parseInt(typed, 10)
  const size = Math.max(
    1,
    Math.min(
      left,
      typedNumber !== null && Number.isFinite(typedNumber) ? typedNumber : DEFAULT_RUN,
    ),
  )

  return (
    <section aria-label="Today's simulations" className="flex flex-col gap-2 px-1">
      {left > 0 && !run.isSuccess && (
        <div className="panel-highlight flex flex-col gap-3 rounded-lg border border-hairline bg-surface-1 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="primary"
                className="max-sm:w-full"
                loading={run.isPending}
                onClick={() => run.mutate(size)}
              >
                <PlayIcon />
                Run {fmt.int(size)} Simulations
              </Button>
            </div>
            <span className="num text-body-compact text-ink-subtle">
              {fmt.pct(
                today.simulations.limit ? today.simulations.used / today.simulations.limit : 0,
                0,
              )}{' '}
              depleted
            </span>
          </div>
          <Disclosure summary="Run more">
            <label className="flex flex-wrap items-center gap-2 px-3 pb-3 text-body text-ink-subtle">
              Simulations
              <Input
                type="number"
                min={1}
                max={left}
                value={typed ?? String(size)}
                onChange={(e) => setTyped(e.currentTarget.value)}
                // Held to what the day allows once the field is left, never mid-keystroke.
                onBlur={() => setTyped(String(size))}
                className="w-28"
              />
              of <span className="num">{fmt.int(left)}</span> left today. The full allowance takes
              hours; sleep pauses it and nothing is lost.
            </label>
          </Disclosure>
          <QuotaGauge
            used={today.simulations.used}
            limit={today.simulations.limit}
            label="Daily simulation allowance depletion"
          />
        </div>
      )}
      {/* Always mounted, so the outcome is announced when it arrives. */}
      <div aria-live="polite">
        {run.isSuccess && (
          <p className="text-body text-ink">
            Running <span className="num">{fmt.int(run.data.tasks.length)}</span>{' '}
            {run.data.tasks.length === 1 ? 'task' : 'tasks'} ·{' '}
            <span className="num">{fmt.int(run.data.simulations)}</span> simulations.{' '}
            <Link to="/tasks" className={LINK}>
              Open Tasks
            </Link>
          </p>
        )}
        {run.isError && (
          <ErrorNotice error={run.error} title="Today's simulations could not start" />
        )}
      </div>
    </section>
  )
}
