import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import { readAccessOverview, resolveEntitlement } from './api'
import { AccessCards } from './AccessCards'
import type { EntitlementResolution, EntitlementSelectionResult, WorkspaceEvent } from './types'
import { WorkbenchContext, type WorkbenchContextValue } from './workbench-context'

vi.mock('./api', () => ({
  readAccessOverview: vi.fn(),
  resolveEntitlement: vi.fn(),
}))

const candidate = {
  code: 'insighthub.dashboard_view',
  name: 'InsightHub 仪表盘查看',
  system_code: 'insighthub',
  system_name: '数据洞察中心',
  risk_level: 'low',
  max_duration_days: 180,
  approval_policy: 'manager',
}

function resolution(query: string): EntitlementResolution {
  return {
    status: 'matched',
    target_field: 'entitlement_id',
    query,
    candidates: [candidate],
    eligible_access: [],
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise })
  return { promise, resolve }
}

function renderContainer(selectEntitlement: WorkbenchContextValue['selectEntitlement']) {
  const context: WorkbenchContextValue = {
    identity: { employee_id: 'EMP-001', name: '林晓', department: 'product', roles: [] },
    draft: null,
    missingFields: ['entitlement_id', 'duration_days', 'justification'],
    demoSession: { demo_mode_enabled: false, demo_session_active: false, fault_mode: null },
    events: [] as WorkspaceEvent[],
    businessStatus: 'collecting',
    error: null,
    requestResult: null,
    retryableInterruption: false,
    connectionState: 'connected',
    isSubmitting: false,
    selectEntitlement,
    submit: vi.fn(async () => undefined),
  }
  return render(
    <WorkbenchContext.Provider value={context}>
      <AccessCards />
    </WorkbenchContext.Provider>,
  )
}

describe('AccessCards container concurrency', () => {
  beforeEach(() => {
    vi.mocked(readAccessOverview).mockReset().mockResolvedValue({ items: [] })
    vi.mocked(resolveEntitlement).mockReset()
  })

  it('allows only one candidate selection preview while the first is in flight', async () => {
    const preview = deferred<EntitlementSelectionResult>()
    const selectEntitlement = vi.fn(() => preview.promise)
    vi.mocked(resolveEntitlement).mockResolvedValue(resolution('仪表盘查看'))
    renderContainer(selectEntitlement)
    const query = await screen.findByRole('textbox', { name: '查找可申请权限' })
    fireEvent.change(query, { target: { value: '仪表盘查看' } })
    fireEvent.click(screen.getByRole('button', { name: '解析权限名称' }))
    const candidateButton = await screen.findByRole('button', { name: /InsightHub 仪表盘查看/ })
    fireEvent.click(candidateButton)
    fireEvent.click(candidateButton)
    expect(selectEntitlement).toHaveBeenCalledTimes(1)
    expect(candidateButton).toBeDisabled()

    preview.resolve({
      status: 'revalidated',
      code: candidate.code,
      message: '重新校验通过，可以继续补充申请字段。',
      previous_confirmation_invalidated: false,
    })
    await waitFor(() => expect(screen.getByText('重新校验通过')).toBeInTheDocument())
  })

  it('ignores an older out-of-order entitlement resolution', async () => {
    const first = deferred<EntitlementResolution>()
    const second = deferred<EntitlementResolution>()
    vi.mocked(resolveEntitlement)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    renderContainer(vi.fn(async () => ({
      status: 'revalidated',
      code: candidate.code,
      message: '重新校验通过，可以继续补充申请字段。',
      previous_confirmation_invalidated: false,
    })))
    const query = await screen.findByRole('textbox', { name: '查找可申请权限' })
    fireEvent.change(query, { target: { value: '旧问题' } })
    fireEvent.click(screen.getByRole('button', { name: '解析权限名称' }))
    fireEvent.change(query, { target: { value: '新问题' } })
    fireEvent.click(screen.getByRole('button', { name: '解析权限名称' }))

    second.resolve(resolution('新问题'))
    await screen.findByText('已匹配一个权限')
    first.resolve({ ...resolution('旧问题'), status: 'no_match', candidates: [], eligible_access: [] })
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 0))
    expect(screen.getByText('已匹配一个权限')).toBeInTheDocument()
    expect(screen.queryByText('没有匹配的权限')).not.toBeInTheDocument()
  })
})
