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
  it('ensures a workspace before loading draft and replay state', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/drafts/current') {
        return Response.json({ draft: null })
      }
      if (url === '/api/workspaces/identity') {
        return Response.json({
          employee_id: 'EMP-002',
          name: '陈峰',
          department: 'product_operations',
          roles: ['manager'],
        })
      }
      if (url === '/api/workspaces/ensure') {
        expect(init?.method).toBe('POST')
        return Response.json({ status: 'created' }, { status: 201 })
      }
      if (url === '/api/demo/session') {
        return Response.json({ demo_mode_enabled: true, demo_session_active: false })
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

    expect(snapshot.identity.employee_id).toBe('EMP-002')
    expect(snapshot.draft).toBeNull()
    expect(snapshot.demoSession).toEqual({ demo_mode_enabled: true, demo_session_active: false })
    expect(snapshot.lastEventId).toBe(1)
    expect(snapshot.events[0]?.type).toBe('message.assistant')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/workspaces/ensure',
      expect.objectContaining({ credentials: 'include', method: 'POST' }),
    )
  })
})
