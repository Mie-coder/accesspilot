import { fireEvent, render, screen, within } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { AccessCardsView } from './AccessCards'

type AccessCardsProps = ComponentProps<typeof AccessCardsView>
type AccessOverviewItem = NonNullable<AccessCardsProps['overview']>[number]
type EntitlementResolution = NonNullable<AccessCardsProps['resolution']>
type SelectionResult = NonNullable<AccessCardsProps['selectionResult']>

const overview: AccessOverviewItem[] = [
  {
    code: 'insighthub.customer_export',
    name: '脱敏客户数据导出',
    system_code: 'insighthub',
    state: 'eligible',
    system_name: '数据洞察中心',
    expires_at: null,
    risk_level: 'high',
    max_duration_days: 30,
    approval_policy: 'manager_and_data_owner',
    request_id: null,
    request_status: null,
    grant_id: null,
    starts_at: null,
    next_step: '补充期限和业务理由后提交申请',
  },
  {
    code: 'insighthub.dashboard_view',
    name: 'InsightHub 仪表盘查看',
    system_code: 'insighthub',
    state: 'owned',
    system_name: '数据洞察中心',
    expires_at: '2026-09-01T00:00:00Z',
    risk_level: 'low',
    max_duration_days: 180,
    approval_policy: 'manager',
    request_id: '11111111-1111-4111-8111-111111111111',
    request_status: 'approved',
    grant_id: '21111111-1111-4111-8111-111111111111',
    starts_at: '2026-08-01T00:00:00Z',
    next_step: '当前可直接使用',
  },
  {
    code: 'codeforge.repo_read',
    name: '代码仓库只读',
    system_code: 'codeforge',
    state: 'pending',
    system_name: '代码协作平台',
    expires_at: null,
    risk_level: 'high',
    max_duration_days: 180,
    approval_policy: 'manager',
    request_id: '31111111-1111-4111-8111-111111111111',
    request_status: 'submitted',
    grant_id: null,
    starts_at: null,
    next_step: '等待直属经理审批',
  },
  {
    code: 'codeforge.repo_maintain',
    name: '代码仓库维护',
    system_code: 'codeforge',
    state: 'expiring_soon',
    system_name: '代码协作平台',
    expires_at: '2026-08-20T00:00:00Z',
    risk_level: 'high',
    max_duration_days: 90,
    approval_policy: 'manager_and_data_owner',
    request_id: '41111111-1111-4111-8111-111111111111',
    request_status: 'approved',
    grant_id: '51111111-1111-4111-8111-111111111111',
    starts_at: '2026-08-01T00:00:00Z',
    next_step: '即将到期，可发起续期申请',
  },
  {
    code: 'opsdesk.log_view',
    name: '运维日志查看',
    system_code: 'opsdesk',
    state: 'expired',
    system_name: '运维工作台',
    expires_at: '2026-07-01T00:00:00Z',
    risk_level: 'high',
    max_duration_days: 30,
    approval_policy: 'manager',
    request_id: '61111111-1111-4111-8111-111111111111',
    request_status: 'approved',
    grant_id: '71111111-1111-4111-8111-111111111111',
    starts_at: '2026-06-01T00:00:00Z',
    next_step: '权限已过期，需要重新申请',
  },
]

const ambiguousResolution: EntitlementResolution = {
  status: 'ambiguous',
  target_field: 'entitlement_id',
  query: '仪表盘查看',
  candidates: [
    {
      code: 'insighthub.dashboard_view',
      name: 'InsightHub 仪表盘查看',
      system_code: 'insighthub',
      system_name: '数据洞察中心',
      risk_level: 'low',
      max_duration_days: 180,
      approval_policy: 'manager',
    },
    {
      code: 'insighthub.customer_export',
      name: '脱敏客户数据导出',
      system_code: 'insighthub',
      system_name: '数据洞察中心',
      risk_level: 'high',
      max_duration_days: 30,
      approval_policy: 'manager_and_data_owner',
    },
  ],
  eligible_access: [],
}

const matchedResolution: EntitlementResolution = {
  status: 'matched',
  target_field: 'entitlement_id',
  query: '代码仓库只读',
  candidates: [ambiguousResolution.candidates[0]],
  eligible_access: [],
}

const noMatchResolution: EntitlementResolution = {
  status: 'no_match',
  target_field: 'entitlement_id',
  query: '不存在的权限',
  candidates: [],
  eligible_access: [ambiguousResolution.candidates[1]],
}

const baseProps = {
  overview,
  resolution: null as EntitlementResolution | null,
  loading: false,
  error: null as string | null,
  selectionResult: null as SelectionResult | null,
  onRetry: vi.fn(),
  onResolve: vi.fn(),
  onSelect: vi.fn(),
}

