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

export type DemoRole = 'applicant' | 'manager' | 'data_owner'

export interface ApprovalStep {
  step_id: string
  step_order: number
  approver_id: string
  approver_role: string
  step_status: string
  comment: string | null
  decided_at: string | null
}

export interface ApprovalInboxItem {
  request_id: string
  approval_case_id: string
  approval_step_id: string
  step_order: number
  approver_role: string
  step_status: string
  approval_status: string
  requester_id: string
  requester_name: string
  entitlement_code: string
  entitlement_name: string
  risk_level: string
  duration_days: number
  justification: string
  submitted_at: string
}

export interface ApprovalInbox {
  actor: {
    employee_id: string
    name: string
    roles: string[]
  }
  items: ApprovalInboxItem[]
}

export interface RequestDetail {
  view_mode: 'read_only_replay'
  fault_mode: 'iam_failure' | 'iam_timeout' | null
  request: {
    request_id: string
    requester_id: string
    requester_name: string
    entitlement_code: string
    duration_days: number
    justification: string
    request_status: string
    confirmed_at: string
    created_at: string
  }
  entitlement: {
    code: string
    name: string
    system_code: string
    risk_level: string
    approval_policy: string
    owner_id: string | null
  }
  risk_review: {
    risk_level: string
    outcome: string
    summary: string
    findings: string[]
    citations: Array<{
      policy_code: string
      reason: string
      title: string | null
      content: string | null
    }>
  } | null
  approval: {
    approval_case_id: string
    approval_status: string
    created_at: string
    steps: ApprovalStep[]
  } | null
  provisioning: {
    provisioning_status: string
    provisioning_attempt_id: string | null
    attempt_count: number
    last_error: string | null
    updated_at: string | null
    access_granted: boolean
    grant_id: string | null
    starts_at: string | null
    expires_at: string | null
  }
  audit_events: Array<{
    audit_event_id: string
    event_type: string
    actor_type: string
    actor_id: string | null
    details: Record<string, unknown>
    created_at: string
  }>
}
