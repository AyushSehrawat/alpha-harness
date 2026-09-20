/** Tasks: what the labs added. Only the Tasks tab runs them. */

import type { components } from '@/api/generated'
import { http, qs } from '@/api/http'

type Schemas = components['schemas']

/** IDLE: not started. QUEUED: run, waiting for its cores to fit in the free slots. */
export type TaskStatus = Schemas['StudyStatus']
export type LabTask = Schemas['LabTask']
export type LabTasks = Schemas['LabTasks']
export type RankedAlpha = Omit<Schemas['RankedAlpha'], 'settings'> & {
  settings: { universe?: string; neutralization?: string; [key: string]: unknown } | null
}

/** A submittable Alpha from any task, with the task that found it. */
export type TaskAlpha = Omit<Schemas['TaskAlpha'], 'settings'> & {
  settings: RankedAlpha['settings']
}

const B = '/api/lab-tasks'

export const labTasks = {
  list: () => http.get<LabTasks>(B),
  /** Spends simulation quota. */
  runAll: () => http.post<LabTasks>(`${B}/run-all`),
  /** Spends simulation quota. Also resumes a paused task. */
  run: (id: number) => http.post<LabTask>(`${B}/${id}/run`),
  pause: (id: number) => http.post<LabTask>(`${B}/${id}/pause`),
  stop: (id: number) => http.post<LabTask>(`${B}/${id}/stop`),
  change: (id: number, body: { cores?: number; simulations?: number }) =>
    http.patch<LabTask>(`${B}/${id}`, body),
  remove: (id: number) => http.del<Schemas['TaskRemoved']>(`${B}/${id}`),
  top: (id: number, limit = 50) => http.get<RankedAlpha[]>(`${B}/${id}/top${qs({ limit })}`),
  /** Every Alpha from every task that nothing refuses: each check PASS, WARNING or PENDING. */
  submittable: () => http.get<TaskAlpha[]>(`${B}/submittable`),
}
