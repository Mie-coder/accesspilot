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
  draftRevision: 0,
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
      <output data-testid="draft-confirmed">{String(workbench.draft?.confirmed)}</output>
      <output data-testid="draft-entitlement">{workbench.draft?.entitlement_id}</output>
      <output data-testid="draft-justification">{workbench.draft?.justification}</output>
      <output data-testid="request-id">{workbench.requestResult?.request_id}</output>
      <output data-testid="packet-mode">{workbench.decisionPacket?.generation_mode}</output>
      <output data-testid="packet-error">{workbench.decisionPacketError}</output>
      <output data-testid="approval-status">{workbench.approvalCase?.approval_status}</output>
      <output data-testid="approval-error">{workbench.approvalError}</output>
      <output data-testid="approval-busy">{String(workbench.isStartingApproval)}</output>
      <button type="button" onClick={() => void workbench.submit()}>submit request</button>
      <button type="button" onClick={() => void workbench.retryDecisionPacket()}>retry packet</button>
      <button type="button" onClick={() => void workbench.startApproval()}>start approval</button>
      <button
        type="button"
        onClick={() => void workbench.selectEntitlement('codeforge.repo_read')}
      >select entitlement</button>
    </>
  )
}

function turnFrame(event: string, seq: number, payload: Record<string, unknown>): string {
  const data = JSON.stringify({
    schema_version: 'v1',
    turn_id: 'turn-resume',
    seq,
    occurred_at: '2026-08-20T08:00:00Z',
    payload,
  })
  return `id: turn-resume:${seq}\nevent: ${event}\ndata: ${data}\n\n`
}

function streamFromText(text: string): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(text))
      controller.close()
    },
  })
}

function controlledStream(): {
  stream: ReadableStream<Uint8Array>
  push: (text: string) => void
  close: () => void
} {
  const encoder = new TextEncoder()
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null
  return {
    stream: new ReadableStream<Uint8Array>({
      start(value) {
        controller = value
      },
    }),
    push(text) {
      controller?.enqueue(encoder.encode(text))
    },
    close() {
      controller?.close()
    },
  }
}

