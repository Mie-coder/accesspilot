import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  login,
  logout,
  previewDraft,
  readAuthSession,
  replayEvents,
  resetAuthClientState,
  streamChatMessage,
} from './api'

afterEach(() => {
  resetAuthClientState()
  vi.unstubAllGlobals()
})

describe('T19 auth client contract', () => {
  it('keeps CSRF in memory and sends it on writes after login', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        principal: {
          employee_id: 'EMP-001',
          name: '林默',
          department: 'product_operations',
          roles: ['requester'],
        },
        csrf_token: 'csrf-login',
        expires_at: '2030-01-01T00:00:00Z',
      }))
      .mockResolvedValueOnce(Response.json({ status: 'logged_out' }))
    vi.stubGlobal('fetch', fetchMock)

    await login('EMP-001')
    await logout()

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/auth/login')
    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-login')
    expect(fetchMock.mock.calls[1]?.[1]?.credentials).toBe('include')
    expect(localStorage.getItem('accesspilot_session')).toBeNull()
    expect(localStorage.getItem('accesspilot_csrf')).toBeNull()
  })

  it('refreshes Principal/CSRF from GET session without legacy workspace bootstrap', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/drafts/preview') {
        expect(JSON.parse(String(init?.body))).not.toHaveProperty('employee_id')
        return Response.json({
          draft: {
            employee_id: 'EMP-002',
            entitlement_id: null,
            duration_days: null,
            justification: null,
            confirmed: false,
          },
          draft_revision: 1,
          missing_fields: ['entitlement_id', 'duration_days', 'justification'],
          is_complete: false,
          can_enter_approval: false,
          entitlement_resolution: null,
          issues: [],
        })
      }
      expect(String(input)).toBe('/api/auth/session')
      return Response.json({
        principal: {
          employee_id: 'EMP-002',
          name: '陈峰',
          department: 'product_operations',
          roles: ['manager'],
        },
        csrf_token: 'csrf-refresh',
        expires_at: '2030-01-01T00:00:00Z',
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const payload = await readAuthSession()
    await previewDraft({
      entitlement_id: null,
      duration_days: null,
      justification: null,
      confirmed: false,
    })

    expect(payload.principal.employee_id).toBe('EMP-002')
    const previewCall = fetchMock.mock.calls.find(
      ([input]) => String(input) === '/api/drafts/preview',
    )
    expect(previewCall).toBeDefined()
    const previewBody = JSON.parse(String(previewCall?.[1]?.body)) as Record<string, unknown>
    expect(previewBody).not.toHaveProperty('employee_id')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('returns expired SSE clients to the login state through the shared 401 event', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ detail: '登录会话无效或已过期' }, { status: 401 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const unauthorized = vi.fn()
    window.addEventListener('accesspilot:unauthorized', unauthorized)

    await expect(streamChatMessage('继续').next()).rejects.toMatchObject({ status: 401 })
    await expect(replayEvents()).rejects.toMatchObject({ status: 401 })

    expect(unauthorized).toHaveBeenCalledTimes(2)
    window.removeEventListener('accesspilot:unauthorized', unauthorized)
  })
})
