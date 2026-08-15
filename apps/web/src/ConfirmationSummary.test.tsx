import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ConfirmationSummary } from './ConfirmationSummary'
import type { RequestDraft, WorkspaceIdentity } from './types'

const identity: WorkspaceIdentity = {
  employee_id: 'EMP-001',
  name: '林晓',
  department: '数据分析',
  roles: ['employee'],
}

const completeDraft: RequestDraft = {
  employee_id: 'EMP-001',
  entitlement_id: 'insighthub.customer_export',
  duration_days: 14,
  justification: '用于季度客户分析',
  confirmed: false,
}

describe('ConfirmationSummary', () => {
  it('stays hidden while required application information is incomplete', () => {
    render(
      <ConfirmationSummary
        identity={identity}
        draft={{ ...completeDraft, justification: null }}
        entitlementName="脱敏客户数据导出"
        isBusy={false}
        onConfirm={vi.fn()}
      />,
    )

    expect(screen.queryByRole('heading', { name: '请确认申请信息' })).not.toBeInTheDocument()
  })

  it('stays hidden after the backend-confirmed state returns', () => {
    render(
      <ConfirmationSummary
        identity={identity}
        draft={{ ...completeDraft, confirmed: true }}
        entitlementName="脱敏客户数据导出"
        isBusy={false}
        onConfirm={vi.fn()}
      />,
    )

    expect(screen.queryByRole('heading', { name: '请确认申请信息' })).not.toBeInTheDocument()
  })

  it('disables confirmation while another confirmation action is in progress', () => {
    render(
      <ConfirmationSummary
        identity={identity}
        draft={completeDraft}
        entitlementName="脱敏客户数据导出"
        isBusy
        onConfirm={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: '确认申请内容' })).toBeDisabled()
  })

  it('shows a complete, server-fact-backed confirmation sheet in the chat area', () => {
    render(
      <ConfirmationSummary
        identity={identity}
        draft={completeDraft}
        entitlementName="脱敏客户数据导出"
        isBusy={false}
        onConfirm={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { name: '请确认申请信息' })).toBeInTheDocument()
    expect(screen.getByText('林晓 · EMP-001')).toBeInTheDocument()
    expect(screen.getByText('脱敏客户数据导出（insighthub.customer_export）')).toBeInTheDocument()
    expect(screen.getByText('14 天')).toBeInTheDocument()
    expect(screen.getByText('用于季度客户分析')).toBeInTheDocument()
  })

  it('keeps confirmation available with the entitlement code when the name fact is unavailable', () => {
    const onConfirm = vi.fn()
    render(
      <ConfirmationSummary
        identity={identity}
        draft={completeDraft}
        entitlementName={null}
        isBusy={false}
        onConfirm={onConfirm}
      />,
    )

    expect(screen.getByText('insighthub.customer_export')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认申请内容' }))
    expect(onConfirm).toHaveBeenCalledOnce()
  })
})
