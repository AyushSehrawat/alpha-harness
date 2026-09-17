/** Settings Sampler: where one proven expression could also run, and queueing it there. */

import type { components } from '@/api/generated'
import { http } from '@/api/http'

type Schemas = components['schemas']

export type SettingsPlan = Schemas['SettingsPlan']
export type RegionPlan = Schemas['RegionPlan']
export type MarketRow = Schemas['MarketRow']
export type Pair = Schemas['Pair']

export interface MarketPick {
  region: string
  delay: number
  universe: string
}

export interface SampleRequest {
  alphaId: string
  /** Empty means every market the plan offers; likewise for each filter below. */
  markets: MarketPick[]
  neutralizations: string[]
  pairs: Pair[]
  /** Concurrent slots the task holds; ten simulations ride in each. */
  cores: number
}

const B = '/api/tools/settings-sampler'

export const settingsSampler = {
  /** Free: reads the Alpha and the catalog, simulates nothing. */
  preview: (alphaId: string) => http.post<SettingsPlan>(`${B}/preview`, { alphaId }),
  addTask: (body: SampleRequest) => http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}

export const marketKey = (m: MarketPick) => `${m.region}|${m.delay}|${m.universe}`
export const pairLabel = (p: Pair) =>
  p.maxTrade === 'OFF' && p.maxPosition === 'OFF'
    ? 'Neither'
    : p.maxTrade === 'ON'
      ? 'Max Trade'
      : 'Max Position'
