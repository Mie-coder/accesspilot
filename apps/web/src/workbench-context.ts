import { createContext, useContext } from 'react'

import type {
  DecisionPacket,
  RequestDraft,
  RequestResult,
  RequestDetail,
  WorkspaceEvent,
  WorkspaceIdentity,
  EntitlementSelectionResult,
  ConnectionState,
} from './types'

export interface WorkbenchContextValue {
  identity: WorkspaceIdentity
  draft: RequestDraft | null
  missingFields: string[]
  events: WorkspaceEvent[]
  businessStatus: string
  error: string | null
  requestResult: RequestResult | null
  decisionPacket: DecisionPacket | null
  decisionPacketError: string | null
  approvalCase: RequestDetail['approval']
  approvalError: string | null
  retryableInterruption: boolean
  connectionState: ConnectionState
  submit: () => Promise<void>
  retryDecisionPacket: () => Promise<void>
  startApproval: () => Promise<void>
  selectEntitlement: (entitlementId: string) => Promise<EntitlementSelectionResult>
  isSubmitting: boolean
  isGeneratingDecisionPacket: boolean
  isStartingApproval: boolean
}

export const WorkbenchContext = createContext<WorkbenchContextValue | null>(null)

export function useWorkbench(): WorkbenchContextValue {
  const value = useContext(WorkbenchContext)
  if (value === null) throw new Error('useWorkbench 必须在 WorkbenchRuntime 内使用')
  return value
}
