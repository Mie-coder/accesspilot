import type { ChatModelRunResult } from '@assistant-ui/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createChatModelAdapter } from './runtime'
import type { ChatTurn } from './types'

const turn: ChatTurn = {
  assistant_message: '请提供要申请的权限编号。',
  draft: {
    employee_id: 'EMP-001',
    entitlement_id: null,
    duration_days: null,
    justification: null,
    confirmed: false,
  },
  missing_fields: ['entitlement_id', 'duration_days', 'justification'],
  phase: 'collecting',
  business_status: 'collecting',
  quota: { used: 1, limit: 20, remaining: 19 },
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('createChatModelAdapter', () => {
  it('sends the latest visible user text and replays SSE state after the turn', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/chat/messages') {
        expect(init?.signal).toBeInstanceOf(AbortSignal)
        expect(JSON.parse(String(init?.body))).toEqual({ content: '我是 EMP-001' })
        return Response.json(turn)
      }
      if (String(input) === '/api/events') {
        expect(new Headers(init?.headers).get('Last-Event-ID')).toBe('12')
        return new Response(
          'id: 13\nevent: draft.updated\ndata: {"draft":{},"missing_fields":[],"can_enter_approval":false}\n\n',
        )
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const onTurn = vi.fn()
    const onEvents = vi.fn()
    const onError = vi.fn()
    const adapter = createChatModelAdapter({
      getLastEventId: () => 12,
      onTurn,
      onEvents,
      onError,
    })
    const output = adapter.run({
      messages: [{ role: 'user', content: [{ type: 'text', text: '我是 EMP-001' }] }],
      abortSignal: new AbortController().signal,
    } as never) as AsyncGenerator<ChatModelRunResult>

    const result = await output.next()

    expect(result.value).toEqual({ content: [{ type: 'text', text: turn.assistant_message }] })
    expect(onTurn).toHaveBeenCalledWith(turn)
    expect(onEvents).toHaveBeenCalledWith([
      expect.objectContaining({ id: 13, type: 'draft.updated' }),
    ])
    expect(onError).not.toHaveBeenCalled()
  })

  it('surfaces a safe backend error through assistant-ui', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json(
          { detail: '模型调用额度已用尽，当前为只读回放模式' },
          { status: 429 },
        ),
      ),
    )
    const onError = vi.fn()
    const adapter = createChatModelAdapter({
      getLastEventId: () => 0,
      onTurn: vi.fn(),
      onEvents: vi.fn(),
      onError,
    })
    const output = adapter.run({
      messages: [{ role: 'user', content: [{ type: 'text', text: '继续' }] }],
      abortSignal: new AbortController().signal,
    } as never) as AsyncGenerator<ChatModelRunResult>

    await expect(output.next()).rejects.toThrow('模型调用额度已用尽')
    expect(onError).toHaveBeenCalledWith('模型调用额度已用尽，当前为只读回放模式')
  })
})
