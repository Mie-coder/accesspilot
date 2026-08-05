import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { DraftCard } from './DraftCard'
import type { RequestDraft } from './types'

const completeDraft: RequestDraft = {
  employee_id: 'EMP-001',
  entitlement_id: 'insighthub.customer_export',
  duration_days: 14,
  justification: '用于季度客户分析',
  confirmed: false,
}

describe('DraftCard', () => {
  it('shows missing fields and prevents confirmation while the draft is incomplete', () => {
    render(
      <DraftCard
        draft={{ ...completeDraft, entitlement_id: null }}
        missingFields={['entitlement_id']}
        requestResult={null}
        isBusy={false}
        onConfirm={vi.fn()}
        onSubmit={vi.fn()}
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
        isBusy={false}
        onConfirm={onConfirm}
        onSubmit={vi.fn()}
      />,
    )

    expect(screen.getByText('完整，等待确认')).toBeInTheDocument()
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
        isBusy={false}
        onConfirm={vi.fn()}
        onSubmit={onSubmit}
      />,
    )

    expect(screen.getByText('已确认，可送审')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '提交正式申请' }))
    expect(onSubmit).toHaveBeenCalledOnce()
  })
})
