import type {
  ApprovalInbox,
  ChatTurn,
  ModelQuota,
  RequestDraft,
  RequestResult,
  RequestDetail,
  WorkspaceEvent,
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
  const draft = await readDraft()

  const [quota, events] = await Promise.all([
    requestJson<ModelQuota>('/api/model-quota'),
    replayEvents(),
  ])
  return {
    draft,
    quota,
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

export async function startNewWorkspace(): Promise<void> {
  await requestJson<{ status: string }>('/api/workspaces', { method: 'POST' })
}

export async function readApprovalInbox(actorId: string): Promise<ApprovalInbox> {
  return requestJson<ApprovalInbox>(
    `/api/approval-inbox?actor_id=${encodeURIComponent(actorId)}`,
  )
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
  actorId: string,
  decision: 'approve' | 'reject',
  comment: string | null,
): Promise<void> {
  await requestJson(`/api/approval-cases/${encodeURIComponent(caseId)}/decisions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actor_id: actorId, decision, comment }),
  })
}

export async function setFaultMode(
  faultMode: 'iam_failure' | 'iam_timeout' | null,
): Promise<void> {
  await requestJson('/api/workspaces/fault-mode', {
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
