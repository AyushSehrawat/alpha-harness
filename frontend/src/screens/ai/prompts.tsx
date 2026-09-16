import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { scopeLabel } from '@/api/types'
import { fmt } from '@/lib/format'
import { useScope } from '@/lib/scope'
import { llm, type PromptInfo } from '@/screens/ai/api'
import { Badge, ErrorNotice, KV, LINK, Notice, Panel, Skeleton } from '@/ui/kit'
import { Sheet } from '@/ui/overlay'
import { ScopePicker } from '@/ui/scope-picker'
import { type Column, DataTable } from '@/ui/table'

const CONTEXT: Record<PromptInfo['context'], string> = {
  none: 'No catalog context',
  catalog_tree: 'Dataset hierarchy',
  dataset_fields: 'Fields of chosen datasets',
}

const PRE =
  'num overflow-auto rounded-md border border-hairline bg-canvas p-3 text-body-compact whitespace-pre-wrap text-ink-muted'

export function Prompts() {
  const prompts = useQuery({
    queryKey: ['ai', 'prompts'],
    queryFn: llm.prompts,
  })
  const [scope, setScope] = useScope('ai')
  const context = useQuery({
    queryKey: ['ai', 'context', scope],
    queryFn: () => llm.context(scope),
  })
  const [open, setOpen] = useState<PromptInfo | null>(null)

  const columns: Column<PromptInfo>[] = [
    {
      key: 'label',
      header: 'Prompt',
      width: 'minmax(160px,1.2fr)',
      cell: (p) => <span className="text-ink">{p.label}</span>,
    },
    {
      key: 'context',
      header: 'Context',
      width: 'minmax(160px,1fr)',
      cell: (p) => <Badge tone="outline">{CONTEXT[p.context]}</Badge>,
    },
    {
      key: 'model',
      header: 'Model',
      width: 'minmax(140px,1fr)',
      cell: (p) => (
        <span className="num text-body-compact text-ink-subtle">{p.model ?? 'chat default'}</span>
      ),
    },
    {
      key: 'temperature',
      header: 'Temperature',
      width: '100px',
      align: 'right',
      cell: (p) => fmt.ratio(p.temperature, 1),
    },
    {
      key: 'characters',
      header: 'Characters',
      width: '100px',
      align: 'right',
      cell: (p) => fmt.int(p.characters),
    },
    {
      key: 'tokens',
      header: '~Tokens',
      width: '90px',
      align: 'right',
      cell: (p) => fmt.int(p.estimatedTokens),
    },
  ]

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title="Prompts"
        description="Every instruction the assistant receives, word for word. Click one to read it. They are read-only: the backend offers no way to edit or reset them yet."
        bodyClassName="p-0"
      >
        {prompts.isError ? (
          <ErrorNotice className="m-4" title="Could not load the prompts" error={prompts.error} />
        ) : (
          <DataTable
            label="Prompts"
            rows={prompts.data?.prompts ?? []}
            columns={columns}
            rowKey={(p) => p.slug}
            loading={prompts.isLoading}
            onRowClick={setOpen}
            empty="No prompts."
          />
        )}
      </Panel>

      <Panel
        title="What the model sees about your data"
        description="The dataset hierarchy (categories, subcategories and datasets with their metadata), never the raw field catalogue, which would fill the context window."
        actions={<ScopePicker scope={scope} onChange={setScope} />}
      >
        {context.isError ? (
          <ErrorNotice title="Could not render the context" error={context.error} />
        ) : !context.data ? (
          <Skeleton className="h-96" />
        ) : (
          <div className="flex flex-col gap-2">
            <p className="num text-body-compact text-ink-subtle">
              {context.data.scope} · {fmt.int(context.data.counts.categories)} categories ·{' '}
              {fmt.int(context.data.counts.subcategories)} subcategories ·{' '}
              {fmt.int(context.data.counts.datasets)} datasets ·{' '}
              {fmt.int(context.data.counts.fields)} fields summarised ·{' '}
              {fmt.int(context.data.characters)} chars · ~{fmt.int(context.data.estimatedTokens)}{' '}
              tokens
            </p>
            {context.data.counts.datasets === 0 && (
              <Notice tone="warn" title={`${scopeLabel(scope)} is not downloaded`}>
                The model would see an empty catalogue.{' '}
                <Link to="/data/$tab" params={{ tab: 'fields' }} className={LINK}>
                  Download it in Data
                </Link>
              </Notice>
            )}
            <section aria-label="Model context">
              <pre className={`${PRE} max-h-128`}>{context.data.text}</pre>
            </section>
          </div>
        )}
      </Panel>

      <Sheet
        open={open !== null}
        onOpenChange={(o) => !o && setOpen(null)}
        title={open?.label}
        description={open?.purpose}
      >
        {open && (
          <div className="flex flex-col gap-3">
            <KV
              items={[
                ['Slug', open.slug],
                ['Context', CONTEXT[open.context]],
                ['Model', open.model ?? 'the chat default'],
                ['Temperature', fmt.ratio(open.temperature, 1)],
                [
                  'Size',
                  `${fmt.int(open.characters)} chars · ~${fmt.int(open.estimatedTokens)} tokens`,
                ],
              ]}
            />
            <section aria-label="Prompt text">
              <pre className={PRE}>{open.body}</pre>
            </section>
          </div>
        )}
      </Sheet>
    </div>
  )
}
