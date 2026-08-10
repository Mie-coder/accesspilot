import { createContext, useContext } from 'react'

import type {
  RequestDraft,
  RequestResult,
  WorkspaceEvent,
  WorkspaceIdentity,
  DemoSession,
} from './types'

export interface WorkbenchContextValue {
  identity: WorkspaceIdentity
  draft: RequestDraft | null
  missingFields: string[]
  demoSession: DemoSession
  events: WorkspaceEvent[]
  businessStatus: string
  error: string | null
  requestResult: RequestResult | null
  retryableInterruption: boolean
  submit: () => Promise<void>
  isSubmitting: boolean
}

export const WorkbenchContext = createContext<WorkbenchContextValue | null>(null)

export function useWorkbench(): WorkbenchContextValue {
  const value = useContext(WorkbenchContext)
  if (value === null) throw new Error('useWorkbench 必须在 WorkbenchRuntime 内使用')
  return value
}
