export interface RequestDraft {
  employee_id: string | null
  entitlement_id: string | null
  duration_days: number | null
  justification: string | null
  confirmed: boolean
}

export interface ModelQuota {
  used: number
  limit: number
  remaining: number
}

export interface ChatTurn {
  assistant_message: string
  draft: RequestDraft
  missing_fields: string[]
  phase: string
  business_status: string
  quota: ModelQuota
}

export interface WorkspaceEvent {
  id: number
  type: string
  payload: Record<string, unknown>
}

export interface RequestResult {
  request_id: string
  request_status: string
}

export interface WorkspaceSnapshot {
  draft: RequestDraft | null
  quota: ModelQuota
  events: WorkspaceEvent[]
  lastEventId: number
}
