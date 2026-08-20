import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactElement } from 'react'

import { resetAuthClientState } from './api'
import { App } from './App'
import { entitlementNameForCode } from './entitlement-name'
import type { RequestDraft } from './types'

const appMocks = vi.hoisted(() => ({
  append: vi.fn(),
  draft: null as RequestDraft | null,
  chatConfirm: null as (() => void) | null,
  sidebarConfirm: null as (() => void) | null,
}))

// Keep these tests focused on the auth state machine.  The full workbench has
// its own component/runtime tests; replacing it here still renders the real
// App, LoginScreen, BootError, and WorkbenchPage branches.
vi.mock('./WorkbenchRuntime', () => ({
  WorkbenchRuntime: ({ children }: { children: unknown }) => children,
}))
vi.mock('@assistant-ui/react', () => ({
  useAui: () => ({ thread: { append: appMocks.append } }),
  useAuiState: (selector: (state: { thread: { isRunning: boolean } }) => unknown) =>
    selector({ thread: { isRunning: false } }),
}))
vi.mock('./ChatThread', () => ({
  ChatThread: ({ confirmation }: {
    confirmation?: ReactElement<{ onConfirm: () => void }>
  }) => {
    appMocks.chatConfirm = confirmation?.props.onConfirm ?? null
    return (
      <>
        <div>对话视图内容</div>
        <input aria-label="未发送的对话内容" defaultValue="" />
        {confirmation ?? null}
      </>
    )
  },
}))
vi.mock('./AgentTrajectory', () => ({
  AgentTrajectory: () => <div>轨迹视图内容</div>,
}))
vi.mock('./AccessCards', () => ({ AccessCards: () => null }))
vi.mock('./DraftCard', () => ({
  DraftCard: ({ onConfirm }: { onConfirm: () => void }) => {
    appMocks.sidebarConfirm = onConfirm
    return null
  },
}))
vi.mock('./OperationsConsole', () => ({ OperationsConsole: () => null }))
vi.mock('./PolicyCard', () => ({ PolicyCard: () => null }))
vi.mock('./RequestTimeline', () => ({ RequestTimeline: () => null }))
vi.mock('./workbench-context', () => ({
  useWorkbench: () => ({
    identity: {
      employee_id: 'EMP-003',
      name: '数据负责人',
      department: 'security',
      roles: [],
    },
    draft: appMocks.draft,
    missingFields: appMocks.draft === null ? [] : [],
    events: [],
    businessStatus: 'collecting',
    error: null,
    requestResult: null,
    retryableInterruption: false,
    connectionState: 'connected',
    submit: async () => undefined,
    selectEntitlement: async () => ({
      status: 'rejected' as const,
      code: '',
      message: '',
      previous_confirmation_invalidated: false,
    }),
    isSubmitting: false,
  }),
}))

const principal = {
  employee_id: 'EMP-003',
  name: '数据负责人',
  department: 'security',
  roles: [],
}

