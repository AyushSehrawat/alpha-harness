/** Tasks: everything the labs added. Only here does a task run, wait for cores, pause or stop. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { PauseIcon, PencilIcon, PlayIcon, SquareIcon, Trash2Icon } from 'lucide-react'
import { type ComponentProps, useEffect, useState } from 'react'
import { DASH, fmt } from '@/lib/format'
import { useRefetchOn } from '@/lib/ws'
import { DetailSheet } from '@/screens/pool/detail'
import { MAX_SIMULATIONS } from '@/screens/research-labs/lab-task'
import { type LabTask, labTasks, type RankedAlpha, type TaskStatus } from '@/screens/tasks/api'
import {
  Badge,
  Button,
  Disclosure,
  Empty,
  ErrorNotice,
  Field,
  Fieldset,
  Input,
  LINK,
  Metric,
  MetricBadge,
  Notice,
  Page,
  PageHeader,
  Panel,
  Progress,
  Segmented,
  signTone,
  TEXT_TONE,
} from '@/ui/kit'
import { Confirm, Dialog } from '@/ui/overlay'
import { type Column, DataTable } from '@/ui/table'

const CORES = [1, 2, 3, 4]

const STATUS: Record<TaskStatus, { label: string; tone: ComponentProps<typeof Badge>['tone'] }> = {
  IDLE: { label: 'Not Started', tone: 'outline' },
  QUEUED: { label: 'Waiting', tone: 'warn' },
  RUNNING: { label: 'Running', tone: 'profit' },
  PAUSED: { label: 'Paused', tone: 'muted' },
  COMPLETE: { label: 'Complete', tone: 'neutral' },
  FAILED: { label: 'Failed', tone: 'loss' },
}

const TOP_COLUMNS: Column<RankedAlpha>[] = [
  {
    key: 'expression',
    header: 'Expression',
    width: 'minmax(280px,3fr)',
    cell: (r) => (
      <span className="num block truncate text-ink" title={r.expression ?? undefined}>
        {r.expression ?? DASH}
      </span>
    ),
  },
  {
    key: 'sharpe',
    header: 'Sharpe',
    width: '90px',
    align: 'right',
    cell: (r) =>
      r.sharpe == null ? (
        DASH
      ) : (
        <MetricBadge tone={r.sharpe > 0 ? 'profit' : r.sharpe < 0 ? 'loss' : 'neutral'}>
          {fmt.ratio(r.sharpe)}
        </MetricBadge>
      ),
  },
  {
    key: 'fitness',
    header: 'Fitness',
    width: '80px',
    align: 'right',
    cell: (r) => fmt.ratio(r.fitness),
  },
  {
    key: 'turnover',
    header: 'Turnover',
    width: '88px',
    align: 'right',
    cell: (r) => fmt.pct(r.turnover),
  },
  {
    key: 'universe',
    header: 'Universe',
    width: '96px',
    cell: (r) => r.settings?.universe ?? DASH,
  },
  {
    key: 'neutralization',
    header: 'Neutralization',
    width: '120px',
    cell: (r) => r.settings?.neutralization ?? DASH,
  },
]

/** What a task searches for leads the table when it is not Sharpe, which the table shows anyway. */
const topColumns = (task: LabTask): Column<RankedAlpha>[] =>
  task.objectiveLabel === 'Sharpe'
    ? TOP_COLUMNS
    : [
        ...TOP_COLUMNS.slice(0, 1),
        {
          key: 'value',
          header: task.objectiveLabel,
          width: '112px',
          align: 'right',
          cell: (r) => <span className={TEXT_TONE[signTone(r.value)]}>{fmt.ratio(r.value)}</span>,
        },
        ...TOP_COLUMNS.slice(1),
      ]

type Act = { action: 'runAll' } | { action: 'run' | 'pause' | 'stop' | 'remove'; task: LabTask }