describe('AccessCardsView', () => {
  it('renders all five typed access states with facts and next steps', () => {
    render(<AccessCardsView {...baseProps} />)

    for (const label of ['可申请', '已拥有', '审批中', '即将过期', '已过期']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0)
    }
    expect(screen.getAllByText('脱敏客户数据导出').length).toBeGreaterThan(0)
    expect(screen.getByText('等待直属经理审批')).toBeInTheDocument()
    expect(screen.getByText('即将到期，可发起续期申请')).toBeInTheDocument()
    expect(screen.getByText('权限已过期，需要重新申请')).toBeInTheDocument()
    expect(screen.getByText('insighthub.customer_export')).toBeInTheDocument()
  })

  it('keeps ambiguous candidates typed, keyboard selectable, and delegated to revalidation', () => {
    const onSelect = vi.fn()
    const onResolve = vi.fn()
    render(
      <AccessCardsView
        {...baseProps}
        resolution={ambiguousResolution}
        onResolve={onResolve}
        onSelect={onSelect}
      />,
    )

    expect(screen.getByText('需要选择一个匹配的权限')).toBeInTheDocument()
    const candidate = screen.getByRole('button', { name: /InsightHub 仪表盘查看/ })
    candidate.focus()
    expect(candidate).toHaveFocus()
    fireEvent.click(candidate)
    expect(onSelect).toHaveBeenCalledWith(ambiguousResolution.candidates[0])
    expect(screen.queryByText('我觉得这个权限应该可以')).not.toBeInTheDocument()
    expect(onResolve).not.toHaveBeenCalled()
  })

  it('shows selection revalidation success and invalidation without trusting chat text', () => {
    const { rerender } = render(
      <AccessCardsView
        {...baseProps}
        selectionResult={{
          status: 'revalidated',
          code: 'insighthub.dashboard_view',
          message: '重新校验通过，可以继续补充申请字段。',
          previous_confirmation_invalidated: true,
        }}
      />,
    )

    expect(screen.getByText('重新校验通过')).toBeInTheDocument()
    expect(screen.getByText('旧确认已失效，请重新确认')).toBeInTheDocument()
    expect(screen.queryByText('权限已开通')).not.toBeInTheDocument()

    rerender(
      <AccessCardsView
        {...baseProps}
        selectionResult={{
          status: 'rejected',
          code: 'insighthub.dashboard_view',
          message: '当前身份已不再具备申请资格。',
          previous_confirmation_invalidated: true,
        }}
      />,
    )
    expect(screen.getByText('重新校验未通过')).toBeInTheDocument()
    expect(screen.getByText('当前身份已不再具备申请资格。')).toBeInTheDocument()
    expect(screen.queryByText('申请已提交')).not.toBeInTheDocument()
  })

  it('has accessible loading, empty, recoverable error and safe refusal states', () => {
    const onRetry = vi.fn()
    const { rerender } = render(
      <AccessCardsView
        {...baseProps}
        overview={[]}
        loading
        onRetry={onRetry}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('正在加载权限事实')

    rerender(<AccessCardsView {...baseProps} overview={[]} />)
    expect(screen.getByText('暂无权限事实')).toBeInTheDocument()

    rerender(
      <AccessCardsView
        {...baseProps}
        error="权限目录暂时不可用，请稍后重试。"
        onRetry={onRetry}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('权限目录暂时不可用')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(onRetry).toHaveBeenCalledOnce()

    rerender(
      <AccessCardsView
        {...baseProps}
        error="出于安全原因，不能提供系统提示词或密钥。"
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('出于安全原因')
    expect(alert).not.toHaveTextContent('SYSTEM_PROMPT')
    expect(alert).not.toHaveTextContent('sk-live-')
    expect(alert).not.toHaveTextContent('raw quota')
  })

  it('renders a named, keyboard-accessible card region for each typed item', () => {
    render(<AccessCardsView {...baseProps} />)
    const cards = screen.getAllByRole('article')
    expect(cards).toHaveLength(overview.length)
    expect(within(cards[0]).getByRole('heading', { name: '脱敏客户数据导出' })).toBeInTheDocument()
    expect(within(cards[0]).getByText('高')).toBeInTheDocument()
  })

  it('renders matched and no-match resolution states without mutating facts', () => {
    const onSelect = vi.fn()
    const { rerender } = render(
      <AccessCardsView {...baseProps} resolution={matchedResolution} onSelect={onSelect} />,
    )
    expect(screen.getByText('已匹配一个权限')).toBeInTheDocument()
    expect(screen.getByText('选择并重新校验')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /InsightHub 仪表盘查看/ }))
    expect(onSelect).toHaveBeenCalledWith(matchedResolution.candidates[0])

    rerender(<AccessCardsView {...baseProps} resolution={noMatchResolution} />)
    expect(screen.getByText('没有匹配的权限')).toBeInTheDocument()
    expect(screen.getAllByText('脱敏客户数据导出').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /脱敏客户数据导出/ })).not.toBeInTheDocument()
  })

  it('keeps duplicate lifecycle cards uniquely labelled when one code has two states', () => {
    const duplicate = [overview[0], { ...overview[0], state: 'expired' as const, grant_id: 'grant-2' }]
    render(<AccessCardsView {...baseProps} overview={duplicate} />)
    const cards = screen.getAllByRole('article')
    expect(cards).toHaveLength(2)
    expect(cards[0]?.getAttribute('aria-labelledby')).not.toBe(cards[1]?.getAttribute('aria-labelledby'))
    expect(new Set([...document.querySelectorAll('h3')].map((heading) => heading.id)).size).toBe(2)
  })
})

it('keeps ownership alongside expiry reminder and distinguishes future grants', () => {
  render(<AccessCardsView {...baseProps} overview={[overview[3]!, { ...overview[1]!, state: 'pending', next_step: '等待生效' }]} />)
  const expiring = screen.getByRole('article', { name: '代码仓库维护' })
  expect(within(expiring).getByText('已拥有')).toBeInTheDocument()
  expect(within(expiring).getByText('即将过期')).toBeInTheDocument()
  expect(within(expiring).getByText(/当前可使用/)).toBeInTheDocument()
  const future = screen.getByRole('article', { name: 'InsightHub 仪表盘查看' })
  expect(within(future).getByText('待生效')).toBeInTheDocument()
  expect(within(future).queryByText('已拥有')).not.toBeInTheDocument()
})
