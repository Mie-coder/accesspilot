import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ChatThread } from './ChatThread'
import { WorkbenchRuntime } from './WorkbenchRuntime'
import { useWorkbench } from './workbench-context'
import type { WorkspaceSnapshot } from './types'

const snapshot: WorkspaceSnapshot = {
  identity: {
    employee_id: 'EMP-001',
    name: '林默',
    department: 'product_operations',
    roles: [],
  },
  draft: null,
  events: [{
    id: 8,
    type: 'error.recoverable',
    payload: {
      code: 'UPSTREAM_UNAVAILABLE',
      message: '本轮可安全重试，请稍后再试。',
    },
  }],
  lastEventId: 8,
}


const historySnapshot: WorkspaceSnapshot = {
  ...snapshot,
  events: [
    { id: 1, type: 'message.user', payload: { content: '我是 EMP-001' } },
    {
      id: 2,
      type: 'message.completed',
      payload: {
        turn_id: 'turn-1',
        message_id: 'message-1',
        content: '已完成回答',
      },
    },
  ],
  lastEventId: 2,
}

const recoveredSnapshot: WorkspaceSnapshot = {
  ...snapshot,
  events: [
    {
      id: 8,
      type: 'error.recoverable',
      payload: { turn_id: 'turn-failed', message: '旧错误不应继续显示' },
    },
    {
      id: 9,
      type: 'message.completed',
      payload: {
        turn_id: 'turn-retry',
        message_id: 'message-retry',
        content: '重试后已完成',
      },
    },
  ],
  lastEventId: 9,
}

function Probe() {
  const workbench = useWorkbench()
  return (
    <>
      <output data-testid="workbench-error">{workbench.error}</output>
      <output data-testid="connection-state">{workbench.connectionState}</output>
      <output data-testid="draft-employee">{workbench.draft?.employee_id}</output>
      <output data-testid="missing-count">{workbench.missingFields.length}</output>
      <output data-testid="request-id">{workbench.requestResult?.request_id}</output>
      <output data-testid="packet-mode">{workbench.decisionPacket?.generation_mode}</output>
      <output data-testid="packet-error">{workbench.decisionPacketError}</output>
      <button type="button" onClick={() => void workbench.submit()}>submit request</button>
      <button type="button" onClick={() => void workbench.retryDecisionPacket()}>retry packet</button>
    </>
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('WorkbenchRuntime hydration', () => {
  it('restores a persisted recoverable error after refresh', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', {
      headers: { 'Content-Type': 'text/event-stream' },
    })))
    const view = render(
      <WorkbenchRuntime snapshot={snapshot}>
        <Probe />
        <ChatThread />
      </WorkbenchRuntime>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('workbench-error')).toHaveTextContent('本轮可安全重试')
    })
    expect(screen.getAllByText('本轮可安全重试，请稍后再试。').length).toBeGreaterThan(0)
    view.unmount()
  })

  it('hydrates persisted user and completed assistant messages instead of starter prompts', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', {
      headers: { 'Content-Type': 'text/event-stream' },
    })))
    const view = render(
      <WorkbenchRuntime snapshot={historySnapshot}>
        <ChatThread />
      </WorkbenchRuntime>,
    )

    await waitFor(() => {
      expect(screen.getByText('我是 EMP-001')).toBeInTheDocument()
      expect(screen.getByText('已完成回答')).toBeInTheDocument()
    })
    expect(screen.queryByText('从一句真实需求开始')).not.toBeInTheDocument()
    view.unmount()
  })

  it('does not restore a recoverable error after a later turn completes', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', {
      headers: { 'Content-Type': 'text/event-stream' },
    })))
    const view = render(
      <WorkbenchRuntime snapshot={recoveredSnapshot}>
        <Probe />
        <ChatThread />
      </WorkbenchRuntime>,
    )

    await waitFor(() => {
      expect(screen.getByText('重试后已完成')).toBeInTheDocument()
    })
    expect(screen.getByTestId('workbench-error')).toBeEmptyDOMElement()
    expect(screen.queryByText('旧错误不应继续显示')).not.toBeInTheDocument()
    view.unmount()
  })

  it('exposes reconnecting without turning the activity stream failure into a turn error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('activity stream offline')
    }))
    const view = render(
      <WorkbenchRuntime snapshot={{ ...snapshot, events: [], lastEventId: 0 }}>
        <Probe />
      </WorkbenchRuntime>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('connection-state')).toHaveTextContent('reconnecting')
    })
    expect(screen.getByTestId('workbench-error')).toBeEmptyDOMElement()
    view.unmount()
  })

  it('hydrates a login-bound employee in the initial draft without asking chat to identify them', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', {
      headers: { 'Content-Type': 'text/event-stream' },
    })))
    render(
      <WorkbenchRuntime
        snapshot={{
          ...snapshot,
          identity: { ...snapshot.identity, employee_id: 'EMP-002' },
          draft: null,
        }}
      >
        <Probe />
      </WorkbenchRuntime>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('draft-employee')).toHaveTextContent('EMP-002')
      expect(screen.getByTestId('missing-count')).toHaveTextContent('3')
    })
  })

  it('keeps the submitted request and retries only Packet generation after a Packet failure', async () => {
    const requestId = '11111111-1111-4111-8111-111111111111'
    let packetAttempts = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/events') {
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            'abort',
            () => reject(new DOMException('请求已取消', 'AbortError')),
            { once: true },
          )
        })
      }
      if (url === '/api/requests') {
        return Response.json({ request_id: requestId, request_status: 'submitted' })
      }
      if (url === `/api/requests/${requestId}/decision-packet`) {
        packetAttempts += 1
        expect(init?.method).toBe('POST')
        expect(init?.body).toBeUndefined()
        if (packetAttempts === 1) {
          return Response.json({ detail: '决策材料暂时无法生成，请重试' }, { status: 503 })
        }
        return Response.json({
          packet_id: '22222222-2222-4222-8222-222222222222',
          request_id: requestId,
          generation_mode: 'provider',
          packet_version: 'v1',
          catalog_version: 'fictional-catalog-v1',
          created_at: '2026-08-12T01:00:00Z',
          frozen_request: {
            requester_id: 'EMP-001', requester_name: '林晓',
            entitlement_code: 'insighthub.customer_export', entitlement_name: '脱敏客户数据导出',
            duration_days: 14, justification: '季度客户分析', request_status: 'submitted',
            confirmed_at: '2026-08-12T00:58:00Z',
          },
          catalog: { risk_level: 'high', approval_policy: 'manager_and_data_owner', max_duration_days: 30 },
          fixed_route: [],
          items: [],
          advisory: null,
          availability_message: null,
        })
      }
      if (url === '/api/events?follow=false') {
        return new Response('', { headers: { 'Content-Type': 'text/event-stream' } })
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = render(
      <WorkbenchRuntime snapshot={{ ...snapshot, events: [], lastEventId: 0 }}>
        <Probe />
      </WorkbenchRuntime>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'submit request' }))
    await waitFor(() => {
      expect(screen.getByTestId('request-id')).toHaveTextContent(requestId)
      expect(screen.getByTestId('packet-error')).toHaveTextContent('决策材料暂时无法生成')
    })
    expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/requests')).toHaveLength(1)
    expect(packetAttempts).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: 'retry packet' }))
    await waitFor(() => expect(screen.getByTestId('packet-mode')).toHaveTextContent('provider'))
    expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/requests')).toHaveLength(1)
    expect(packetAttempts).toBe(2)
    view.unmount()
  })

})
