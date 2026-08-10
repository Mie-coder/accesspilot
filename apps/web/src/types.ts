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
  retry_consumed: number
}
export interface DemoSession {
  demo_mode_enabled: boolean
  demo_session_active: boolean
  employee_id?: string
  fault_mode: 'iam_failure' | 'iam_timeout' | null
}

export type ConnectionState = 'connected' | 'reconnecting'


export interface ChatTurn {
  assistant_message: string
  draft: RequestDraft
  missing_fields: string[]
  phase: string
  business_status: string
  intent: string
  security_flagged: boolean
  tool_results: Array<Record<string, unknown>>
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

export interface WorkspaceIdentity {
  employee_id: string
  name: string
  department: string
  roles: string[]
}

export interface WorkspaceSnapshot {
  identity: WorkspaceIdentity
  draft: RequestDraft | null
  demoSession: DemoSession
  events: WorkspaceEvent[]
  lastEventId: number
}

/** Facts returned by the identity-scoped access overview endpoint. */
export type AccessOverviewState =
  | 'eligible'
  | 'owned'
  | 'pending'
  | 'expiring_soon'
  | 'expired'

export interface AccessOverviewItem {
  state: AccessOverviewState
  code: string
  name: string
  system_code: string
  system_name: string
  risk_level: string
  max_duration_days: number | null
  approval_policy: string
  request_id: string | null
  request_status: string | null
  grant_id: string | null
  starts_at: string | null
  expires_at: string | null
  next_step: string
}

export interface AccessOverview {
  items: AccessOverviewItem[]
}

export type EntitlementResolutionStatus = 'matched' | 'ambiguous' | 'no_match'

export interface EntitlementCandidate {
  code: string
  name: string
  system_code: string
  system_name: string
  risk_level: string
  max_duration_days: number | null
  approval_policy: string
}

export interface EntitlementResolution {
  status: EntitlementResolutionStatus
  target_field: 'entitlement_id'
  query: string
  candidates: EntitlementCandidate[]
  eligible_access: EntitlementCandidate[]
}

export interface DraftPreviewResponse {
  draft: RequestDraft | null
  missing_fields: string[]
  is_complete: boolean
  can_enter_approval: boolean
  entitlement_resolution: EntitlementResolution | null
  issues: Array<{ code: string; message: string }>
}

export type EntitlementSelectionStatus = 'revalidated' | 'rejected'

export interface EntitlementSelectionResult {
  status: EntitlementSelectionStatus
  code: string
  message: string
  previous_confirmation_invalidated: boolean
}

export interface PolicyCatalogItem {
  policy_code: string
  title: string
  content: string
  version: string
  source: string
  similarity?: number | null
}

export type PolicyAnswerStatus =
  | 'grounded'
  | 'insufficient_evidence'
  | 'retrieval_unavailable'

export interface PolicyEvidence {
  policy_code: string
  title: string
  content: string
  version: string
  source: string
  similarity: number | null
}

export interface PolicyAnswer {
  status: PolicyAnswerStatus
  answer: string
  evidence: PolicyEvidence[]
  next_step: string
}


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
