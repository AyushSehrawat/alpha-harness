/** Providers as cards: pick one, then add its free API key in a popup. */

import { useMutation } from '@tanstack/react-query'
import { ExternalLinkIcon, PlusIcon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { errorMessage } from '@/api/http'
import { fmt } from '@/lib/format'
import { type LLMKey, type LLMProvider, llm } from '@/screens/ai/api'
import { Badge, Button, Empty, ErrorNotice, Field, Input, Notice, Panel, Skeleton } from '@/ui/kit'
import { Dialog } from '@/ui/overlay'
import { useInvalidateKeys, useKeys, useModels, useProviders } from './shared'

export function Providers() {
  const providers = useProviders()
  const models = useModels()
  const keys = useKeys()
  const [adding, setAdding] = useState<LLMProvider | null>(null)

  if (providers.isError)
    return <ErrorNotice title="Could not load the providers" error={providers.error} />
  if (!providers.data) return <Skeleton className="h-64" />

  const { data } = providers
  if (data.providers.length === 0) {
    return (
      <Panel>
        <Empty title="The backend lists no providers" />
      </Panel>
    )
  }

  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {data.providers.map((p) => {
          const keyCount = keys.data?.keys.filter((k) => k.provider === p.id).length ?? 0
          // Google lists no models of its own: they are in the shared roster.
          const modelCount =
            p.models.length || (models.data?.models ?? []).filter((m) => m.provider === p.id).length
          return (
            <button
              key={p.id}
              type="button"
              onClick={() => setAdding(p)}
              className="group flex min-h-32 flex-col justify-between gap-4 rounded-lg border border-hairline bg-surface-1 p-4 text-left transition-colors hover:border-hairline-strong hover:bg-surface-2"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex min-w-0 flex-col gap-1.5">
                  <span className="text-title text-ink">{p.label}</span>
                  {p.id === data.default && <Badge className="w-fit">Recommended</Badge>}
                </div>
                <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-hairline text-ink-subtle transition-colors group-hover:border-hairline-strong group-hover:text-ink">
                  <PlusIcon className="size-4" />
                </span>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-body-compact">
                {keys.isPending ? null : keyCount > 0 ? (
                  <Badge>
                    <span className="num">{fmt.int(keyCount)}</span>{' '}
                    {keyCount === 1 ? 'Key' : 'Keys'}
                  </Badge>
                ) : (
                  <span className="text-ink-subtle">No Key yet</span>
                )}
                {modelCount > 0 && (
                  <span className="text-ink-subtle">
                    <span className="num">{fmt.int(modelCount)}</span>{' '}
                    {modelCount === 1 ? 'model' : 'models'}
                  </span>
                )}
              </div>
            </button>
          )
        })}
      </div>
      <AddKeyDialog provider={adding} onClose={() => setAdding(null)} />
    </>
  )
}

/** Opens for one provider. The key is cleared as soon as it is sent; only its last characters come back. */
function AddKeyDialog({
  provider,
  onClose,
}: {
  provider: LLMProvider | null
  onClose: () => void
}) {
  const invalidate = useInvalidateKeys()
  const [key, setKey] = useState('')
  const [label, setLabel] = useState('')
  const [added, setAdded] = useState<LLMKey | null>(null)

  const add = useMutation({
    mutationFn: (p: LLMProvider) =>
      llm.addKey({
        key: key.trim(),
        label: label.trim() || null,
        provider: p.id,
      }),
    onSuccess: (result, p) => {
      setAdded(result)
      setKey('')
      setLabel('')
      toast.success(`Added ${p.label} Key ${result.hint}`)
      invalidate()
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  const reset = () => {
    setKey('')
    setLabel('')
    setAdded(null)
    add.reset()
  }
  const close = () => {
    reset()
    onClose()
  }

  return (
    <Dialog
      open={provider !== null}
      onOpenChange={(open) => !open && close()}
      className="max-w-2xl"
      title={provider ? `Add a ${provider.label} Key` : 'Add a Key'}
      footer={
        added ? (
          <>
            <Button variant="ghost" onClick={reset}>
              Add another
            </Button>
            <Button variant="primary" onClick={close}>
              Done
            </Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button
              variant="primary"
              type="submit"
              form="llm-add-key"
              loading={add.isPending}
              disabled={!key.trim()}
            >
              Add Key
            </Button>
          </>
        )
      }
    >
      {provider &&
        (added ? (
          <Notice title="Key added">
            {provider.label} Key <span className="num text-ink">{added.hint}</span>
            {added.label && <> labelled “{added.label}”</>} is in the pool.
          </Notice>
        ) : (
          <form
            id="llm-add-key"
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              if (key.trim()) add.mutate(provider)
            }}
          >
            <Button
              variant="secondary"
              className="w-fit"
              render={<a href={provider.onboardingUrl} target="_blank" rel="noopener noreferrer" />}
            >
              Get a free {provider.label} Key
              <ExternalLinkIcon />
            </Button>
            <Field label="API Key">
              <Input
                type="password"
                autoComplete="off"
                spellCheck={false}
                className="num"
                placeholder={provider.keyHint}
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
            </Field>
            <Field label="Label (optional)">
              <Input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="e.g. personal account"
                autoComplete="off"
              />
            </Field>
          </form>
        ))}
    </Dialog>
  )
}
