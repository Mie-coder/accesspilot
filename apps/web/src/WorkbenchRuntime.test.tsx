import { render, screen, waitFor } from '@testing-library/react'
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

})