export function TasksScreen() {
  const queryClient = useQueryClient()
  const list = useQuery({ queryKey: ['lab-tasks'], queryFn: labTasks.list })
  useRefetchOn('studies', ['lab-tasks'], 2_000)
  useRefetchOn('simulations', ['lab-tasks'], 5_000)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [editing, setEditing] = useState<LabTask | null>(null)
  const [confirming, setConfirming] = useState<Act | null>(null)
  const [alphaId, setAlphaId] = useState<string | null>(null)

  // Nothing picked yet: open on what is running, then stay there. Re-deriving this every render
  // would move the pane out from under the reader the moment that task finished.
  const running = list.data?.tasks.find((t) => t.status === 'RUNNING')?.id ?? null
  useEffect(() => {
    if (running != null) setSelectedId((id) => id ?? running)
  }, [running])

  const act = useMutation({
    mutationFn: async (a: Act) => {
      if (a.action === 'runAll') await labTasks.runAll()
      else await labTasks[a.action](a.task.id)
    },
    onSuccess: () => {
      setConfirming(null)
      for (const key of [['lab-tasks'], ['bar'], ['today'], ['simulations']])
        void queryClient.invalidateQueries({ queryKey: key })
    },
  })

  // A failed action leaves its notice behind; the next dialog must not open wearing it.
  const ask = (a: Act) => {
    act.reset()
    setConfirming(a)
  }

  const all = list.data?.tasks ?? []
  const slots = list.data?.slots ?? 8
  const open = all.filter((t) => t.status !== 'COMPLETE' && t.status !== 'FAILED')
  const total = (tasks: LabTask[], pick: (t: LabTask) => number) =>
    tasks.reduce((n, t) => n + pick(t), 0)
  const runningCores = total(
    open.filter((t) => t.status === 'RUNNING'),
    (t) => t.cores,
  )
  const assignedCores = total(open, (t) => t.cores)
  const waiting = all.filter((t) => t.status === 'QUEUED').length
  const fresh = all.filter((t) => t.status === 'IDLE').length
  const selected = all.find((t) => t.id === selectedId) ?? null
  const copy = confirming ? confirmCopy(confirming, fresh) : null

  const columns: Column<LabTask>[] = [
    {
      key: 'task',
      header: 'Task',
      width: 'minmax(220px,2fr)',
      cell: (t) => (
        <span className="block min-w-0 truncate" title={t.datasetIds.join(', ')}>
          <span className="text-ink">
            {t.labName}
            {t.templateName ? ` · ${t.templateName}` : ''}
          </span>
          <span className="text-ink-subtle">
            {' · '}
            <span className="num">{`${t.region} D${t.delay}`}</span>
            {' · '}
            <span className="num">{fmt.int(t.seeds > 0 ? t.seeds : t.datasetIds.length)}</span>
            {t.seeds > 0 ? ' seeds' : t.datasetIds.length === 1 ? ' dataset' : ' datasets'}
          </span>
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      width: '110px',
      cell: (t) => <TaskBadge task={t} />,
    },
    {
      key: 'cores',
      header: 'Cores',
      width: '64px',
      align: 'right',
      cell: (t) => fmt.int(t.cores),
    },
    {
      key: 'simulations',
      header: 'Simulations',
      width: 'minmax(220px,1.5fr)',
      cell: (t) => (
        <span className="flex w-full min-w-0 items-center gap-2">
          <Progress
            className="flex-1"
            value={t.target > 0 ? t.simulated / t.target : 0}
            label="Simulated"
          />
          <span className="num shrink-0 text-body-compact">
            {fmt.int(t.simulated)} / {fmt.int(t.target)}
          </span>
        </span>
      ),
    },
    {
      key: 'best',
      header: 'Best',
      width: '96px',
      align: 'right',
      cell: (t) => (
        <span title={t.objectiveLabel} className={TEXT_TONE[signTone(t.best)]}>
          {fmt.ratio(t.best)}
        </span>
      ),
    },
    {
      key: 'actions',
      header: '',
      width: '150px',
      align: 'right',
      cell: (t) => (
        <Actions
          task={t}
          onAct={(action) =>
            action === 'pause' ? act.mutate({ action, task: t }) : ask({ action, task: t })
          }
          onEdit={() => setEditing(t)}
        />
      ),
    },
  ]

  return (
    <Page>
      <PageHeader
        title="Tasks"
        actions={
          <Button
            variant="primary"
            disabled={fresh === 0}
            onClick={() => ask({ action: 'runAll' })}
          >
            <PlayIcon />
            Run All
          </Button>
        }
      />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric
          boxed
          label="Running Cores"
          value={list.isPending ? DASH : `${fmt.int(runningCores)} / ${fmt.int(slots)}`}
        />
        <Metric
          boxed
          label="Cores Assigned"
          value={list.isPending ? DASH : fmt.int(assignedCores)}
          tone={assignedCores > slots ? 'warn' : 'neutral'}
          hint={waiting > 0 ? `${fmt.int(waiting)} waiting` : undefined}
        />
        <Metric
          boxed
          label="Simulations Assigned"
          value={list.isPending ? DASH : fmt.int(total(open, (t) => t.target))}
        />
        <Metric
          boxed
          label="Simulated"
          value={list.isPending ? DASH : fmt.int(total(open, (t) => t.simulated))}
        />
      </div>
      {list.isError && all.length > 0 && (
        <ErrorNotice error={list.error} title="Could not load tasks" />
      )}
      {act.isError && confirming === null && <ErrorNotice error={act.error} />}

      <Panel>
        {list.data && all.length === 0 ? (
          <Empty title="No tasks yet">
            <Link to="/labs" className={LINK}>
              Open Research Labs
            </Link>
          </Empty>
        ) : (
          <DataTable
            label="Tasks"
            rows={all}
            columns={columns}
            rowKey={(t) => String(t.id)}
            onRowClick={(t) => setSelectedId(t.id)}
            loading={list.isPending}
            error={list.error}
          />
        )}
      </Panel>
      {selected && <TaskDetail task={selected} onOpenAlpha={setAlphaId} />}

      {editing && <EditTask key={editing.id} task={editing} onClose={() => setEditing(null)} />}
      <DetailSheet alphaId={alphaId} onClose={() => setAlphaId(null)} />
      <Confirm
        open={confirming !== null}
        onOpenChange={(isOpen) => {
          if (!isOpen) {
            setConfirming(null)
            act.reset()
          }
        }}
        title={copy?.title ?? ''}
        confirmLabel={copy?.label ?? 'Confirm'}
        danger={confirming?.action === 'stop' || confirming?.action === 'remove'}
        pending={act.isPending}
        onConfirm={() => confirming && act.mutate(confirming)}
      >
        {copy?.body}
        {act.isError && <ErrorNotice error={act.error} className="mt-3" />}
      </Confirm>
    </Page>
  )
}

function confirmCopy(a: Act, fresh: number): { title: string; label: string; body?: string } {
  switch (a.action) {
    case 'runAll':
      return {
        title: `Run ${fmt.int(fresh)} ${fresh === 1 ? 'task' : 'tasks'}?`,
        label: 'Run All',
      }
    case 'run':
      if (a.task.status === 'PAUSED') return { title: 'Resume this task?', label: 'Resume' }
      if (a.task.status === 'FAILED')
        return {
          title: 'Retry this task?',
          label: 'Retry',
          body: 'Simulations it sent before it failed are scored on the way.',
        }
      return { title: 'Run this task?', label: 'Run Task' }
    case 'stop':
      return {
        title: 'Stop this task?',
        label: 'Stop Task',
        body: 'Simulations already sent finish and are kept; the rest come off the queue.',
      }
    default:
      return {
        title: 'Remove this task?',
        label: 'Remove Task',
        body: 'The Alphas it found stay in Alphas.',
      }
  }
}

function TaskBadge({ task }: { task: LabTask }) {
  const { label, tone } =
    task.stopping && task.status === 'RUNNING'
      ? { label: 'Stopping', tone: 'warn' as const }
      : (STATUS[task.status] ?? STATUS.IDLE)
  return <Badge tone={tone}>{label}</Badge>
}

/** A failed task can run again: simulations it sent before it failed are scored then. */
const RUN_LABEL = { IDLE: 'Run', PAUSED: 'Resume', FAILED: 'Retry' } as const

function Actions({
  task,
  onAct,
  onEdit,
}: {
  task: LabTask
  onAct: (action: 'run' | 'pause' | 'stop' | 'remove') => void
  onEdit: () => void
}) {
  const { status, stopping } = task
  const finished = status === 'COMPLETE' || status === 'FAILED'
  return (
    // Inside a clickable row: a click on these must not also select the row.
    <span
      role="group"
      aria-label="Task actions"
      className="flex items-center justify-end gap-0.5"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      {(status === 'IDLE' || status === 'PAUSED' || status === 'FAILED') && (
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={RUN_LABEL[status]}
          title={RUN_LABEL[status]}
          onClick={() => onAct('run')}
        >
          <PlayIcon />
        </Button>
      )}
      {(status === 'RUNNING' || status === 'QUEUED') && !stopping && (
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Pause"
          title="Pause"
          onClick={() => onAct('pause')}
        >
          <PauseIcon />
        </Button>
      )}
      {(status === 'RUNNING' || status === 'PAUSED' || status === 'QUEUED') && !stopping && (
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Stop"
          title="Stop"
          onClick={() => onAct('stop')}
        >
          <SquareIcon />
        </Button>
      )}
      {!finished && !stopping && (
        <Button size="icon-sm" variant="ghost" aria-label="Edit" title="Edit" onClick={onEdit}>
          <PencilIcon />
        </Button>
      )}
      {status !== 'RUNNING' && (
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Remove"
          title="Remove"
          onClick={() => onAct('remove')}
        >
          <Trash2Icon />
        </Button>
      )}
    </span>
  )
}

function TaskDetail({
  task,
  onOpenAlpha,
}: {
  task: LabTask
  onOpenAlpha: (alphaId: string) => void
}) {
  const top = useQuery({
    queryKey: ['lab-tasks', 'top', task.id],
    queryFn: () => labTasks.top(task.id, 50),
  })
  return (
    <Panel
      title={[task.labName, task.templateName, `${task.region} D${task.delay}`]
        .filter(Boolean)
        .join(' · ')}
      description={
        task.seeds > 0
          ? `${task.universe ?? DASH} · ${fmt.int(task.seeds)} seeds · Population ${fmt.int(task.population)} · Mutation ${fmt.pct(task.mutationRate, 0)}`
          : `Decay ${task.decay ?? DASH} · ${fmt.int(task.fields)} fields · ${task.datasetIds.join(', ')}`
      }
      actions={<TaskBadge task={task} />}
    >
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric
            boxed
            label="Simulated"
            value={`${fmt.int(task.simulated)} / ${fmt.int(task.target)}`}
          />
          <Metric boxed label="In Flight" value={fmt.int(task.queued + task.running)} />
          <Metric boxed label="Failed" value={fmt.int(task.failed)} />
          <Metric boxed label={`Best ${task.objectiveLabel}`} value={fmt.ratio(task.best)} />
        </div>
        {task.message && (
          <Notice tone={task.status === 'FAILED' ? 'error' : 'info'} title={task.message} />
        )}
        {task.template && (
          <Disclosure summary="Template">
            <code className="num text-body-compact break-all text-ink">{task.template}</code>
          </Disclosure>
        )}
        {top.isError && top.data && (
          <ErrorNotice error={top.error} title="Could not load the best Alphas" />
        )}
        <DataTable
          label="Top Alphas"
          rows={top.data ?? []}
          columns={topColumns(task)}
          rowKey={(r) => String(r.trialId)}
          onRowClick={(r) => r.alphaId && onOpenAlpha(r.alphaId)}
          loading={top.isPending}
          error={top.error}
          empty="No Alphas back yet."
        />
      </div>
    </Panel>
  )
}

function EditTask({ task, onClose }: { task: LabTask; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [cores, setCores] = useState(task.cores)
  const [simulations, setSimulations] = useState(String(task.target))
  const count = Number(simulations)
  // Below what it has already simulated the task is finished the moment it is saved, and the
  // count reads past its own target. Stop is the way to end a task early.
  const least = Math.max(1, task.simulated)
  const valid = Number.isInteger(count) && count >= least && count <= MAX_SIMULATIONS
  const change = useMutation({
    mutationFn: () => labTasks.change(task.id, { cores, simulations: count }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['lab-tasks'] })
      onClose()
    },
  })

  return (
    <Dialog
      open
      onOpenChange={(isOpen) => !isOpen && onClose()}
      title="Edit Task"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!valid}
            loading={change.isPending}
            onClick={() => change.mutate()}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Fieldset legend="Cores">
          <Segmented
            label="Cores"
            items={CORES.map((v) => ({ value: v, label: v }))}
            value={cores}
            onChange={setCores}
          />
        </Fieldset>
        <Field
          label="Simulations"
          hint={
            task.simulated > 0 && (
              <>
                <span className="num">{fmt.int(task.simulated)}</span> simulated so far: the least
                it can be set to
              </>
            )
          }
        >
          <Input
            type="number"
            min={least}
            max={MAX_SIMULATIONS}
            step={1}
            className="w-40"
            value={simulations}
            onChange={(e) => setSimulations(e.target.value)}
          />
        </Field>
        {change.isError && <ErrorNotice error={change.error} title="Could not change the task" />}
      </div>
    </Dialog>
  )
}