const authPayload = {
  csrf_token: 'csrf-test-token',
  principal,
  expires_at: '2026-08-12T00:00:00Z',
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function emptyEventsResponse(): Response {
  return new Response('', {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

function authenticatedFetch(options: { logoutStatus?: number } = {}) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/auth/session') return jsonResponse(authPayload)
    if (url === '/api/drafts/current') return jsonResponse({ draft: null })
    if (url === '/api/events?follow=false') return emptyEventsResponse()
    if (url === '/api/auth/logout') {
      return jsonResponse(
        options.logoutStatus && options.logoutStatus !== 200
          ? { detail: '退出失败，请稍后重试' }
          : { status: 'revoked' },
        options.logoutStatus ?? 200,
      )
    }
    if (url === '/api/auth/login') return jsonResponse(authPayload)
    throw new Error(`unexpected fetch ${url} ${init?.method ?? 'GET'}`)
  })
}

beforeEach(() => {
  resetAuthClientState()
  vi.unstubAllGlobals()
  appMocks.append.mockReset().mockImplementation(() => new Promise<void>(() => undefined))
  appMocks.draft = null
  appMocks.chatConfirm = null
  appMocks.sidebarConfirm = null
})

describe('application confirmation facts', () => {
  it('does not reuse a name loaded for an earlier entitlement code', () => {
    expect(entitlementNameForCode(
      { code: 'old.permission', name: '旧权限名称' },
      'new.permission',
    )).toBeNull()
  })
})

describe('App authentication state machine', () => {
  it('switches tabs with roving keyboard focus and keeps the live conversation mounted without side effects', async () => {
    const fetchMock = authenticatedFetch()
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    const trajectoryTab = await screen.findByRole('tab', { name: '轨迹' })
    const conversationTab = screen.getByRole('tab', { name: '对话' })
    const draftInput = screen.getByRole('textbox', { name: '未发送的对话内容' })
    const conversationPanel = screen.getByRole('tabpanel', { name: '对话' })
    expect(conversationTab).toHaveAttribute('aria-selected', 'true')
    expect(conversationTab).toHaveAttribute('tabindex', '0')
    expect(trajectoryTab).toHaveAttribute('tabindex', '-1')
    expect(screen.getByText('对话视图内容')).toBeInTheDocument()
    fireEvent.change(draftInput, { target: { value: '尚未发送' } })
    const fetchCount = fetchMock.mock.calls.length

    conversationTab.focus()
    fireEvent.keyDown(conversationTab, { key: 'ArrowRight' })

    expect(trajectoryTab).toHaveAttribute('aria-selected', 'true')
    expect(trajectoryTab).toHaveFocus()
    expect(trajectoryTab).toHaveAttribute('tabindex', '0')
    expect(conversationTab).toHaveAttribute('tabindex', '-1')
    expect(conversationPanel).toHaveAttribute('hidden')
    expect(screen.getByText('轨迹视图内容')).toBeInTheDocument()
    expect(screen.getByText('对话视图内容')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(fetchCount)
    expect(appMocks.append).not.toHaveBeenCalled()

    fireEvent.keyDown(trajectoryTab, { key: 'Home' })
    expect(conversationTab).toHaveFocus()
    expect(conversationTab).toHaveAttribute('aria-selected', 'true')
    expect(draftInput).toHaveValue('尚未发送')

    fireEvent.keyDown(conversationTab, { key: 'ArrowLeft' })
    expect(trajectoryTab).toHaveFocus()
    fireEvent.keyDown(trajectoryTab, { key: 'End' })
    expect(trajectoryTab).toHaveFocus()
  })

  it('allows only one same-frame explicit confirmation across both confirmation buttons', async () => {
    appMocks.draft = {
      employee_id: 'EMP-003',
      entitlement_id: 'insighthub.customer_export',
      duration_days: 14,
      justification: '用于季度客户分析',
      confirmed: false,
    }
    const fetchMock = authenticatedFetch()
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/access-overview') return jsonResponse({ items: [] })
      if (url === '/api/auth/session') return jsonResponse(authPayload)
      if (url === '/api/drafts/current') return jsonResponse({ draft: appMocks.draft })
      if (url === '/api/events?follow=false') return emptyEventsResponse()
      throw new Error(`unexpected fetch ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await waitFor(() => expect(appMocks.chatConfirm).not.toBeNull())

    act(() => {
      appMocks.chatConfirm?.()
      appMocks.sidebarConfirm?.()
    })

    expect(appMocks.append).toHaveBeenCalledOnce()
  })

  it('shows four Mock Login accounts after an anonymous 401 without legacy bootstrap', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toBe('/api/auth/session')
      return jsonResponse({ detail: '登录会话无效或已过期' }, 401)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    await waitFor(() => expect(screen.getByRole('heading', { name: '选择一个作品集账号' })).toBeInTheDocument())
    expect(screen.getByText(/作品集 Mock 登录，非真实身份认证/)).toBeInTheDocument()
    for (const accountId of ['EMP-001', 'EMP-002', 'EMP-003', 'EMP-004']) {
      expect(screen.getByText(accountId)).toBeInTheDocument()
    }
    expect(screen.getAllByText('查看当前轮到或已参与的 Case')).toHaveLength(2)
    expect(screen.getByText('查看已全部批准的 Case')).toBeInTheDocument()
    expect(screen.queryByText(/处理当前轮到|执行已全部批准/)).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/workspaces'))).toBe(false)
  })

  it('renders login loading and a stable error when account login fails', async () => {
    let resolveLogin: ((response: Response) => void) | undefined
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input) === '/api/auth/session') {
        return Promise.resolve(jsonResponse({}, 401))
      }
      if (String(input) === '/api/auth/login') {
        return new Promise<Response>((resolve) => { resolveLogin = resolve })
      }
      throw new Error(`unexpected fetch ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    const account = await screen.findByRole('button', { name: /EMP-001/ })
    account.click()
    await waitFor(() => expect(account).toBeDisabled())
    resolveLogin?.(jsonResponse({ detail: 'Mock 登录失败' }, 503))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Mock 登录失败'))
    expect(screen.getByRole('button', { name: /EMP-001/ })).not.toBeDisabled()
  })

  it('hydrates the authenticated principal after session refresh', async () => {
    const fetchMock = authenticatedFetch()
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    await waitFor(() => expect(screen.getByText(/EMP-003/)).toBeInTheDocument())
    expect(screen.getByText('数据负责人')).toBeInTheDocument()
    expect(screen.queryByText(/角色切换/)).not.toBeInTheDocument()
    expect(screen.queryByText(/DemoConsole/)).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/workspaces'))).toBe(false)
  })

  it('shows a retryable boot error for non-401 bootstrap failures', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: '服务暂时不可用' }, 503)))

    render(<App />)

    await waitFor(() => expect(screen.getByRole('heading', { name: '暂时无法连接 AccessPilot API' })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: '重新连接' })).toBeInTheDocument()
  })

  it('keeps the authenticated workbench visible when logout fails, then returns to login on success', async () => {
    const fetchMock = authenticatedFetch({ logoutStatus: 503 })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await waitFor(() => expect(screen.getByRole('button', { name: '退出登录' })).toBeInTheDocument())
    screen.getByRole('button', { name: '退出登录' }).click()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('退出失败'))
    expect(screen.getByText(/EMP-003/)).toBeInTheDocument()

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/auth/logout') return jsonResponse({ status: 'revoked' })
      if (url === '/api/auth/session') return jsonResponse(authPayload)
      if (url === '/api/drafts/current') return jsonResponse({ draft: null })
      if (url === '/api/events?follow=false') return emptyEventsResponse()
      throw new Error(`unexpected fetch ${url}`)
    })
    screen.getByRole('button', { name: '退出登录' }).click()
    await waitFor(() => expect(screen.getByRole('heading', { name: '选择一个作品集账号' })).toBeInTheDocument())
  })

  it('returns to Mock Login when a business request broadcasts a 401', async () => {
    vi.stubGlobal('fetch', authenticatedFetch())

    render(<App />)
    await waitFor(() => expect(screen.getByText(/EMP-003/)).toBeInTheDocument())
    window.dispatchEvent(new Event('accesspilot:unauthorized'))

    await waitFor(() => expect(screen.getByRole('heading', { name: '选择一个作品集账号' })).toBeInTheDocument())
  })
})
