/**
 * Data Explorer (spec §4.2): a synced market's fields, narrowed in More filters down to whole
 * categories, whole subcategories or single datasets. Syncing lives in Sync with BRAIN.
 */

import { Link, useNavigate, useParams } from '@tanstack/react-router'
import { useEffect } from 'react'
import { fmt } from '@/lib/format'
import { useScope } from '@/lib/scope'
import { useDatasetPick } from '@/screens/data/dataset-pick'
import { Button, Empty, LINK, Metric, Page, PageHeader, Panel } from '@/ui/kit'
import { FieldsTab } from './fields'
import { MarketBar } from './market'

export function DataScreen() {
  const { tab } = useParams({ from: '/data/$tab' })
  const [scope, update] = useScope('data')
  const picking = useDatasetPick((s) => s.active)
  const follow = useDatasetPick((s) => s.follow)

  // A pick follows the market shown here: another region or delay empties it.
  useEffect(() => {
    if (picking) follow(scope)
  }, [picking, follow, scope])

  return (
    <Page>
      <PageHeader
        title="Data Explorer"
        description="Browse, filter and compare the Data Fields you synced from BRAIN, locally."
      />
      {picking && <PickBar />}
      <MarketBar scope={scope} update={update} />
      {tab === 'fields' ? (
        <FieldsTab scope={scope} />
      ) : (
        <Panel>
          <Empty title={`There is no “${tab}” tab`}>
            <Link to="/data/$tab" params={{ tab: 'fields' }} className={LINK}>
              Open Fields
            </Link>
          </Empty>
        </Panel>
      )}
    </Page>
  )
}

/** While a lab picks datasets: how many are ticked, where to tick them, and the way back to it. */
function PickBar() {
  const navigate = useNavigate()
  const count = useDatasetPick((s) => s.ids.length)
  const back = (done: boolean) => {
    const pick = useDatasetPick.getState()
    const to = pick.from
    if (done) pick.finish()
    else pick.cancel()
    void navigate({ to })
  }

  return (
    <div className="sticky top-0 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-hairline bg-surface-1 p-3">
      <Metric boxed size="sm" label="Datasets Selected" value={fmt.int(count)} />
      <span className="min-w-0 flex-1 text-body-compact text-pretty text-ink-subtle">
        Tick whole categories, whole subcategories or single datasets in More filters.
      </span>
      <Button variant="ghost" onClick={() => back(false)}>
        Cancel
      </Button>
      <Button variant="primary" disabled={count === 0} onClick={() => back(true)}>
        Done
      </Button>
    </div>
  )
}
