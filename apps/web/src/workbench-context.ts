import { createContext, useContext } from 'react'

import type {
  ModelQuota,
  RequestDraft,
  RequestResult,
  WorkspaceEvent,
  WorkspaceIdentity,
} from './types'

export interface WorkbenchContextValue {
  identity: WorkspaceIdentity
  draft: RequestDraft | null
  missingFields: string[]
  quota: ModelQuota
  events: WorkspaceEvent[]
  businessStatus: string
  error: string | null
  requestResult: RequestResult | null
  isSubmitting: boolean
  switchIdentity: (employeeId: string) => Promise<void>
  submit: () => Promise<void>
  reset: () => Promise<void>
}

export const WorkbenchContext = createContext<WorkbenchContextValue | null>(null)

export function useWorkbench(): WorkbenchContextValue {
  const value = useContext(WorkbenchContext)
  if (value === null) throw new Error('useWorkbench 必须在 WorkbenchRuntime 内使用')
  return value
}
