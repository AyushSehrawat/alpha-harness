/** Research Labs: every lab, two by two. */

import { Link } from '@tanstack/react-router'
import {
  ArrowUpRightIcon,
  BlocksIcon,
  DnaIcon,
  FlaskConicalIcon,
  type LucideIcon,
  SearchIcon,
  ZapIcon,
} from 'lucide-react'
import { LAB_TABS } from '@/shell/nav'
import { Page, PageHeader } from '@/ui/kit'

const LABS = 4

const ICONS: Record<(typeof LAB_TABS)[number]['tab'], LucideIcon> = {
  search: SearchIcon,
  template: BlocksIcon,
  evolution: DnaIcon,
  'power-pool': ZapIcon,
}

const number = (index: number) => String(index + 1).padStart(2, '0')

export function ResearchLabsScreen() {
  return (
    <Page>
      <PageHeader title="Research Labs" />
      <div className="grid gap-3 sm:grid-cols-2">
        {LAB_TABS.map((lab, index) => {
          const Icon = ICONS[lab.tab]
          return (
            <Link
              key={lab.tab}
              to={lab.to}
              className="panel-highlight group flex min-h-56 flex-col justify-between rounded-lg border border-hairline bg-surface-1 p-6 transition-colors hover:border-hairline-strong hover:bg-surface-2"
            >
              <div className="flex items-start justify-between">
                <span className="flex size-12 items-center justify-center rounded-md border border-hairline-strong bg-surface-2 text-ink-muted transition-colors group-hover:border-primary group-hover:text-primary">
                  <Icon className="size-6" aria-hidden />
                </span>
                <ArrowUpRightIcon
                  className="size-5 text-ink-tertiary transition-colors group-hover:text-ink"
                  aria-hidden
                />
              </div>
              <div className="flex flex-col gap-1">
                <span className="num text-body-compact text-ink-subtle">{number(index)}</span>
                <h2 className="text-headline font-semibold text-balance text-ink">{lab.label}</h2>
              </div>
            </Link>
          )
        })}
        {/* Clamped: a fifth lab would make this negative, and `Array.from` throws on that. */}
        {Array.from({ length: Math.max(0, LABS - LAB_TABS.length) }, (_, i) => (
          <div
            key={i}
            className="flex min-h-56 flex-col justify-between rounded-lg border border-dashed border-hairline p-6 text-ink-subtle"
          >
            <span className="flex size-12 items-center justify-center rounded-md border border-dashed border-hairline">
              <FlaskConicalIcon className="size-6" aria-hidden />
            </span>
            <div className="flex flex-col gap-1.5">
              <span className="num text-body-compact">{number(LAB_TABS.length + i)}</span>
              <h2 className="text-headline text-balance text-ink-subtle">Coming Soon</h2>
            </div>
          </div>
        ))}
      </div>
    </Page>
  )
}
