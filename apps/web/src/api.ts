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

export interface SseFrame<T = unknown> {
  id: string
  event: string
  data: T
}

export interface TurnEventEnvelope {
  schema_version: 'v1'
  turn_id: string
  seq: number
  occurred_at: string
  payload: Record<string, unknown>
}

export type TurnSseFrame = SseFrame<TurnEventEnvelope>

const TURN_EVENTS = new Set([
  'turn.started',
  'intent.detected',
  'tool.started',
  'tool.completed',
  'draft.updated',
  'message.delta',
  'message.completed',
  'business.status',
  'turn.interrupted',
  'error.recoverable',
])

const TERMINAL_EVENTS = new Set([
  'message.completed',
  'error.recoverable',
  'turn.interrupted',
])

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function abortError(): DOMException {
  return new DOMException('请求已取消', 'AbortError')
}

function assertTurnEnvelope(frame: SseFrame, requireOccurredAt = true): TurnSseFrame {
  if (!TURN_EVENTS.has(frame.event) || !isRecord(frame.data)) {
    throw new Error('当前轮 SSE 事件格式无效')
  }
  const data = frame.data
  if (
    data.schema_version !== 'v1' ||
    typeof data.turn_id !== 'string' ||
    data.turn_id.length === 0 ||
    typeof data.seq !== 'number' ||
    !Number.isSafeInteger(data.seq) ||
    data.seq < 1 ||
    (requireOccurredAt && (typeof data.occurred_at !== 'string' || data.occurred_at.length === 0)) ||
    !isRecord(data.payload) ||
    frame.id !== `${data.turn_id}:${data.seq}`
  ) {
    throw new Error('当前轮 SSE 事件格式无效')
  }
  return { id: frame.id, event: frame.event, data: data as unknown as TurnEventEnvelope }
}

function responseStream(response: Response): ReadableStream<Uint8Array> {
  if (response.body === null) throw new Error('后端没有返回 SSE 流')
  return response.body as ReadableStream<Uint8Array>
}


/**
 * Parse an SSE response incrementally. It consumes bytes rather than
 * Response.text(), so a frame may be split at any UTF-8 boundary.
 */
