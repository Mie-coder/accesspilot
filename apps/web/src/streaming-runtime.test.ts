import type { ChatModelRunResult } from '@assistant-ui/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createChatModelAdapter } from './runtime'

afterEach(() => {
  vi.unstubAllGlobals()
})

function frame(event: string, seq: number, payload: Record<string, unknown>): string {
  const data = JSON.stringify({
    schema_version: 'v1',
    turn_id: 'turn-1',
    seq,
    occurred_at: '2026-08-10T00:00:00Z',
    payload,
  })
  return `id: turn-1:${seq}\nevent: ${event}\ndata: ${data}\n\n`
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

describe('createChatModelAdapter T14 stream contract', () => {
  it('yields each real delta and does not duplicate completed content', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('/api/chat/messages/stream')
      expect(init?.signal).toBeInstanceOf(AbortSignal)
      expect(JSON.parse(String(init?.body))).toEqual({ content: '我是 EMP-001' })
      return new Response(
        streamFromText(
          frame('message.delta', 1, { text: '第一段' }) +
            frame('message.delta', 2, { text: '第二段' }) +
            frame('message.completed', 3, {
              message_id: 'message-1',
              content: '第一段第二段',
              persisted_event_id: 8,
            }),
        ),
        { headers: { 'Content-Type': 'text/event-stream' } },
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    const adapter = createChatModelAdapter({
      getLastEventId: () => 12,
      onTurn: vi.fn(),
      onEvents: vi.fn(),
      onError: vi.fn(),
    })
    const output = adapter.run({
      messages: [{ role: 'user', content: [{ type: 'text', text: '我是 EMP-001' }] }],
      abortSignal: new AbortController().signal,
    } as never) as AsyncGenerator<ChatModelRunResult>

    const first = await output.next()
    const second = await output.next()
    const done = await output.next()
    expect(first.value).toEqual({ content: [{ type: 'text', text: '第一段' }] })
    expect(second.value).toEqual({ content: [{ type: 'text', text: '第一段第二段' }] })
    expect(done).toEqual({ done: true, value: undefined })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('reports recoverable stream errors exactly once', async () => {
    const fetchMock = vi.fn(async () => new Response(
      streamFromText(frame('error.recoverable', 1, {
        code: 'UPSTREAM_UNAVAILABLE',
        message: '本轮可安全重试',
      })),
      { headers: { 'Content-Type': 'text/event-stream' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
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

    await expect(output.next()).rejects.toThrow('本轮可安全重试')
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith('本轮可安全重试')
  })

})
