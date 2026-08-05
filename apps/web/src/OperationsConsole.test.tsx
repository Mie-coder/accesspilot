import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { readApprovalInbox, readRequestDetail } from './api'
import { OperationsConsole, OperationsView } from './OperationsConsole'
import type { ApprovalInbox, RequestDetail } from './types'

vi.mock('./api', () => ({
  decideApproval: vi.fn(),
  provisionRequest: vi.fn(),
  readApprovalInbox: vi.fn(),
  readRequestDetail: vi.fn(),
  recoverProvisioning: vi.fn(),
  setFaultMode: vi.fn(),
  startApproval: vi.fn(),
}))

const requestId = '11111111-1111-4111-8111-111111111111'
const caseId = '22222222-2222-4222-8222-222222222222'

const inbox: ApprovalInbox = {
  actor: { employee_id: 'EMP-002', name: '周敏', roles: ['manager'] },
  items: [
    {
      request_id: requestId,
      approval_case_id: caseId,
      approval_step_id: '33333333-3333-4333-8333-333333333333',
      step_order: 1,
      approver_role: 'manager',
      step_status: 'pending',
      approval_status: 'pending_manager',
      requester_id: 'EMP-001',
      requester_name: '林晓',
      entitlement_code: 'insighthub.customer_export',
      entitlement_name: '脱敏客户数据导出',
      risk_level: 'high',
      duration_days: 14,
      justification: '季度客户分析',
      submitted_at: '2026-08-05T01:00:00Z',
    },
  ],
}

function detail(overrides: Partial<RequestDetail> = {}): RequestDetail {
  return {
    view_mode: 'read_only_replay',
    fault_mode: null,
    request: {
      request_id: requestId,
      requester_id: 'EMP-001',
      requester_name: '林晓',
      entitlement_code: 'insighthub.customer_export',
      duration_days: 14,
      justification: '季度客户分析',
      request_status: 'submitted',
      confirmed_at: '2026-08-05T01:00:00Z',
      created_at: '2026-08-05T01:00:00Z',
    },
    entitlement: {
      code: 'insighthub.customer_export',
      name: '脱敏客户数据导出',
      system_code: 'insighthub',
      risk_level: 'high',
      approval_policy: 'manager_and_data_owner',
      owner_id: 'EMP-003',
    },
    risk_review: {
      risk_level: 'high',
      outcome: 'requires_human_review',
      summary: '高风险权限需要人工审批。',
      findings: ['期限为 14 天'],
      citations: [
        {
          policy_code: 'POL-003',
          reason: '要求双审批',
          title: '高风险权限双审批',
          content: '高风险权限必须依次审批。',
        },
      ],
    },
    approval: {
      approval_case_id: caseId,
      approval_status: 'pending_manager',
      created_at: '2026-08-05T01:01:00Z',
      steps: [
        {
          step_id: '33333333-3333-4333-8333-333333333333',
          step_order: 1,
          approver_id: 'EMP-002',
          approver_role: 'manager',
          step_status: 'pending',
          comment: null,
          decided_at: null,
        },
        {
          step_id: '44444444-4444-4444-8444-444444444444',
          step_order: 2,
          approver_id: 'EMP-003',
          approver_role: 'data_owner',
          step_status: 'waiting',
          comment: null,
          decided_at: null,
        },
      ],
    },
    provisioning: {
      provisioning_status: 'not_started',
      provisioning_attempt_id: null,
      attempt_count: 0,
      last_error: null,
      updated_at: null,
      access_granted: false,
      grant_id: null,
      starts_at: null,
      expires_at: null,
    },
    audit_events: [
      {
        audit_event_id: '55555555-5555-4555-8555-555555555555',
        event_type: 'request.submitted',
        actor_type: 'employee',
        actor_id: 'EMP-001',
        details: {},
        created_at: '2026-08-05T01:00:00Z',
      },
    ],
    ...overrides,
  }
}

const callbacks = {
  onRefresh: vi.fn(),
  onSelectRequest: vi.fn(),
  onStartApproval: vi.fn(),
  onDecision: vi.fn(),
  onFaultMode: vi.fn(),
  onProvision: vi.fn(),
  onRecover: vi.fn(),
  onRetryProvision: vi.fn(),
}

function renderView(
  props: Partial<Parameters<typeof OperationsView>[0]> = {},
) {
  render(
    <OperationsView
      roleLabel="直属经理"
      quotaRemaining={20}
      inbox={inbox}
      detail={detail()}
      isLoading={false}
      isBusy={false}
      error={null}
      {...callbacks}
      {...props}
    />,
  )
}

