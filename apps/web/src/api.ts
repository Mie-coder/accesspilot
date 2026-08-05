import type {
  ChatTurn,
  ModelQuota,
  RequestDraft,
  RequestResult,
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
  let draft: RequestDraft | null
  try {
    draft = await readDraft()
  } catch (error) {
    if (!(error instanceof ApiError) || ![401, 404].includes(error.status)) throw error
    await requestJson<{ status: string }>('/api/workspaces', { method: 'POST' })
    draft = await readDraft()
  }

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
