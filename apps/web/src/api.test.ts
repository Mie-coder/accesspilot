import { afterEach, describe, expect, it, vi } from 'vitest'

import { bootstrapWorkspace, parseSseEvents } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('parseSseEvents', () => {
  it('keeps stable ids and parses each safe backend event', () => {
    const events = parseSseEvents(
      'id: 41\nevent: message.user\ndata: {"content":"我是 EMP-001"}\n\n' +
        'id: 42\nevent: business.status\ndata: {"status":"collecting"}\n\n',
    )

    expect(events).toEqual([
      { id: 41, type: 'message.user', payload: { content: '我是 EMP-001' } },
      { id: 42, type: 'business.status', payload: { status: 'collecting' } },
    ])
  })
})

describe('bootstrapWorkspace', () => {
  it('creates a workspace after an unauthorized draft read, then loads replay state', async () => {
    let draftReads = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/drafts/current') {
        draftReads += 1
        return draftReads === 1
          ? new Response('{"detail":"Workspace cookie 是必须的"}', {
              status: 401,
              headers: { 'Content-Type': 'application/json' },
            })
          : Response.json({ draft: null })
      }
      if (url === '/api/workspaces') {
        expect(init?.method).toBe('POST')
        return Response.json({ status: 'created' }, { status: 201 })
      }
      if (url === '/api/model-quota') {
        return Response.json({ used: 0, limit: 20, remaining: 20 })
      }
      if (url === '/api/events') {
        expect(new Headers(init?.headers).get('Last-Event-ID')).toBe('0')
        return new Response(
          'id: 1\nevent: message.assistant\ndata: {"content":"请告诉我你需要什么权限。"}\n\n',
          { headers: { 'Content-Type': 'text/event-stream' } },
        )
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const snapshot = await bootstrapWorkspace()

    expect(snapshot.draft).toBeNull()
    expect(snapshot.quota.remaining).toBe(20)
    expect(snapshot.lastEventId).toBe(1)
    expect(snapshot.events[0]?.type).toBe('message.assistant')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/workspaces',
      expect.objectContaining({ credentials: 'include', method: 'POST' }),
    )
  })
})
