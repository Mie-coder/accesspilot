import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { readAccessibleRequests, readRequestDetail } from './api'
import { OperationsConsole, OperationsView } from './OperationsConsole'
import type { CaseList, RequestDetail } from './types'

vi.mock('./api', () => ({
  readAccessibleRequests: vi.fn(),
  readRequestDetail: vi.fn(),
}))

const requestId = '11111111-1111-4111-8111-111111111111'
const caseId = '22222222-2222-4222-8222-222222222222'

const cases: CaseList = {
  items: [
    {
      request_id: requestId,
      requester_id: 'EMP-001',
      requester_name: '林晓',
      entitlement_code: 'insighthub.customer_export',
      entitlement_name: '脱敏客户数据导出',
      duration_days: 14,
      justification: '季度客户分析',
      request_status: 'submitted',
      approval_status: 'pending_manager',
      created_at: '2026-08-05T01:00:00Z',
    },
  ],
}

function detail(overrides: Partial<RequestDetail> = {}): RequestDetail {
  return {
    view_mode: 'read_only_replay',
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
    decision_packet: null,
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
}

function renderView(props: Partial<Parameters<typeof OperationsView>[0]> = {}) {
  render(
    <OperationsView
      roleLabel="直属经理"
      cases={cases}
      detail={detail()}
      isLoading={false}
      error={null}
      {...callbacks}
      {...props}
    />,
  )
}

describe('OperationsView T20 read-only ACL view', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows an accessible loading state while resource facts are fetched', () => {
    renderView({ isLoading: true, cases: null, detail: null })
    expect(screen.getByRole('status')).toHaveTextContent('正在读取可访问 Case')
  })

  it('shows a retryable error without inventing Case facts', () => {
    renderView({ error: 'API 暂时不可用', cases: null, detail: null })
    expect(screen.getByRole('alert')).toHaveTextContent('API 暂时不可用')
    expect(screen.getByRole('button', { name: '重新读取' })).toBeInTheDocument()
  })

  it('shows a truthful empty state when SQL ACL returns no related Case', () => {
    renderView({ cases: { items: [] }, detail: null })
    expect(screen.getByText('当前没有可访问 Case')).toBeInTheDocument()
    expect(screen.getByText(/等待中的未轮到步骤.*不会出现/)).toBeInTheDocument()
  })

  it('shows current or previously decided Case facts but no T22/T23 action controls', () => {
    renderView()
    expect(screen.getByRole('button', { name: /脱敏客户数据导出/ })).toBeInTheDocument()
    expect(screen.getByText('高风险权限双审批')).toBeInTheDocument()
    expect(screen.getByText(/共享 Case 来自 PostgreSQL/)).toBeInTheDocument()
    for (const action of [
      '启动风险审查',
      '批准当前步骤',
      '驳回',
      '开始幂等开通',
      '查询原 IAM 操作',
      '清除故障并幂等重试',
    ]) {
      expect(screen.queryByRole('button', { name: action })).not.toBeInTheDocument()
    }
  })

  it('keeps an approved admin Case read-only even when recovery would later be legal', () => {
    const current = detail()
    renderView({
      roleLabel: '权限管理员',
      cases: {
        items: [{ ...cases.items[0], approval_status: 'approved' }],
      },
      detail: detail({
        approval: { ...current.approval!, approval_status: 'approved' },
        provisioning: {
          ...current.provisioning,
          provisioning_status: 'unknown',
          last_error: 'IAM 响应超时，结果未知',
        },
      }),
    })

    expect(screen.getByText('结果未知')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '查询原 IAM 操作' })).not.toBeInTheDocument()
    expect(
      screen.getByText(/当前页面只回放已持久化的业务事实.*不在读取过程中执行审批或开通/),
    ).toBeInTheDocument()
  })
})

describe('OperationsConsole principal-scoped Case loading', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('selects details only from the server-filtered accessible list', async () => {
    const anotherRequestId = '77777777-7777-4777-8777-777777777777'
    const anotherCase = {
      ...cases.items[0],
      request_id: anotherRequestId,
      entitlement_code: 'insighthub.dashboard_admin',
      entitlement_name: '经营看板管理',
      justification: '维护经营指标',
      approval_status: 'approved',
    }
    vi.mocked(readAccessibleRequests).mockResolvedValue({ items: [cases.items[0], anotherCase] })
    vi.mocked(readRequestDetail).mockImplementation(async (id) => {
      if (id === anotherRequestId) {
        const current = detail()
        return detail({
          request: {
            ...current.request,
            request_id: anotherRequestId,
            entitlement_code: anotherCase.entitlement_code,
            justification: anotherCase.justification,
          },
          entitlement: {
            ...current.entitlement,
            code: anotherCase.entitlement_code,
            name: anotherCase.entitlement_name,
          },
        })
      }
      return detail()
    })

    render(<OperationsConsole roleLabel="直属经理" requestId={null} />)

    await screen.findByRole('button', { name: /经营看板管理/ })
    expect(readAccessibleRequests).toHaveBeenCalledOnce()
    expect(readRequestDetail).toHaveBeenCalledWith(requestId)

    fireEvent.click(screen.getByRole('button', { name: /经营看板管理/ }))
    await waitFor(() => expect(readRequestDetail).toHaveBeenLastCalledWith(anotherRequestId))
    expect(await screen.findByRole('heading', { name: '经营看板管理' })).toBeInTheDocument()
  })

  it('does not probe a requestId that is absent from the accessible list', async () => {
    const untrustedRequestId = '99999999-9999-4999-8999-999999999999'
    vi.mocked(readAccessibleRequests).mockResolvedValue(cases)
    vi.mocked(readRequestDetail).mockResolvedValue(detail())

    render(<OperationsConsole roleLabel="直属经理" requestId={untrustedRequestId} />)

    await waitFor(() => expect(readRequestDetail).toHaveBeenCalledWith(requestId))
    expect(readRequestDetail).not.toHaveBeenCalledWith(untrustedRequestId)
  })
})
