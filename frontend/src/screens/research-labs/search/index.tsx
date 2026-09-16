/**
 * Search Lab: choose datasets, cores and simulations, then run the search as a task in
 * Tasks. It writes one- and two-operator Alphas from the datasets' fields, steering towards
 * the best Sharpe.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { PlayIcon } from 'lucide-react'
import { toast } from 'sonner'
import { today } from '@/api/core'
import { errorMessage } from '@/api/http'
import { useDebounced } from '@/lib/use-debounced'
import {
  labBody,
  MAX_SIMULATIONS,
  simulationsValid,
  useLabMarket,
  vectorOperatorsOf,
} from '@/screens/research-labs/lab-task'
import { type SearchLabRequest, searchLab } from '@/screens/research-labs/search/api'
import { DatasetsPanel, SettingsPanel } from '@/screens/research-labs/task-settings'
import { Button, ErrorNotice, Page, PageHeader } from '@/ui/kit'
import { useSearchLab } from './state'

export function SearchLabScreen() {
  const stored = useSearchLab()
  const day = useQuery({ queryKey: ['today'], queryFn: () => today.get() })
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { chosen, names, choose } = useLabMarket(stored, stored.set, '/labs/search')

  const options = useQuery({
    queryKey: ['search-lab', 'options'],
    queryFn: searchLab.options,
    staleTime: 5 * 60_000,
  })
  // Until the user types a number, the task takes what is left of today (never stored).
  const maxSimulations = options.data?.maxSimulations ?? MAX_SIMULATIONS
  const unspoken = day.data?.simulations.unspoken ?? 0
  const draft = {
    ...stored,
    simulations: stored.simulations ?? (unspoken > 0 ? Math.min(unspoken, maxSimulations) : null),
  }
  const vectorOperators = vectorOperatorsOf(draft, options.data?.vector)
  const body: SearchLabRequest = labBody(draft, vectorOperators)
  const key = JSON.stringify(body)
  const settledKey = useDebounced(key, 300)
  const preview = useQuery({
    queryKey: ['search-lab', 'preview', settledKey],
    queryFn: () => searchLab.preview(JSON.parse(settledKey) as SearchLabRequest),
    enabled: chosen && options.isSuccess && settledKey === key,
    placeholderData: keepPreviousData,
  })
  const plan = chosen ? preview.data : undefined

  const add = useMutation({
    mutationFn: (count: number) => searchLab.runTask({ ...body, simulations: count }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['lab-tasks'] })
      void queryClient.invalidateQueries({ queryKey: ['today'] })
      toast.success('Task running', {
        action: {
          label: 'Open Tasks',
          onClick: () => void navigate({ to: '/tasks' }),
        },
      })
    },
    onError: (error) => toast.error(errorMessage(error)),
  })
  const ready =
    plan !== undefined &&
    settledKey === key &&
    !preview.isFetching &&
    plan.problems.length === 0 &&
    simulationsValid(draft, maxSimulations)
  // What stops Run Task that no panel below already says.
  const blocked = !chosen
    ? 'Choose datasets to run.'
    : draft.simulations === null
      ? 'Enter the simulations to run.'
      : null

  return (
    <Page>
      <PageHeader
        title="Search Lab"
        actions={
          <>
            {blocked && (
              <span id="run-task-blocked" className="text-body-compact text-ink-subtle">
                {blocked}
              </span>
            )}
            <Button
              variant="primary"
              disabled={!ready}
              loading={add.isPending}
              aria-describedby={blocked ? 'run-task-blocked' : undefined}
              onClick={() => draft.simulations !== null && add.mutate(draft.simulations)}
            >
              <PlayIcon />
              Run Task
            </Button>
          </>
        }
      />
      {options.isError && (
        <ErrorNotice error={options.error} title="Could not read your operators" />
      )}
      <DatasetsPanel
        ids={draft.datasetIds}
        names={names}
        onChoose={choose}
        onRemove={(id) => stored.set({ datasetIds: stored.datasetIds.filter((x) => x !== id) })}
      />
      <SettingsPanel
        draft={draft}
        set={stored.set}
        vector={options.data?.vector ?? []}
        chosenVector={vectorOperators}
        decays={options.data?.decays}
        maxSimulations={maxSimulations}
        plan={plan}
        error={chosen && preview.isError ? preview.error : null}
      />
    </Page>
  )
}
