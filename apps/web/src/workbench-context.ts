import { createContext, useContext } from 'react'

import type {
  RequestDraft,
  RequestResult,
  WorkspaceEvent,
  WorkspaceIdentity,
  DemoSession,
  EntitlementSelectionResult,
  ConnectionState,
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
  connectionState: ConnectionState
  submit: () => Promise<void>
  selectEntitlement: (entitlementId: string) => Promise<EntitlementSelectionResult>
  isSubmitting: boolean
}

export const WorkbenchContext = createContext<WorkbenchContextValue | null>(null)

export function useWorkbench(): WorkbenchContextValue {
  const value = useContext(WorkbenchContext)
  if (value === null) throw new Error('useWorkbench 必须在 WorkbenchRuntime 内使用')
  return value
}
