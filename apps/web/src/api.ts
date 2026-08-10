import type {
  ApprovalInbox,
  ChatTurn,
  ModelQuota,
  RequestDraft,
  RequestResult,
  RequestDetail,
  WorkspaceEvent,
  WorkspaceIdentity,
  DemoSession,
  WorkspaceSnapshot,
} from './types'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') return body.detail
  } catch {
    // 非 JSON 错误也只向用户展示稳定的 HTTP 状态，不暴露响应正文。
  }
  return `请求失败（${response.status}）`
}

async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      ...init.headers,
    },
  })
  if (!response.ok) {
    throw new ApiError(response.status, await errorMessage(response))
  }
  return (await response.json()) as T
}

async function readDraft(): Promise<RequestDraft | null> {
  const response = await requestJson<{ draft: RequestDraft | null }>('/api/drafts/current')
  return response.draft
}

export async function readDemoSession(): Promise<DemoSession> {
  try {
    return await requestJson<DemoSession>('/api/demo/session')
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return { demo_mode_enabled: false, demo_session_active: false, fault_mode: null }
    }
    throw error
  }

}

export function parseSseEvents(text: string): WorkspaceEvent[] {
  return text
    .split(/\r?\n\r?\n/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => {
      const fields = new Map<string, string>()
      for (const line of block.split(/\r?\n/)) {
        const separator = line.indexOf(':')
        if (separator === -1) continue
        fields.set(line.slice(0, separator), line.slice(separator + 1).trimStart())
      }
      const id = Number(fields.get('id'))
      const type = fields.get('event')
      const rawData = fields.get('data')
      if (!Number.isSafeInteger(id) || id < 0 || !type || !rawData) {
        throw new Error('后端事件格式无效')
      }
      const payload = JSON.parse(rawData) as unknown
      if (payload === null || Array.isArray(payload) || typeof payload !== 'object') {
        throw new Error('后端事件 payload 无效')
      }
      return { id, type, payload: payload as Record<string, unknown> }
    })
}

export async function replayEvents(
  afterId = 0,
  signal?: AbortSignal,
): Promise<WorkspaceEvent[]> {
  const response = await fetch('/api/events', {
    credentials: 'include',
    headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(afterId) },
    signal,
  })
  if (!response.ok) {
    throw new ApiError(response.status, await errorMessage(response))
  }
  return parseSseEvents(await response.text())
}

export async function bootstrapWorkspace(): Promise<WorkspaceSnapshot> {
  // 后端原子地复用有效 Workspace 或创建新空间，首次加载无需先触发 401/404。
  await requestJson<{ status: string }>('/api/workspaces/ensure', { method: 'POST' })
  const [identity, draft, events, demoSession] = await Promise.all([
    requestJson<WorkspaceIdentity>('/api/workspaces/identity'),
    readDraft(),
    replayEvents(),
    readDemoSession(),
  ])
  return {
    identity,
    draft,
    demoSession,
    events,
    lastEventId: events.at(-1)?.id ?? 0,
  }
}

export async function sendChatMessage(
  content: string,
  signal?: AbortSignal,
): Promise<ChatTurn> {
  return requestJson<ChatTurn>('/api/chat/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
    signal,
  })
}

export async function submitRequest(signal?: AbortSignal): Promise<RequestResult> {
  return requestJson<RequestResult>('/api/requests', { method: 'POST', signal })
}

export async function readApprovalInbox(): Promise<ApprovalInbox> {
  return requestJson<ApprovalInbox>('/api/approval-inbox')
}

export async function readRequestDetail(requestId: string): Promise<RequestDetail> {
  return requestJson<RequestDetail>(`/api/requests/${encodeURIComponent(requestId)}`)
}

export async function startApproval(requestId: string): Promise<void> {
  await requestJson(`/api/requests/${encodeURIComponent(requestId)}/approval-case`, {
    method: 'POST',
  })
}

export async function decideApproval(
  caseId: string,
  decision: 'approve' | 'reject',
  comment: string | null,
): Promise<void> {
  await requestJson(`/api/approval-cases/${encodeURIComponent(caseId)}/decisions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, comment }),
  })
}

export async function setFaultMode(
  faultMode: 'iam_failure' | 'iam_timeout' | null,
): Promise<void> {
  await requestJson('/api/demo/fault-mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fault_mode: faultMode }),
  })
}

export async function provisionRequest(requestId: string): Promise<void> {
  await requestJson(`/api/requests/${encodeURIComponent(requestId)}/provision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ idempotency_key: `accesspilot-${requestId}` }),
  })
}

export async function recoverProvisioning(requestId: string): Promise<void> {
  await requestJson(`/api/requests/${encodeURIComponent(requestId)}/provision/recover`, {
    method: 'POST',
  })
}
export async function enterDemoSession(
  employeeId: string,
): Promise<DemoSession & WorkspaceIdentity> {
  return requestJson<DemoSession & WorkspaceIdentity>('/api/demo/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ employee_id: employeeId }),
  })
}

export async function exitDemoSession(): Promise<DemoSession & WorkspaceIdentity> {
  return requestJson<DemoSession & WorkspaceIdentity>('/api/demo/session/exit', {
    method: 'POST',
  })
}

export async function resetDemoWorkspace(): Promise<void> {
  await requestJson<{ status: string }>('/api/demo/reset', { method: 'POST' })
}

export async function readDemoModelQuota(): Promise<ModelQuota> {
  return requestJson<ModelQuota>('/api/demo/model-quota')
}