function workspaceFrame(
  id: number,
  event: string,
  payload: Record<string, unknown>,
): string {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(payload)}\n\n`
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('WorkbenchRuntime hydration', () => {
  it('applies the authoritative confirmed draft from a resumed terminal frame', async () => {
    const unconfirmedDraft = {
      employee_id: 'EMP-001',
      entitlement_id: 'insighthub.customer_export',
      duration_days: 14,
      justification: '虚构季度分析',
      confirmed: false,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/events') {
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            'abort',
            () => reject(new DOMException('请求已取消', 'AbortError')),
            { once: true },
          )
        })
      }
      if (String(input) === '/api/chat/messages/stream') {
        return new Response(
          streamFromText(
            turnFrame('message.delta', 1, { text: '已确认。' })
              + turnFrame('message.completed', 2, {
                content: '已确认。申请草稿已就绪，可以提交正式申请。',
                business_status: 'ready_to_submit',
                draft_revision: 2,
                draft: { ...unconfirmedDraft, confirmed: true },
              }),
          ),
          { headers: { 'Content-Type': 'text/event-stream' } },
        )
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    }))
    const view = render(
      <WorkbenchRuntime snapshot={{
        ...snapshot,
        draft: unconfirmedDraft,
        draftRevision: 1,
        events: [],
      }}>
        <Probe />
        <ChatThread />
      </WorkbenchRuntime>,
    )

    fireEvent.change(screen.getByRole('textbox', { name: '描述权限申请' }), {
      target: { value: '确认提交' },
    })
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))

    await waitFor(() => {
      expect(screen.getByTestId('draft-confirmed')).toHaveTextContent('true')
    })
    view.unmount()
  })

  it('does not let a delayed Workspace event roll back a newer current-turn draft', async () => {
    const workspace = controlledStream()
    const revisionThreeDraft = {
      employee_id: 'EMP-001',
      entitlement_id: 'insighthub.customer_export',
      duration_days: 14,
      justification: '虚构旧理由',
      confirmed: false,
    }
    const revisionFourDraft = {
      ...revisionThreeDraft,
      justification: '虚构新理由',
      confirmed: true,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/events') {
        return new Response(workspace.stream, {
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      if (String(input) === '/api/chat/messages/stream') {
        return new Response(
          streamFromText(turnFrame('message.completed', 1, {
            content: '已确认新草稿。',
            business_status: 'ready_to_submit',
            draft: revisionFourDraft,
            draft_revision: 4,
          })),
          { headers: { 'Content-Type': 'text/event-stream' } },
        )
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    }))
    const view = render(
      <WorkbenchRuntime snapshot={{
        ...snapshot,
        draft: revisionThreeDraft,
        draftRevision: 3,
        events: [],
        lastEventId: 0,
      }}>
        <Probe />
        <ChatThread />
      </WorkbenchRuntime>,
    )

    fireEvent.change(screen.getByRole('textbox', { name: '描述权限申请' }), {
      target: { value: '确认新草稿' },
    })
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    await waitFor(() => {
      expect(screen.getByTestId('draft-justification')).toHaveTextContent('虚构新理由')
      expect(screen.getByTestId('draft-confirmed')).toHaveTextContent('true')
    })

    workspace.push(workspaceFrame(1, 'draft.updated', {
      draft: revisionThreeDraft,
      draft_revision: 3,
    }))
    await waitFor(() => expect(screen.getByTestId('connection-state')).toHaveTextContent('connected'))
    expect(screen.getByTestId('draft-justification')).toHaveTextContent('虚构新理由')
    expect(screen.getByTestId('draft-confirmed')).toHaveTextContent('true')
    workspace.close()
    view.unmount()
  })

  it('treats identical same-revision server drafts as idempotent', async () => {
    const workspace = controlledStream()
    const authoritativeDraft = {
      employee_id: 'EMP-001',
      entitlement_id: 'insighthub.dashboard_view',
      duration_days: 7,
      justification: '虚构幂等验证',
      confirmed: false,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/events') {
        return new Response(workspace.stream, {
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = render(
      <WorkbenchRuntime snapshot={{
        ...snapshot,
        draft: authoritativeDraft,
        draftRevision: 5,
        events: [],
        lastEventId: 0,
      }}><Probe /></WorkbenchRuntime>,
    )

    workspace.push(workspaceFrame(1, 'draft.updated', {
      draft: authoritativeDraft,
      draft_revision: 5,
    }))
    await waitFor(() => expect(screen.getByTestId('connection-state')).toHaveTextContent('connected'))
    expect(screen.getByTestId('draft-justification')).toHaveTextContent('虚构幂等验证')
    expect(screen.getByTestId('workbench-error')).toBeEmptyDOMElement()
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/drafts/current')).toBe(false)
    workspace.close()
    view.unmount()
  })

  it('fails closed on a conflicting same-revision draft and reloads authority', async () => {
    const workspace = controlledStream()
    const authoritativeDraft = {
      employee_id: 'EMP-001',
      entitlement_id: 'insighthub.dashboard_view',
      duration_days: 7,
      justification: '虚构权威理由',
      confirmed: false,
    }
    const conflictingDraft = {
      ...authoritativeDraft,
      justification: '虚构冲突理由',
      confirmed: true,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/events') {
        return new Response(workspace.stream, {
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      if (String(input) === '/api/drafts/current') {
        return Response.json({ draft: authoritativeDraft, draft_revision: 6 })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = render(
      <WorkbenchRuntime snapshot={{
        ...snapshot,
        draft: authoritativeDraft,
        draftRevision: 6,
        events: [],
        lastEventId: 0,
      }}><Probe /></WorkbenchRuntime>,
    )

    workspace.push(workspaceFrame(1, 'message.completed', {
      content: '不应接受冲突草稿',
      draft: conflictingDraft,
      draft_revision: 6,
    }))
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/drafts/current')).toBe(true)
      expect(screen.getByTestId('workbench-error')).toHaveTextContent('草稿版本冲突')
    })
    expect(screen.getByTestId('draft-justification')).toHaveTextContent('虚构权威理由')
    expect(screen.getByTestId('draft-confirmed')).toHaveTextContent('false')
    workspace.close()
    view.unmount()
  })

  it('ignores server drafts with missing or invalid revisions', async () => {
    const workspace = controlledStream()
    const authoritativeDraft = {
      employee_id: 'EMP-001',
      entitlement_id: 'insighthub.dashboard_view',
      duration_days: 7,
      justification: '虚构有效草稿',
      confirmed: false,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/events') {
        return new Response(workspace.stream, {
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    }))
    const view = render(
      <WorkbenchRuntime snapshot={{
        ...snapshot,
        draft: authoritativeDraft,
        draftRevision: 7,
        events: [],
        lastEventId: 0,
      }}><Probe /></WorkbenchRuntime>,
    )

    workspace.push(
      workspaceFrame(1, 'draft.updated', {
        draft: { ...authoritativeDraft, justification: '缺失 revision' },
      })
      + workspaceFrame(2, 'draft.updated', {
        draft: { ...authoritativeDraft, justification: '非法 revision' },
        draft_revision: '8',
      }),
    )
    await waitFor(() => expect(screen.getByTestId('connection-state')).toHaveTextContent('connected'))
    expect(screen.getByTestId('draft-justification')).toHaveTextContent('虚构有效草稿')
    workspace.close()
    view.unmount()
  })

  it('keeps a local selection out of the draft until preview returns server authority', async () => {
    const workspace = controlledStream()
    const originalDraft = {
      employee_id: 'EMP-001',
      entitlement_id: 'insighthub.dashboard_view',
      duration_days: 7,
      justification: '虚构本地输入边界',
      confirmed: false,
    }
    let resolvePreview: ((response: Response) => void) | undefined
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      if (String(input) === '/api/events') {
        return Promise.resolve(new Response(workspace.stream, {
          headers: { 'Content-Type': 'text/event-stream' },
        }))
      }
      if (String(input) === '/api/drafts/preview') {
        return new Promise<Response>((resolve) => { resolvePreview = resolve })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    }))
    const view = render(
      <WorkbenchRuntime snapshot={{
        ...snapshot,
        draft: originalDraft,
        draftRevision: 2,
        events: [],
        lastEventId: 0,
      }}><Probe /></WorkbenchRuntime>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'select entitlement' }))
    expect(screen.getByTestId('draft-entitlement')).toHaveTextContent(
      'insighthub.dashboard_view',
    )
    resolvePreview?.(Response.json({
      draft: { ...originalDraft, entitlement_id: 'codeforge.repo_read' },
      draft_revision: 3,
      missing_fields: [],
      is_complete: true,
      can_enter_approval: false,
      entitlement_resolution: {
        status: 'matched',
        target_field: 'entitlement_id',
        query: 'codeforge.repo_read',
        candidates: [{ code: 'codeforge.repo_read' }],
        eligible_access: [],
      },
      issues: [],
    }))
    await waitFor(() => {
      expect(screen.getByTestId('draft-entitlement')).toHaveTextContent('codeforge.repo_read')
    })
    workspace.close()
    view.unmount()
  })

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

  it('starts approval only after Packet generation and confirms it by rereading the requester Case', async () => {
    const requestId = '11111111-1111-4111-8111-111111111111'
    let approvalStarts = 0
    let resolveStart: (() => void) | undefined
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
        return Response.json({
          packet_id: '22222222-2222-4222-8222-222222222222',
          request_id: requestId,
          generation_mode: 'deterministic',
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
      if (url === `/api/requests/${requestId}/approval-case`) {
        approvalStarts += 1
        await new Promise<void>((resolve) => { resolveStart = resolve })
        return Response.json({ approval_case_id: 'case-1', approval_status: 'pending_manager' })
      }
      if (url === `/api/requests/${requestId}`) {
        return Response.json({
          approval: {
            approval_case_id: 'case-1',
            approval_status: 'pending_manager',
            created_at: '2026-08-12T01:01:00Z',
            steps: [],
          },
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
    await waitFor(() => expect(screen.getByTestId('packet-mode')).toHaveTextContent('deterministic'))

    fireEvent.click(screen.getByRole('button', { name: 'start approval' }))
    await waitFor(() => expect(screen.getByTestId('approval-busy')).toHaveTextContent('true'))
    expect(approvalStarts).toBe(1)
    resolveStart?.()

    await waitFor(() => expect(screen.getByTestId('approval-status')).toHaveTextContent('pending_manager'))
    expect(screen.getByTestId('approval-error')).toBeEmptyDOMElement()
    expect(fetchMock).toHaveBeenCalledWith(`/api/requests/${requestId}`, expect.anything())
    view.unmount()
  })

  it('keeps approval start retryable when POST or requester Case refresh fails', async () => {
    const requestId = '11111111-1111-4111-8111-111111111111'
    let startAttempts = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/events') {
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('', 'AbortError')), { once: true })
        })
      }
      if (url === '/api/requests') return Response.json({ request_id: requestId, request_status: 'submitted' })
      if (url === `/api/requests/${requestId}/decision-packet`) {
        return Response.json({
          packet_id: 'packet-1', request_id: requestId, generation_mode: 'deterministic',
          packet_version: 'v1', catalog_version: 'catalog-v1', created_at: '2026-08-12T01:00:00Z',
          frozen_request: {
            requester_id: 'EMP-001', requester_name: '林晓', entitlement_code: 'access.read',
            entitlement_name: '读取权限', duration_days: 7, justification: '业务查询',
            request_status: 'submitted', confirmed_at: '2026-08-12T00:58:00Z',
          },
          catalog: { risk_level: 'low', approval_policy: 'manager', max_duration_days: 30 },
          fixed_route: [], items: [], advisory: null, availability_message: null,
        })
      }
      if (url === `/api/requests/${requestId}/approval-case`) {
        startAttempts += 1
        if (startAttempts === 1) return Response.json({ detail: '启动审批失败，可安全重试' }, { status: 503 })
        return Response.json({ detail: '审批流已经创建' }, { status: 409 })
      }
      if (url === `/api/requests/${requestId}`) {
        return Response.json({
          approval: { approval_case_id: 'case-1', approval_status: 'pending_manager', created_at: '', steps: [] },
        })
      }
      if (url === '/api/events?follow=false') return new Response('', { headers: { 'Content-Type': 'text/event-stream' } })
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = render(
      <WorkbenchRuntime snapshot={{ ...snapshot, events: [], lastEventId: 0 }}><Probe /></WorkbenchRuntime>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'submit request' }))
    await waitFor(() => expect(screen.getByTestId('packet-mode')).toHaveTextContent('deterministic'))
    fireEvent.click(screen.getByRole('button', { name: 'start approval' }))
    await waitFor(() => expect(screen.getByTestId('approval-error')).toHaveTextContent('启动审批失败'))

    fireEvent.click(screen.getByRole('button', { name: 'start approval' }))
    await waitFor(() => expect(screen.getByTestId('approval-status')).toHaveTextContent('pending_manager'))
    expect(startAttempts).toBe(2)
    view.unmount()
  })

})
