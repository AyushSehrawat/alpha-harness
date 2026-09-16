/**
 * Resizable split panes on react-resizable-panels: wide screens get a draggable hairline
 * gutter, narrow screens stack as a column. Each layout persists per id in localStorage.
 */

import { Children, type ReactNode, useSyncExternalStore } from 'react'
import { Group, Panel, Separator, useDefaultLayout } from 'react-resizable-panels'
import { cn } from '@/lib/cn'

export const WIDE = '(min-width: 1024px)'

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      const list = window.matchMedia(query)
      list.addEventListener('change', notify)
      return () => list.removeEventListener('change', notify)
    },
    () => window.matchMedia(query).matches,
  )
}

/** A number is pixels; a unitless string is a percentage of the group. */
interface PaneSize {
  default?: number | string
  min?: number | string
  max?: number | string
}

/**
 * The drag handle. `gutter` keeps the 12px canvas gap between panels with a hairline in it;
 * `edge` sits on an existing border (the sidebar) with a wider invisible hit area.
 */
export function ResizeHandle({ variant = 'gutter' }: { variant?: 'gutter' | 'edge' }) {
  const edge = variant === 'edge'
  return (
    <Separator
      className={cn(
        'group relative flex shrink-0 cursor-col-resize justify-center self-stretch outline-none',
        edge ? 'z-10 -ml-px w-px' : 'w-3',
      )}
    >
      <span
        className={cn(
          'h-full w-px transition-colors group-data-[separator=active]:w-0.5 group-data-[separator=active]:bg-primary-focus group-data-[separator=focus]:w-0.5 group-data-[separator=focus]:bg-primary-focus group-data-[separator=hover]:w-0.5 group-data-[separator=hover]:bg-hairline-strong',
          edge ? 'bg-transparent' : 'bg-hairline',
        )}
      />
      {edge && <span aria-hidden className="absolute inset-y-0 -right-1.5 -left-1.5" />}
    </Separator>
  )
}

/** Two panes side by side from 1024px up, stacked below it. Takes exactly two children. */
export function SplitPane({
  id,
  first,
  second,
  children,
  className,
}: {
  id: string
  first?: PaneSize
  second?: PaneSize
  children: ReactNode
  className?: string
}) {
  const wide = useMediaQuery(WIDE)
  const [a, b] = Children.toArray(children)
  if (!wide)
    return (
      <div className={cn('flex min-w-0 flex-col gap-3', className)}>
        {a}
        {b}
      </div>
    )
  return (
    <Split id={id} first={first} second={second} className={className}>
      {a}
      {b}
    </Split>
  )
}

function Split({
  id,
  first,
  second,
  children,
  className,
}: {
  id: string
  first?: PaneSize | undefined
  second?: PaneSize | undefined
  children: ReactNode
  className?: string | undefined
}) {
  const [a, b] = Children.toArray(children)
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: `ah-panels:${id}`,
    storage: localStorage,
    onlySaveAfterUserInteractions: true,
  })
  return (
    <Group
      id={id}
      orientation="horizontal"
      defaultLayout={defaultLayout}
      onLayoutChanged={onLayoutChanged}
      className={cn('min-w-0', className)}
    >
      <Panel
        id="first"
        defaultSize={first?.default}
        minSize={first?.min}
        maxSize={first?.max}
        className="min-w-0"
      >
        {a}
      </Panel>
      <ResizeHandle />
      <Panel
        id="second"
        defaultSize={second?.default}
        minSize={second?.min}
        maxSize={second?.max}
        className="min-w-0"
      >
        {b}
      </Panel>
    </Group>
  )
}