export async function* parseSseStream(
  stream: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<SseFrame> {
  if (signal?.aborted) throw abortError()
  const reader = stream.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  let buffer = ''
  let block: string[] = []
  let turnId: string | null = null
  let lastSeq: number | null = null
  const terminalTurns = new Set<string>()
  let cancelled = false
  const queued: SseFrame[] = []

  const onAbort = () => {
    cancelled = true
    void reader.cancel().catch(() => undefined)
  }
  signal?.addEventListener('abort', onAbort, { once: true })

  const dispatch = () => {
    if (block.length === 0) return
    const lines = block
    block = []
    if (lines.every((line) => line.startsWith(':'))) return

    let id: string | null = null
    let event: string | null = null
    const dataLines: string[] = []
    for (const line of lines) {
      if (line.startsWith(':')) continue
      const separator = line.indexOf(':')
      if (separator < 0) throw new Error('后端 SSE 字段格式无效')
      const field = line.slice(0, separator)
      const value = line.slice(separator + 1).replace(/^ /, '')
      if (field === 'id') id = value
      else if (field === 'event') event = value
      else if (field === 'data') dataLines.push(value)
    }
    if (!id || !event || dataLines.length === 0) {
      throw new Error('后端 SSE 事件缺少必要字段')
    }
    let data: unknown
    try {
      data = JSON.parse(dataLines.join('\n'))
    } catch {
      throw new Error('后端 SSE payload 不是有效 JSON')
    }
    if (!isRecord(data)) throw new Error('后端 SSE payload 无效')

    const frame: SseFrame = { id, event, data }
    if ('schema_version' in data) {
      const validated = assertTurnEnvelope(frame, false)
      if (turnId === null) turnId = validated.data.turn_id
      if (turnId !== validated.data.turn_id) {
        throw new Error('当前轮 SSE turn_id 不一致')
      }
      if (lastSeq !== null && validated.data.seq !== lastSeq + 1) {
        throw new Error('当前轮 SSE seq 不连续')
      }
      lastSeq = validated.data.seq
      if (TERMINAL_EVENTS.has(validated.event)) {
        if (terminalTurns.has(validated.data.turn_id)) {
          throw new Error('当前轮 SSE 出现多个终态')
        }
        terminalTurns.add(validated.data.turn_id)
      }
      queued.push(validated)
    } else {
      queued.push(frame)
    }
  }

  const consumeText = (text: string, final = false) => {
    buffer += text
    while (buffer.length > 0) {
      let newline = -1
      let width = 1
      for (let index = 0; index < buffer.length; index += 1) {
        const character = buffer[index]
        if (character === '\n') {
          newline = index
          width = 1
          break
        }
        if (character === '\r') {
          if (index === buffer.length - 1 && !final) break
          newline = index
          width = buffer[index + 1] === '\n' ? 2 : 1
          break
        }
      }
      if (newline < 0) break
      const line = buffer.slice(0, newline)
      buffer = buffer.slice(newline + width)
      if (line.length === 0) dispatch()
      else block.push(line)
    }
    if (final && buffer.length > 0) {
      block.push(buffer)
      buffer = ''
      dispatch()
    }
  }

  try {
    while (true) {
      if (cancelled || signal?.aborted) throw abortError()
      const result = await reader.read()
      if (cancelled || signal?.aborted) throw abortError()
      if (result.done) {
        consumeText(decoder.decode(), true)
        if (block.length > 0) dispatch()
        while (queued.length > 0) yield queued.shift() as SseFrame
        return
      }
      consumeText(decoder.decode(result.value, { stream: true }))
      while (queued.length > 0) yield queued.shift() as SseFrame
    }
  } catch (error) {
    if (cancelled || signal?.aborted) throw abortError()
    if (error instanceof TypeError) throw new Error('后端 SSE 编码无效')
    throw error
  } finally {
    signal?.removeEventListener('abort', onAbort)
    reader.releaseLock()
  }
}


export async function* streamChatMessage(
  content: string,
  signal?: AbortSignal,
): AsyncGenerator<TurnSseFrame> {
  const response = await fetch('/api/chat/messages/stream', {
    method: 'POST',
    credentials: 'include',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ content }),
    signal,
  })
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response))

  let terminalSeen = false
  for await (const frame of parseSseStream(responseStream(response), signal)) {
    const current = assertTurnEnvelope(frame, true)
    if (terminalSeen) throw new Error('当前轮 SSE 终态之后仍有事件')
    if (TERMINAL_EVENTS.has(current.event)) terminalSeen = true
    yield current
  }
  if (!terminalSeen) throw new Error('当前轮 SSE 缺少终态')
}

export interface WorkspaceEventSubscriptionOptions {
  afterId?: number
  signal?: AbortSignal
}

export async function* subscribeWorkspaceEvents(
  afterIdOrOptions: number | WorkspaceEventSubscriptionOptions = 0,
  signal?: AbortSignal,
): AsyncGenerator<WorkspaceEvent> {
  const options = typeof afterIdOrOptions === 'number'
    ? { afterId: afterIdOrOptions, signal }
    : afterIdOrOptions
  const afterId = options.afterId ?? 0
  if (!Number.isSafeInteger(afterId) || afterId < 0) {
    throw new Error('事件游标无效')
  }
  const response = await fetch('/api/events', {
    credentials: 'include',
    headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(afterId) },
    signal: options.signal,
  })
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response))
  for await (const frame of parseSseStream(responseStream(response), options.signal)) {
    const id = Number(frame.id)
    if (!Number.isSafeInteger(id) || id < 0 || !isRecord(frame.data)) {
      throw new Error('后端事件格式无效')
    }
    yield { id, type: frame.event, payload: frame.data }
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
  const response = await fetch('/api/events?follow=false', {
    credentials: 'include',
    headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(afterId) },
    signal,
  })
  if (!response.ok) {
    throw new ApiError(response.status, await errorMessage(response))
  }
  const events: WorkspaceEvent[] = []
  for await (const frame of parseSseStream(responseStream(response), signal)) {
    const id = Number(frame.id)
    if (!Number.isSafeInteger(id) || id < 0 || !isRecord(frame.data)) {
      throw new Error('后端事件格式无效')
    }
    events.push({ id, type: frame.event, payload: frame.data })
  }
  return events
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
