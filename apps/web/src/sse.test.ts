import { describe, expect, it } from 'vitest'

import { parseSseStream } from './api'

function streamFromChunks(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

describe('parseSseStream', () => {
  it('parses arbitrary chunk boundaries, CRLF, multiline data, and ignores heartbeat comments', async () => {
    const stream = streamFromChunks([
      'id: turn-1:1\r\nevent: message.delta\r\ndata: {"schema_version":"v1",',
      '\r\ndata: "turn_id":"turn-1","seq":1,"payload":{"text":"第一段"}}\r\n',
      '\r\n: heartbeat\r\n\r\n',
      'id: turn-1:2\r\nevent: message.completed\r\ndata: {"schema_version":"v1",',
      '\r\ndata: "turn_id":"turn-1","seq":2,"payload":{"content":"第一段"}}\r\n\r\n',
    ])

    const frames: unknown[] = []
    for await (const parsed of parseSseStream(stream)) frames.push(parsed)

    expect(frames).toEqual([
      {
        id: 'turn-1:1',
        event: 'message.delta',
        data: {
          schema_version: 'v1',
          turn_id: 'turn-1',
          seq: 1,
          payload: { text: '第一段' },
        },
      },
      {
        id: 'turn-1:2',
        event: 'message.completed',
        data: {
          schema_version: 'v1',
          turn_id: 'turn-1',
          seq: 2,
          payload: { content: '第一段' },
        },
      },
    ])
  })

  it('fails closed on malformed SSE data instead of yielding an unsafe frame', async () => {
    const stream = streamFromChunks([
      'id: turn-1:1\nevent: message.delta\ndata: {not-json}\n\n',
    ])

    await expect(async () => {
      for await (const parsed of parseSseStream(stream)) {
        void parsed
        // The malformed frame must reject before this body executes.
      }
    }).rejects.toThrow()
  })
})
