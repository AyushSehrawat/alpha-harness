/**
 * Search Lab: choose datasets, cores and simulations, then run the search as a task.
 * Request bodies are snake_case.
 */

import type { components } from '@/api/generated'
import { http } from '@/api/http'

export interface SearchLabRequest {
  region: string
  delay: number
  universe?: string | null
  dataset_ids: string[]
  vector_operators: string[]
  decay: number
  cores: number
  /** Needed to add a task; a preview ignores it. */
  simulations?: number
}

type Schemas = components['schemas']

export type SearchLabOptions = Schemas['Options']
export type SearchLabPreview = Schemas['Preview']
export type QuickRun = Schemas['QuickRun']

const B = '/api/search-lab'

export const searchLab = {
  /** Reads the account's operators, syncing them from BRAIN when missing. */
  options: () => http.get<SearchLabOptions>(`${B}/options`),
  /** Free; queues nothing. */
  preview: (body: SearchLabRequest) => http.post<SearchLabPreview>(`${B}/preview`, body),
  /** Adds the search to Tasks and queues it to run. */
  runTask: (body: SearchLabRequest & { simulations: number }) =>
    http.post<Schemas['AddedTask']>(`${B}/tasks?run=true`, body),
  /**
   * The Dashboard's one click: today's unclaimed simulations, run now, over `dataset_ids`
   * or, when empty, the pyramids not yet formulated this quarter.
   */
  quick: (body: Schemas['QuickRequest']) => http.post<QuickRun>(`${B}/quick`, body),
}
