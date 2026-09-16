/**
 * The market a screen is pointed at: instrument type, region, delay and universe.
 *
 * Each screen keeps its own scope (exploring USA while relocating into Europe is an
 * ordinary morning), persisted so a screen reopens where it was left. What is legal in a
 * region comes from the platform's own settings schema, never a hardcoded table: CHN
 * offers only TOP2000U, and a simulation with an illegal universe still spends quota.
 */

import { useQuery } from '@tanstack/react-query'
import { useCallback } from 'react'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { auth } from '@/api/core'
import type { Scope } from '@/api/types'

export const DEFAULT_SCOPE: Scope = {
  instrumentType: 'EQUITY',
  region: 'USA',
  delay: 1,
  universe: 'TOP3000',
}

/** Regions the app does not offer for now. ALL: BRAIN serves it only 50 fields per request. */
const HIDDEN_REGIONS = new Set(['ALL'])

/** A stored scope, unless it points at a hidden region. */
const usable = (scope: Scope | undefined): Scope =>
  scope && !HIDDEN_REGIONS.has(scope.region) ? scope : DEFAULT_SCOPE

interface ScopeStore {
  scopes: Record<string, Scope>
  set: (key: string, scope: Scope) => void
}

const useScopeStore = create<ScopeStore>()(
  persist(
    (set) => ({
      scopes: {},
      set: (key, scope) => set((state) => ({ scopes: { ...state.scopes, [key]: scope } })),
    }),
    { name: 'alpha-harness-scope' },
  ),
)

/** One screen's scope (`key` = route or lab id) and a setter that merges changes. */
export function useScope(key: string): [Scope, (change: Partial<Scope>) => void] {
  const scope = useScopeStore((state) => usable(state.scopes[key]))
  const set = useScopeStore((state) => state.set)
  const update = useCallback(
    (change: Partial<Scope>) =>
      set(key, { ...usable(useScopeStore.getState().scopes[key]), ...change }),
    [key, set],
  )
  return [scope, update]
}

export interface Choice {
  value: string
  label: string
}

export interface ScopeOptions {
  regions: Choice[]
  delays: Choice[]
  universes: Choice[]
  neutralizations: Choice[]
  /** False until the platform has answered for this region and delay. */
  ready: boolean
  isError: boolean
}

const choices = (list: { value: string | number; label: string }[] | null | undefined): Choice[] =>
  (list ?? []).map((c) => ({
    value: String(c.value),
    label: String(c.label ?? c.value),
  }))

/** Legal regions, delays, universes and neutralizations for a scope, from BRAIN's schema. */
export function useScopeOptions(scope: Scope): ScopeOptions {
  const query = useQuery({
    queryKey: ['scope-options', scope.instrumentType, scope.region, scope.delay],
    queryFn: () =>
      auth.settingsOptions({
        instrumentType: scope.instrumentType,
        region: scope.region,
        delay: scope.delay,
      }),
    staleTime: 10 * 60 * 1000,
  })
  const fields = query.data?.fields
  const universes = choices(fields?.['universe']?.choices)
  return {
    regions: choices(fields?.['region']?.choices).filter((c) => !HIDDEN_REGIONS.has(c.value)),
    delays: choices(fields?.['delay']?.choices),
    universes,
    neutralizations: choices(fields?.['neutralization']?.choices),
    ready: universes.length > 0,
    isError: query.isError,
  }
}
