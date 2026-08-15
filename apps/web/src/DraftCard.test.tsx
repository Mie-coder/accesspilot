import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { DraftCard } from './DraftCard'
import type { DecisionPacket, RequestDraft, RequestDetail } from './types'

const completeDraft: RequestDraft = {
  employee_id: 'EMP-001',
  entitlement_id: 'insighthub.customer_export',
  duration_days: 14,
  justification: '用于季度客户分析',
  confirmed: false,
}

const decisionPacket: DecisionPacket = {
  packet_id: 'packet-1',
  request_id: 'request-1',
  packet_version: 'v1',
  generation_mode: 'deterministic',
  catalog_version: 'fictional-catalog-v1',
  created_at: '2026-08-12T01:00:00Z',
  frozen_request: {
    requester_id: 'EMP-001',
    requester_name: '林晓',
    entitlement_code: 'insighthub.customer_export',
    entitlement_name: '脱敏客户数据导出',
    duration_days: 14,
    justification: '季度客户分析',
    request_status: 'submitted',
    confirmed_at: '2026-08-12T00:58:00Z',
  },
  catalog: { risk_level: 'high', approval_policy: 'manager_and_data_owner', max_duration_days: 30 },
  fixed_route: [],
  items: [],
  advisory: null,
  availability_message: null,
}

const approvalCase: NonNullable<RequestDetail['approval']> = {
  approval_case_id: 'case-1',
  approval_status: 'pending_manager',
  created_at: '2026-08-12T01:01:00Z',
  steps: [],
}

describe('DraftCard', () => {
  it('shows missing fields and prevents confirmation while the draft is incomplete', () => {
    render(
      <DraftCard
        draft={{ ...completeDraft, entitlement_id: null }}
        missingFields={['entitlement_id']}
        requestResult={null}
        decisionPacket={null}
        decisionPacketError={null}
        isGeneratingDecisionPacket={false}
        approvalCase={null}
        approvalError={null}
        isStartingApproval={false}
        isBusy={false}
        onConfirm={vi.fn()}
        onSubmit={vi.fn()}
        onRetryDecisionPacket={vi.fn()}
        onStartApproval={vi.fn()}
      />,
    )

    expect(screen.getByText('还差 1 项')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '补全后才能确认' })).toBeDisabled()
  })

  it('asks the backend to record explicit confirmation for a complete draft', () => {
    const onConfirm = vi.fn()
    render(
      <DraftCard
        draft={completeDraft}
        missingFields={[]}
        requestResult={null}
        decisionPacket={null}
        decisionPacketError={null}
        isGeneratingDecisionPacket={false}
        approvalCase={null}
        approvalError={null}
        isStartingApproval={false}
        isBusy={false}
        entitlementName="脱敏客户数据导出"
        onConfirm={onConfirm}
        onSubmit={vi.fn()}
        onRetryDecisionPacket={vi.fn()}
        onStartApproval={vi.fn()}
      />,
    )

    expect(screen.getByText('完整，等待确认')).toBeInTheDocument()
    expect(screen.getByText('脱敏客户数据导出（insighthub.customer_export）')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认申请内容' }))
    expect(onConfirm).toHaveBeenCalledOnce()
  })

  it('only enables formal submission after the backend-confirmed state returns', () => {
    const onSubmit = vi.fn()
    render(
      <DraftCard
        draft={{ ...completeDraft, confirmed: true }}
        missingFields={[]}
        requestResult={null}
        decisionPacket={null}
        decisionPacketError={null}
        isGeneratingDecisionPacket={false}
        approvalCase={null}
        approvalError={null}
        isStartingApproval={false}
        isBusy={false}
        onConfirm={vi.fn()}
        onSubmit={onSubmit}
        onRetryDecisionPacket={vi.fn()}
        onStartApproval={vi.fn()}
      />,
    )

    expect(screen.getByText('已确认，可送审')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '提交正式申请' }))
    expect(onSubmit).toHaveBeenCalledOnce()
  })

  it('keeps the formal request id and retries only its Decision Packet', () => {
    const onRetryDecisionPacket = vi.fn()
    render(
      <DraftCard
        draft={{ ...completeDraft, confirmed: true }}
        missingFields={[]}
        requestResult={{ request_id: 'request-1', request_status: 'submitted' }}
        decisionPacket={null}
        decisionPacketError="决策材料暂时无法生成"
        isGeneratingDecisionPacket={false}
        approvalCase={null}
        approvalError={null}
        isStartingApproval={false}
        isBusy={false}
        onConfirm={vi.fn()}
        onSubmit={vi.fn()}
        onRetryDecisionPacket={onRetryDecisionPacket}
        onStartApproval={vi.fn()}
      />,
    )

    expect(screen.getByText('request-1')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('决策材料暂时无法生成')
    fireEvent.click(screen.getByRole('button', { name: '重试生成决策材料' }))
    expect(onRetryDecisionPacket).toHaveBeenCalledOnce()
  })

  it('offers a keyboard-focusable start action only after the Packet is frozen', () => {
    const onStartApproval = vi.fn()
    render(
      <DraftCard
        draft={{ ...completeDraft, confirmed: true }}
        missingFields={[]}
        requestResult={{ request_id: 'request-1', request_status: 'submitted' }}
        decisionPacket={decisionPacket}
        decisionPacketError={null}
        isGeneratingDecisionPacket={false}
        approvalCase={null}
        approvalError={null}
        isStartingApproval={false}
        isBusy={false}
        onConfirm={vi.fn()}
        onSubmit={vi.fn()}
        onRetryDecisionPacket={vi.fn()}
        onStartApproval={onStartApproval}
      />,
    )

    const start = screen.getByRole('button', { name: '启动审批' })
    start.focus()
    expect(start).toHaveFocus()
    expect(start).toHaveAttribute('type', 'button')
    fireEvent.click(start)
    expect(onStartApproval).toHaveBeenCalledOnce()
  })

  it('shows approval busy, retryable error, and idempotent already-started states', () => {
    const onStartApproval = vi.fn()
    const common = {
      draft: { ...completeDraft, confirmed: true },
      missingFields: [],
      requestResult: { request_id: 'request-1', request_status: 'submitted' },
      decisionPacket,
      decisionPacketError: null,
      isGeneratingDecisionPacket: false,
      isBusy: false,
      onConfirm: vi.fn(),
      onSubmit: vi.fn(),
      onRetryDecisionPacket: vi.fn(),
      onStartApproval,
    }
    const { rerender } = render(
      <DraftCard
        {...common}
        approvalCase={null}
        approvalError={null}
        isStartingApproval
      />,
    )
    expect(screen.getByRole('button', { name: '正在启动审批' })).toBeDisabled()

    rerender(
      <DraftCard
        {...common}
        approvalCase={null}
        approvalError="启动失败，可安全重试"
        isStartingApproval={false}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('启动失败')
    fireEvent.click(screen.getByRole('button', { name: '重试启动审批' }))
    expect(onStartApproval).toHaveBeenCalledOnce()

    rerender(
      <DraftCard
        {...common}
        approvalCase={approvalCase}
        approvalError={null}
        isStartingApproval={false}
      />,
    )
    expect(screen.getByText('审批已启动')).toBeInTheDocument()
    expect(screen.getByText('待直属经理审批')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /启动审批/ })).not.toBeInTheDocument()
  })
})
