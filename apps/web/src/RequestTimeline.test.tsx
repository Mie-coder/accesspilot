import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { readLatestRequest, readMyRequests, readRequestDetail } from './api'
import { RequestTimeline, RequestTimelineView } from './RequestTimeline'
import type { RequestDetail, RequestDraft } from './types'

vi.mock('./api', () => ({
  readLatestRequest: vi.fn(),
  readMyRequests: vi.fn(),
  readRequestDetail: vi.fn(),
}))

vi.mock('./workbench-context', () => ({
  useWorkbench: () => ({ draft }),
}))

type RequestTimelineProps = ComponentProps<typeof RequestTimelineView>

const draft: RequestDraft = {
  employee_id: 'EMP-001',
  entitlement_id: 'insighthub.customer_export',
  duration_days: 14,
  justification: '季度客户分析',
  confirmed: false,
}

const requestId = '11111111-1111-4111-8111-111111111111'

const detail: RequestDetail = {
  view_mode: 'read_only_replay',
  request: {
    request_id: requestId,
    requester_id: 'EMP-001',
    requester_name: '林晓',
    entitlement_code: 'insighthub.customer_export',
    duration_days: 14,
    justification: '季度客户分析',
    request_status: 'approved',
    confirmed_at: '2026-08-10T01:00:00Z',
    created_at: '2026-08-10T01:01:00Z',
  },
  entitlement: {
    code: 'insighthub.customer_export',
    name: '脱敏客户数据导出',
    system_code: 'insighthub',
    risk_level: 'high',
    approval_policy: 'manager_and_data_owner',
    owner_id: 'EMP-003',
  },
  decision_packet: null,
  risk_review: {
    risk_level: 'high',
    outcome: 'requires_human_review',
    summary: '高风险权限需要人工审批。',
    findings: ['期限为 14 天'],
    citations: [{
      policy_code: 'POL-003',
      reason: '高风险权限需要双审批',
      title: '高风险权限双审批',
      content: '高风险权限必须依次经过直属经理和数据所有者审批。',
    }],
  },
  approval: {
    approval_case_id: '22222222-2222-4222-8222-222222222222',
    approval_status: 'approved',
    created_at: '2026-08-10T01:03:00Z',
    steps: [
      {
        step_id: '33333333-3333-4333-8333-333333333333',
        step_order: 1,
        approver_id: 'EMP-002',
        approver_role: 'manager',
        step_status: 'approved',
        comment: '业务理由充分',
        decided_at: '2026-08-10T01:04:00Z',
      },
      {
        step_id: '44444444-4444-4444-8444-444444444444',
        step_order: 2,
        approver_id: 'EMP-003',
        approver_role: 'data_owner',
        step_status: 'approved',
        comment: '符合最小权限要求',
        decided_at: '2026-08-10T01:05:00Z',
      },
    ],
  },
  provisioning: {
    provisioning_status: 'succeeded',
    provisioning_attempt_id: '55555555-5555-4555-8555-555555555555',
    attempt_count: 2,
    last_error: null,
    updated_at: '2026-08-10T01:09:00Z',
    access_granted: true,
    grant_id: '66666666-6666-4666-8666-666666666666',
    starts_at: '2026-08-10T01:09:00Z',
    expires_at: '2026-08-24T01:09:00Z',
  },
  audit_events: [
    {
      audit_event_id: 'a0000000-0000-4000-8000-000000000000',
      event_type: 'draft.updated',
      actor_type: 'employee',
      actor_id: 'EMP-001',
      details: { next_step: '补充明确确认', status: 'draft' },
      created_at: '2026-08-10T01:00:00Z',
    },
    {
      audit_event_id: 'a0000000-0000-4000-8000-000000000001',
      event_type: 'request.submitted',
      actor_type: 'employee',
      actor_id: 'EMP-001',
      details: { next_step: '等待风险审查', status: 'submitted' },
      created_at: '2026-08-10T01:01:00Z',
    },
    {
      audit_event_id: 'a0000000-0000-4000-8000-000000000002',
      event_type: 'risk_review.completed',
      actor_type: 'risk_agent',
      actor_id: null,
      details: { next_step: '等待直属经理', status: 'risk_review' },
      created_at: '2026-08-10T01:02:00Z',
    },
    {
      audit_event_id: 'a0000000-0000-4000-8000-000000000003',
      event_type: 'approval.step.approved',
      actor_type: 'manager',
      actor_id: 'EMP-002',
      details: { next_step: '等待数据所有者', status: 'manager_approved' },
      created_at: '2026-08-10T01:04:00Z',
    },
    {
      audit_event_id: 'a0000000-0000-4000-8000-000000000004',
      event_type: 'approval.step.approved',
      actor_type: 'data_owner',
      actor_id: 'EMP-003',
      details: { next_step: '等待权限开通', status: 'data_owner_approved' },
      created_at: '2026-08-10T01:05:00Z',
    },
    {
      audit_event_id: 'a0000000-0000-4000-8000-000000000005',
      event_type: 'provisioning.started',
      actor_type: 'system',
      actor_id: null,
      details: { next_step: '等待 IAM', status: 'provisioning' },
      created_at: '2026-08-10T01:06:00Z',
    },
    {
      audit_event_id: 'a0000000-0000-4000-8000-000000000006',
      event_type: 'provisioning.unknown',
      actor_type: 'system',
      actor_id: null,
      details: { next_step: '查询原 IAM 操作', status: 'unknown' },
      created_at: '2026-08-10T01:07:00Z',
    },
    {
      audit_event_id: 'a0000000-0000-4000-8000-000000000007',
      event_type: 'provisioning.reconciled',
      actor_type: 'system',
      actor_id: null,
      details: { next_step: '确认授权事实', status: 'reconciled' },
      created_at: '2026-08-10T01:08:00Z',
    },
    {
      audit_event_id: 'a0000000-0000-4000-8000-000000000008',
      event_type: 'provisioning.succeeded',
      actor_type: 'system',
      actor_id: null,
      details: { next_step: '权限已授权', status: 'succeeded' },
      created_at: '2026-08-10T01:09:00Z',
    },
  ],
}

