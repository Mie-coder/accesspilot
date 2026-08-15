import type { ChatModelRunResult } from '@assistant-ui/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createChatModelAdapter } from './runtime'

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

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('createChatModelAdapter', () => {
  it('sends the latest visible user text and consumes the current-turn stream', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('/api/chat/messages/stream')
      expect(init?.signal).toBeInstanceOf(AbortSignal)
      expect(JSON.parse(String(init?.body))).toEqual({ content: '我是 EMP-001' })
      return new Response(
        streamFromText(
          frame('message.delta', 1, { text: '请提供' }) +
            frame('message.completed', 2, { content: '请提供' }),
        ),
        { headers: { 'Content-Type': 'text/event-stream' } },
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    const onError = vi.fn()
    const adapter = createChatModelAdapter({
      getLastEventId: () => 12,
      onTurn: vi.fn(),
      onEvents: vi.fn(),
      onError,
    })
    const output = adapter.run({
      messages: [{ role: 'user', content: [{ type: 'text', text: '我是 EMP-001' }] }],
      abortSignal: new AbortController().signal,
    } as never) as AsyncGenerator<ChatModelRunResult>

    const result = await output.next()
    const done = await output.next()

    expect(result.value).toEqual({ content: [{ type: 'text', text: '请提供' }] })
    expect(done).toEqual({ done: true, value: undefined })
    expect(onError).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledOnce()
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
