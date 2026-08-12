import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  bootstrapWorkspace,
  createDecisionPacket,
  parseSseEvents,
  readAccessibleRequests,
  readMyRequests,
} from './api'

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
  it('hydrates the authenticated session before loading private state', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/drafts/current') {
        return Response.json({ draft: null })
      }
      if (url === '/api/auth/session') {
        return Response.json({
          employee_id: 'EMP-002',
          principal: {
            employee_id: 'EMP-002',
            name: '陈峰',
            department: 'product_operations',
            roles: ['manager'],
          },
          csrf_token: 'csrf-1',
          expires_at: '2030-01-01T00:00:00Z',
        })
      }
      if (url === '/api/events?follow=false') {
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
    expect(snapshot.lastEventId).toBe(1)
    expect(snapshot.events[0]?.type).toBe('message.assistant')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/session',
      expect.objectContaining({ credentials: 'include' }),
    )
  })
})

describe('principal-scoped Case lists', () => {
  it('uses the requester-only list for My Requests and the ACL list for role work', async () => {
    const fetchMock = vi.fn(async () =>
      Response.json({ items: [] }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(readMyRequests()).resolves.toEqual({ items: [] })
    await expect(readAccessibleRequests()).resolves.toEqual({ items: [] })

    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      '/api/requests/mine',
      '/api/requests/accessible',
    ])
    for (const [, init] of fetchMock.mock.calls) {
      expect(init).toEqual(expect.objectContaining({ credentials: 'include' }))
    }
  })
})

describe('Decision Packet write boundary', () => {
  it('posts only the request path and sends no client-controlled packet body', async () => {
    const response = { packet_id: 'packet-1', request_id: 'request-1' }
    const fetchMock = vi.fn(async () => Response.json(response))
    vi.stubGlobal('fetch', fetchMock)

    await expect(createDecisionPacket('request/with spaces')).resolves.toEqual(response)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/requests/request%2Fwith%20spaces/decision-packet',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
      }),
    )
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBeUndefined()
  })
})