describe('OperationsView', () => {
  it('shows a loading state while facts are being fetched', () => {
    renderView({ isLoading: true, inbox: null, detail: null })
    expect(screen.getByText('正在读取审批事实')).toBeInTheDocument()
  })

  it('shows a retryable error without inventing request facts', () => {
    renderView({ error: 'API 暂时不可用', inbox: null, detail: null })
    expect(screen.getByRole('alert')).toHaveTextContent('API 暂时不可用')
    expect(screen.getByRole('button', { name: '重新读取' })).toBeInTheDocument()
  })

  it('shows an empty inbox and the explicit approval-start boundary', () => {
    renderView({
      inbox: { ...inbox, items: [] },
      detail: detail({ approval: null, risk_review: null }),
    })
    expect(screen.getByText('当前没有待处理步骤')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '启动风险审查' })).toBeInTheDocument()
  })

  it('shows actionable success facts, policy evidence and ordered approval', () => {
    renderView()
    expect(screen.getByRole('button', { name: '批准当前步骤' })).toBeInTheDocument()
    expect(screen.getByText('高风险权限双审批')).toBeInTheDocument()
    expect(screen.getByText(/等待前序/)).toBeInTheDocument()
    expect(screen.getByText('申请人提交正式申请')).toBeInTheDocument()
  })

  it('lets the approver select and open every pending inbox item', async () => {
    const anotherRequestId = '77777777-7777-4777-8777-777777777777'
    const anotherItem = {
      ...inbox.items[0],
      request_id: anotherRequestId,
      approval_case_id: '88888888-8888-4888-8888-888888888888',
      approval_step_id: '99999999-9999-4999-8999-999999999999',
      entitlement_code: 'insighthub.dashboard_admin',
      entitlement_name: '经营看板管理',
      justification: '维护经营指标',
    }
    vi.mocked(readApprovalInbox).mockResolvedValue({ ...inbox, items: [inbox.items[0], anotherItem] })
    vi.mocked(readRequestDetail).mockImplementation(async (id) => {
      if (id === anotherRequestId) {
        const current = detail()
        return detail({
          request: {
            ...current.request,
            request_id: anotherRequestId,
            entitlement_code: anotherItem.entitlement_code,
            justification: anotherItem.justification,
          },
          entitlement: {
            ...current.entitlement,
            code: anotherItem.entitlement_code,
            name: anotherItem.entitlement_name,
          },
          approval: {
            ...current.approval!,
            approval_case_id: anotherItem.approval_case_id,
          },
        })
      }
      return detail()
    })

    render(
      <OperationsConsole
        actorId="EMP-002"
        roleLabel="直属经理"
        requestId={null}
        quotaRemaining={20}
      />,
    )

    await screen.findByRole('button', { name: /经营看板管理/ })
    fireEvent.click(screen.getByRole('button', { name: /经营看板管理/ }))

    await waitFor(() => expect(readRequestDetail).toHaveBeenLastCalledWith(anotherRequestId))
    expect(await screen.findByRole('heading', { name: '经营看板管理' })).toBeInTheDocument()
  })

  it('does not carry an approval comment across actor remounts', () => {
    const { rerender } = render(
      <OperationsView
        key="EMP-002"
        roleLabel="直属经理"
        quotaRemaining={20}
        inbox={inbox}
        detail={detail()}
        isLoading={false}
        isBusy={false}
        error={null}
        {...callbacks}
      />,
    )
    const comment = screen.getByRole('textbox', { name: '审批意见（可选）' })
    fireEvent.change(comment, { target: { value: '经理意见' } })

    rerender(
      <OperationsView
        key="EMP-003"
        roleLabel="数据负责人"
        quotaRemaining={20}
        inbox={inbox}
        detail={detail()}
        isLoading={false}
        isBusy={false}
        error={null}
        {...callbacks}
      />,
    )
    expect(screen.getByRole('textbox', { name: '审批意见（可选）' })).toHaveValue('')
  })

  it('offers status recovery only when IAM result is unknown', () => {
    const current = detail()
    renderView({
      detail: detail({
        approval: { ...current.approval!, approval_status: 'approved' },
        fault_mode: 'iam_timeout',
        provisioning: {
          ...current.provisioning,
          provisioning_status: 'unknown',
          provisioning_attempt_id: '66666666-6666-4666-8666-666666666666',
          attempt_count: 1,
          last_error: 'IAM 响应超时，结果未知',
        },
      }),
      inbox: { ...inbox, items: [] },
    })
    expect(screen.getByRole('button', { name: '查询原 IAM 操作' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始幂等开通' })).not.toBeInTheDocument()
  })

  it('keeps audit replay available when the model quota is exhausted', () => {
    renderView({ quotaRemaining: 0 })
    expect(screen.getByRole('status')).toHaveTextContent('历史消息和审计事实仍可只读回放')
    expect(screen.getByText('只读事实回放')).toBeInTheDocument()
  })
})