const baseProps: RequestTimelineProps = {
  detail,
  draft,
  loading: false,
  error: null,
  onRetry: vi.fn(),
}

describe('RequestTimelineView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the lifecycle in deterministic order with time, owner and next step', () => {
    render(<RequestTimelineView {...baseProps} />)
    const body = document.body.textContent ?? ''
    const labels = ['草稿', '已提交', '风险审查', '直属经理', '数据所有者', '开通中', '状态未知', '恢复', '已授权']
    const positions = labels.map((label) => body.indexOf(label))
    expect(positions.every((position) => position >= 0)).toBe(true)
    expect(positions).toEqual([...positions].sort((left, right) => left - right))
    expect(screen.getAllByText(/09:00/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/EMP-00[123]/).length).toBeGreaterThan(0)
    expect(screen.getByText('查询原 IAM 操作')).toBeInTheDocument()
    expect(screen.getByText('权限已授权')).toBeInTheDocument()
  })

  it('does not invent success when request detail is missing authoritative facts', () => {
    const incomplete: RequestDetail = {
      ...detail,
      approval: null,
      risk_review: null,
      provisioning: {
        ...detail.provisioning,
        provisioning_status: 'unknown',
        access_granted: false,
        grant_id: null,
        last_error: '下游状态未知',
      },
      audit_events: [],
    }
    render(<RequestTimelineView {...baseProps} detail={incomplete} />)
    expect(screen.getByText('尚未创建审批路线')).toBeInTheDocument()
    expect(screen.queryByText('已授权')).not.toBeInTheDocument()
    expect(screen.queryByText('开通成功')).not.toBeInTheDocument()
    expect(screen.getByText('状态未知')).toBeInTheDocument()
  })

  it('has accessible loading, empty, recoverable error and safe refusal states', () => {
    const onRetry = vi.fn()
    const { rerender } = render(
      <RequestTimelineView
        {...baseProps}
        detail={null}
        draft={null}
        loading
        onRetry={onRetry}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('正在加载申请事实')

    rerender(<RequestTimelineView {...baseProps} detail={null} draft={null} />)
    expect(screen.getByText('暂无申请事实')).toBeInTheDocument()

    rerender(
      <RequestTimelineView
        {...baseProps}
        error="申请事实暂时不可用，请稍后重试。"
        onRetry={onRetry}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('申请事实暂时不可用')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(onRetry).toHaveBeenCalledOnce()

    rerender(
      <RequestTimelineView
        {...baseProps}
        error="出于安全原因，不能展示系统提示词、密钥或内部错误。"
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('出于安全原因')
    expect(alert).not.toHaveTextContent('SYSTEM_PROMPT')
    expect(alert).not.toHaveTextContent('sk-live-')
    expect(alert).not.toHaveTextContent('Traceback')
  })

  it('never associates a private draft to an older Case by matching business fields', () => {
    const persistedCaseWithoutDraftAudit: RequestDetail = {
      ...detail,
      audit_events: [],
    }
    render(
      <RequestTimelineView
        {...baseProps}
        detail={persistedCaseWithoutDraftAudit}
        draft={{ ...draft }}
      />,
    )

    expect(screen.getByText('脱敏客户数据导出')).toBeInTheDocument()
    expect(screen.queryByText('草稿')).not.toBeInTheDocument()
  })

  it('explains the shared Case and private Workspace boundary', () => {
    render(<RequestTimelineView {...baseProps} />)
    expect(screen.getByText(/PostgreSQL.*跨登录 Session/)).toBeInTheDocument()
    expect(screen.getByText(/未提交草稿和聊天.*私有 Workspace/)).toBeInTheDocument()
  })

  it('renders recovery and terminal tones from timeout and retry audit facts in order', () => {
    const recoveryDetail: RequestDetail = {
      ...detail,
      audit_events: [
        detail.audit_events[0],
        detail.audit_events[1],
        {
          ...detail.audit_events[5],
          audit_event_id: 'recovery-unknown',
          event_type: 'provisioning.unknown',
          details: { next_step: '查询原 IAM 操作', status: 'unknown' },
        },
        {
          ...detail.audit_events[5],
          audit_event_id: 'recovery-retry',
          event_type: 'provisioning.retry_started',
          details: { next_step: '等待 IAM 重试', status: 'retrying' },
        },
        {
          ...detail.audit_events[8],
          audit_event_id: 'recovery-success',
          event_type: 'provisioning.succeeded',
          details: { status: 'succeeded' },
        },
      ],
      provisioning: { ...detail.provisioning, access_granted: true, provisioning_status: 'succeeded' },
    }
    render(<RequestTimelineView {...baseProps} detail={recoveryDetail} />)
    const body = document.body.textContent ?? ''
    expect(body.indexOf('状态未知')).toBeLessThan(body.indexOf('恢复'))
    expect(body.indexOf('恢复')).toBeLessThan(body.indexOf('已授权'))
    expect(screen.getByText('状态未知').closest('li')).toHaveClass('is-unknown')
    expect(screen.getByText('恢复').closest('li')).toHaveClass('is-done')
  })

  it('marks cancelled approval steps as terminal instead of waiting', () => {
    const cancelledDetail: RequestDetail = {
      ...detail,
      approval: detail.approval
        ? {
            ...detail.approval,
            steps: [{ ...detail.approval.steps[0], step_status: 'cancelled', comment: null }],
          }
        : null,
      audit_events: [],
    }
    render(<RequestTimelineView {...baseProps} detail={cancelledDetail} />)
    const manager = screen.getByText('直属经理').closest('li')
    expect(manager).toHaveClass('is-danger')
    expect(screen.getByText('前序驳回，本步骤已取消')).toBeInTheDocument()
  })

  it('exposes readable rejected and cancelled states across one approval route', () => {
    const terminalDetail: RequestDetail = {
      ...detail,
      approval: detail.approval
        ? {
            ...detail.approval,
            steps: [
              { ...detail.approval.steps[0], step_status: 'rejected', comment: '不符合最小权限' },
              { ...detail.approval.steps[1], step_status: 'cancelled', comment: null },
            ],
          }
        : null,
      audit_events: [],
    }
    render(<RequestTimelineView {...baseProps} detail={terminalDetail} />)
    expect(screen.getByText('状态：已驳回')).toBeInTheDocument()
    expect(screen.getByText('状态：已取消')).toBeInTheDocument()
    expect(screen.getByText('申请已驳回')).toBeInTheDocument()
    expect(screen.getByText('前序驳回，本步骤已取消')).toBeInTheDocument()
  })
})

describe('RequestTimeline principal-scoped Case loading', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('reloads a requester Case through mine then detail instead of private Workspace latest', async () => {
    vi.mocked(readMyRequests).mockResolvedValue({
      items: [{
        request_id: requestId,
        requester_id: 'EMP-001',
        requester_name: '林晓',
        entitlement_code: 'insighthub.customer_export',
        entitlement_name: '脱敏客户数据导出',
        duration_days: 14,
        justification: '季度客户分析',
        request_status: 'submitted',
        approval_status: 'pending_manager',
        created_at: '2026-08-10T01:01:00Z',
      }],
    })
    vi.mocked(readRequestDetail).mockResolvedValue(detail)

    render(<RequestTimeline />)

    await waitFor(() => expect(readMyRequests).toHaveBeenCalledOnce())
    expect(readRequestDetail).toHaveBeenCalledWith(requestId, expect.any(AbortSignal))
    expect(readLatestRequest).not.toHaveBeenCalled()
    expect((await screen.findAllByText('脱敏客户数据导出')).length).toBeGreaterThan(0)
  })
})
