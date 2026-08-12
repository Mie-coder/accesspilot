import type {
  ApprovalInbox,
  CaseList,
  ChatTurn,
  DecisionPacket,
  RequestDraft,
  RequestResult,
  RequestDetail,
  WorkspaceEvent,
  WorkspaceIdentity,
  WorkspaceSnapshot,
  AccessOverview,
  DraftPreviewResponse,
  EntitlementResolution,
  PolicyAnswer,
  PolicyCatalogItem,
  ProvisioningTaskList,
} from './types'

export interface AuthSessionPayload {
  csrf_token: string
  principal: WorkspaceIdentity
  expires_at: string
}

/** Fields accepted by the server-owned draft preview DTO. */
export type DraftPreviewInput = Omit<RequestDraft, 'employee_id'>

// Kept in module memory only.  The server rotates this value on every
// GET /api/auth/session; it is never written to localStorage/sessionStorage.
let csrfToken: string | null = null

export function resetAuthClientState(): void {
  csrfToken = null
}

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
  const headers = new Headers({
    Accept: 'text/event-stream',
    'Content-Type': 'application/json',
  })
  if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch('/api/chat/messages/stream', {
    method: 'POST',
    credentials: 'include',
    headers,
    body: JSON.stringify({ content }),
    signal,
  })
  notifyUnauthorized(response)
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
  onOpen?: () => void
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
  notifyUnauthorized(response)
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response))
  options.onOpen?.()
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

function notifyUnauthorized(response: Response): void {
  if (response.status === 401 && typeof window !== 'undefined') {
    window.dispatchEvent(new Event('accesspilot:unauthorized'))
  }
}

async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const requestHeaders = new Headers(init.headers)
  requestHeaders.set('Accept', 'application/json')
  if (method !== 'GET' && method !== 'HEAD' && !url.endsWith('/auth/login') && csrfToken) {
    requestHeaders.set('X-CSRF-Token', csrfToken)
  }
  const response = await fetch(url, {
    ...init,
    credentials: 'include',
    headers: requestHeaders,
  })
  notifyUnauthorized(response)
  if (!response.ok) {
    throw new ApiError(response.status, await errorMessage(response))
  }
  return (await response.json()) as T
}

async function readDraft(): Promise<RequestDraft | null> {
  const response = await requestJson<{ draft: RequestDraft | null }>('/api/drafts/current')
  return response.draft
}

export async function login(accountId: string): Promise<AuthSessionPayload> {
  const payload = await requestJson<AuthSessionPayload>('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account_id: accountId }),
  })
  csrfToken = payload.csrf_token
  return payload
}

export async function readAuthSession(): Promise<AuthSessionPayload> {
  const payload = await requestJson<AuthSessionPayload>('/api/auth/session')
  csrfToken = payload.csrf_token
  return payload
}

export async function logout(): Promise<void> {
  // Only clear client state after the server confirms revocation.  A failed
  // logout must not create a false anonymous state.
  await requestJson<{ status: string }>('/api/auth/logout', { method: 'POST' })
  csrfToken = null
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
  notifyUnauthorized(response)
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
  const authSession = await readAuthSession()
  const [draft, events] = await Promise.all([
    readDraft(),
    replayEvents(),
  ])
  return {
    identity: authSession.principal,
    draft,
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

/** Read the current actor's permission facts; never derive these from chat text. */
export async function readAccessOverview(signal?: AbortSignal): Promise<AccessOverview> {
  return requestJson<AccessOverview>('/api/access-overview', { signal })
}

/** Resolve a human permission name against the server-owned entitlement catalog. */
export async function resolveEntitlement(
  query: string,
  signal?: AbortSignal,
): Promise<EntitlementResolution> {
  return requestJson<EntitlementResolution>('/api/entitlements/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query }),
    signal,
  })
}

/** Persist a preview/revalidation result through the deterministic draft endpoint. */
export async function previewDraft(
  draft: DraftPreviewInput,
  signal?: AbortSignal,
): Promise<DraftPreviewResponse> {
  return requestJson<DraftPreviewResponse>('/api/drafts/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(draft),
    signal,
  })
}

export async function readPolicyCatalog(signal?: AbortSignal): Promise<PolicyCatalogItem[]> {
  const payload = await requestJson<{ policies?: PolicyCatalogItem[]; status?: string }>(
    '/api/policies',
    { signal },
  )
  if (payload.status === 'retrieval_unavailable') {
    throw new Error('政策服务暂时不可用，请稍后重试。')
  }
  return Array.isArray(payload.policies) ? payload.policies : []
}

export async function queryPolicy(
  query: string,
  signal?: AbortSignal,
): Promise<PolicyAnswer> {
  return requestJson<PolicyAnswer>('/api/policies/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query }),
    signal,
  })
}

export async function readLatestRequest(signal?: AbortSignal): Promise<RequestDetail | null> {
  const payload = await requestJson<{ request: RequestDetail | null }>(
    '/api/requests/latest',
    { signal },
  )
  return payload.request ?? null
}

/** List only formal Cases submitted by the authenticated Principal. */
export async function readMyRequests(signal?: AbortSignal): Promise<CaseList> {
  return requestJson<CaseList>('/api/requests/mine', { signal })
}

/** List formal Cases related to the current approver/admin through SQL ACLs. */
export async function readAccessibleRequests(signal?: AbortSignal): Promise<CaseList> {
  return requestJson<CaseList>('/api/requests/accessible', { signal })
}

/** Read only actions derived by the server for the fixed permissions administrator. */
export async function readProvisioningTasks(signal?: AbortSignal): Promise<ProvisioningTaskList> {
  return requestJson<ProvisioningTaskList>('/api/provisioning-tasks', { signal })
}

export async function submitRequest(signal?: AbortSignal): Promise<RequestResult> {
  return requestJson<RequestResult>('/api/requests', { method: 'POST', signal })
}

/** Create or return the server-owned immutable Packet; the client supplies no business facts. */
export async function createDecisionPacket(
  requestId: string,
  signal?: AbortSignal,
): Promise<DecisionPacket> {
  return requestJson<DecisionPacket>(
    `/api/requests/${encodeURIComponent(requestId)}/decision-packet`,
    { method: 'POST', signal },
  )
}

export async function readApprovalInbox(): Promise<ApprovalInbox> {
  return requestJson<ApprovalInbox>('/api/approval-inbox')
}

export async function readRequestDetail(
  requestId: string,
  signal?: AbortSignal,
): Promise<RequestDetail> {
  return requestJson<RequestDetail>(`/api/requests/${encodeURIComponent(requestId)}`, { signal })
}

export async function startApproval(requestId: string): Promise<void> {
  await requestJson(`/api/requests/${encodeURIComponent(requestId)}/approval-case`, {
    method: 'POST',
  })
}

export async function decideApproval(
  caseId: string,
  approvalStepId: string,
  decision: 'approve' | 'reject',
  comment: string | null,
): Promise<void> {
  await requestJson(`/api/approval-cases/${encodeURIComponent(caseId)}/decisions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approval_step_id: approvalStepId, decision, comment }),
  })
}

export async function provisionRequest(requestId: string): Promise<void> {
  await requestJson(`/api/requests/${encodeURIComponent(requestId)}/provision`, {
    method: 'POST',
  })
}

export async function recoverProvisioning(requestId: string): Promise<void> {
  await requestJson(`/api/requests/${encodeURIComponent(requestId)}/provision/recover`, {
    method: 'POST',
  })
}
