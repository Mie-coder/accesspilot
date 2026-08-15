import {
  AssistantRuntimeProvider,
  type ThreadHistoryAdapter,
  type ThreadMessageLike,
  useLocalRuntime,
} from '@assistant-ui/react'
import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import {
  createDecisionPacket,
  ApiError,
  readRequestDetail,
  replayEvents,
  previewDraft,
  submitRequest,
  startApproval as startApprovalRequest,
  subscribeWorkspaceEvents,
  type TurnSseFrame,
} from './api'
import { createChatModelAdapter } from './runtime'
import type {
  ChatTurn,
  ConnectionState,
  DecisionPacket,
  RequestDetail,
  RequestDraft,
  RequestResult,
  EntitlementSelectionResult,
  WorkspaceEvent,
  WorkspaceSnapshot,
} from './types'
import { WorkbenchContext, type WorkbenchContextValue } from './workbench-context'

const requiredFields: Array<keyof Pick<
  RequestDraft,
  'entitlement_id' | 'duration_days' | 'justification'
>> = ['entitlement_id', 'duration_days', 'justification']

function missingFields(draft: RequestDraft): string[] {
  return requiredFields.filter((field) => draft[field] === null)
}

const fallbackRecoverableError = '本轮可安全重试，请稍后再试。'

function isTerminalEvent(event: WorkspaceEvent): boolean {
  return event.type === 'message.completed' || event.type === 'turn.interrupted'
}

function activeRecoverableError(events: WorkspaceEvent[]): WorkspaceEvent | null {
  let active: WorkspaceEvent | null = null
  const orderedEvents = [...events].sort((left, right) => left.id - right.id)

  for (const event of orderedEvents) {
    if (event.type === 'error.recoverable') {
      active = event
      continue
    }
    if (isTerminalEvent(event)) active = null
  }

  return active
}

function latestRecoverableError(events: WorkspaceEvent[]): string | null {
  const event = activeRecoverableError(events)
  if (event === null) return null
  return typeof event.payload.message === 'string'
    ? event.payload.message
    : fallbackRecoverableError
}

function eventMessages(events: WorkspaceEvent[]): ThreadMessageLike[] {
  const activeRecoverableErrorId = activeRecoverableError(events)?.id ?? null
  const completedTurnIds = new Set(
    events
      .filter((event) => event.type === 'message.completed')
      .map((event) => event.payload.turn_id)
      .filter((turnId): turnId is string => typeof turnId === 'string'),
  )

  return events.flatMap((event): ThreadMessageLike[] => {
    const content = event.payload.content
    if (event.type === 'message.user') {
      return typeof content === 'string'
        ? [{ id: `event-${event.id}`, role: 'user' as const, content }]
        : []
    }
    if (event.type === 'message.completed') {
      const turnId = typeof event.payload.turn_id === 'string' ? event.payload.turn_id : null
      return typeof content === 'string'
        ? [{
            id: turnId ? `turn-${turnId}` : `event-${event.id}`,
            role: 'assistant' as const,
            content,
          }]
        : []
    }
    if (event.type === 'message.assistant') {
      const turnId = typeof event.payload.turn_id === 'string' ? event.payload.turn_id : null
      if (turnId && completedTurnIds.has(turnId)) return []
      return typeof content === 'string'
        ? [{ id: `event-${event.id}`, role: 'assistant' as const, content }]
        : []
    }
    if (event.type === 'error.recoverable') {
      if (event.id !== activeRecoverableErrorId) return []
      const message = typeof event.payload.message === 'string'
        ? event.payload.message
        : fallbackRecoverableError
      return [{
        id: `event-${event.id}`,
        role: 'assistant' as const,
        content: message,
        status: { type: 'incomplete' as const, reason: 'error' as const, error: message },
        metadata: { custom: { retryable: true } },
      }]
    }
    if (event.type === 'turn.interrupted') {
      const turnId = typeof event.payload.turn_id === 'string' ? event.payload.turn_id : null
      return event.payload.retryable !== false
        ? [{
            id: turnId ? `turn-${turnId}-interrupted` : `event-${event.id}`,
            role: 'assistant' as const,
            content: '本轮已中断，可以重新发送。',
            status: { type: 'incomplete' as const, reason: 'cancelled' as const },
            metadata: { custom: { retryable: true } },
          }]
        : []
    }
    return []
  })
}

function waitForReconnect(delayMs: number, signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) return Promise.resolve(false)
  return new Promise((resolve) => {
    const timeout = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve(true)
    }, delayMs)
    const onAbort = () => {
      window.clearTimeout(timeout)
      resolve(false)
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}


function submittedRequest(events: WorkspaceEvent[]): RequestResult | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event?.type !== 'business.status' || event.payload.status !== 'submitted') continue
    const requestId = event.payload.request_id
    if (typeof requestId === 'string') {
      return { request_id: requestId, request_status: 'submitted' }
    }
  }
  return null
}

export function WorkbenchRuntime({
  snapshot,
  children,
}: {
  snapshot: WorkspaceSnapshot
  children: ReactNode
}) {
  const [draft, setDraft] = useState<RequestDraft | null>(snapshot.draft)
  const [identity] = useState(snapshot.identity)
  const [events, setEvents] = useState(snapshot.events)
  const [businessStatus, setBusinessStatus] = useState('collecting')
  const [error, setError] = useState<string | null>(() => latestRecoverableError(snapshot.events))
  const [requestResult, setRequestResult] = useState<RequestResult | null>(() =>
    submittedRequest(snapshot.events),
  )
  const [decisionPacket, setDecisionPacket] = useState<DecisionPacket | null>(null)
  const [decisionPacketError, setDecisionPacketError] = useState<string | null>(null)
  const [approvalCase, setApprovalCase] = useState<RequestDetail['approval']>(null)
  const [approvalError, setApprovalError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [isGeneratingDecisionPacket, setIsGeneratingDecisionPacket] = useState(false)
  const [isStartingApproval, setIsStartingApproval] = useState(false)
  const packetRequestInFlightRef = useRef(false)
  const [retryableInterruption, setRetryableInterruption] = useState(false)
  const [connectionState, setConnectionState] = useState<ConnectionState>('connected')
  const lastEventIdRef = useRef(snapshot.lastEventId)
  const principalDraft = useMemo<RequestDraft>(() => ({
    employee_id: identity.employee_id,
    entitlement_id: draft?.entitlement_id ?? null,
    duration_days: draft?.duration_days ?? null,
    justification: draft?.justification ?? null,
    confirmed: draft?.confirmed ?? false,
  }), [draft, identity.employee_id])

  const onTurn = useCallback((turn: ChatTurn) => {
    setDraft(turn.draft)
    setBusinessStatus(turn.business_status)
    setError(null)
  }, [])

  const applyTurnEvent = useCallback((event: TurnSseFrame) => {
    const payload = event.data.payload
    if (event.event === 'draft.updated' && payload.draft !== null && typeof payload.draft === 'object') {
      setDraft(payload.draft as RequestDraft)
      setRetryableInterruption(false)
    }
    if (event.event === 'business.status' && typeof payload.status === 'string') {
      setBusinessStatus(payload.status)
    }
    if (event.event === 'error.recoverable') {
      setError(typeof payload.message === 'string' ? payload.message : fallbackRecoverableError)
    }
    if (event.event === 'message.completed' || event.event === 'turn.interrupted') {
      setError(null)
    }
    if (event.event === 'turn.interrupted') {
      setRetryableInterruption(payload.retryable !== false)
    }
  }, [])
  const onEvents = useCallback((newEvents: WorkspaceEvent[]) => {
    if (newEvents.length === 0) return
    setConnectionState('connected')
    lastEventIdRef.current = Math.max(
      lastEventIdRef.current,
      ...newEvents.map((event) => event.id),
    )
    for (const event of newEvents) {
      const payload = event.payload
      if (event.type === 'draft.updated' && payload.draft !== null && typeof payload.draft === 'object') {
        setDraft(payload.draft as RequestDraft)
      }
      if (event.type === 'business.status' && typeof payload.status === 'string') {
        setBusinessStatus(payload.status)
      }
      if (event.type === 'error.recoverable') {
        setError(typeof payload.message === 'string' ? payload.message : fallbackRecoverableError)
      }
      if (isTerminalEvent(event)) {
        setError(null)
      }
      if (event.type === 'turn.interrupted') {
        setRetryableInterruption(payload.retryable !== false)
      }
    }
    setEvents((current) => {
      const seen = new Set(current.map((event) => event.id))
      return [...current, ...newEvents.filter((event) => !seen.has(event.id))]
    })
  }, [])
  const onError = useCallback((message: string) => setError(message), [])

  const adapter = useMemo(
    () =>
      createChatModelAdapter({
        getLastEventId: () => lastEventIdRef.current,
        onTurn,
        onEvents,
        onError,
        onStreamEvent: applyTurnEvent,
      }),
    [applyTurnEvent, onError, onEvents, onTurn],
  )
  const initialMessages = useMemo(() => eventMessages(snapshot.events), [snapshot.events])
  const localHistory = useMemo<ThreadHistoryAdapter>(() => ({
    // The Workspace snapshot is the source of truth; returning null keeps initialMessages intact.
    load: async () => null as never,
    append: async () => undefined,
    delete: async () => undefined,
  }), [])
  const runtime = useLocalRuntime(adapter, {
    initialMessages,
    adapters: { history: localHistory },
  })

  useEffect(() => {
    const controller = new AbortController()
    let active = true

    const consume = async () => {
      let reconnectDelay = 250
      while (active && !controller.signal.aborted) {
        const before = lastEventIdRef.current
        try {
          for await (const event of subscribeWorkspaceEvents({
            afterId: before,
            signal: controller.signal,
            onOpen: () => setConnectionState('connected'),
          })) {
            if (!active) return
            setConnectionState('connected')
            onEvents([event])
          }
          if (!active || controller.signal.aborted) return
          setConnectionState('reconnecting')
        } catch {
          if (!active || controller.signal.aborted) return
          // Connection state is independent from the current turn's error. A
          // reconnect should not make the assistant answer look failed.
          setConnectionState('reconnecting')
        }
        if (lastEventIdRef.current > before) reconnectDelay = 250
        const shouldReconnect = await waitForReconnect(reconnectDelay, controller.signal)
        if (!shouldReconnect) return
        reconnectDelay = Math.min(reconnectDelay * 2, 2000)
      }
    }

    void consume()
    return () => {
      active = false
      controller.abort()
    }
  }, [onEvents])

  const generateDecisionPacket = useCallback(async (requestId: string) => {
    if (packetRequestInFlightRef.current) return
    packetRequestInFlightRef.current = true
    setIsGeneratingDecisionPacket(true)
    setDecisionPacketError(null)
    try {
      setDecisionPacket(await createDecisionPacket(requestId))
    } catch (packetError) {
      setDecisionPacketError(
        packetError instanceof Error ? packetError.message : '决策材料暂时无法生成，请重试。',
      )
    } finally {
      packetRequestInFlightRef.current = false
      setIsGeneratingDecisionPacket(false)
    }
  }, [])

  const submit = useCallback(async () => {
    setIsSubmitting(true)
    setError(null)
    try {
      const result = await submitRequest()
      setRequestResult(result)
      setDecisionPacket(null)
      setDecisionPacketError(null)
      setApprovalCase(null)
      setApprovalError(null)
      setBusinessStatus('submitted')
      await generateDecisionPacket(result.request_id)
      try {
        onEvents(await replayEvents(lastEventIdRef.current))
      } catch {
        // 正式申请已经提交成功；事件流失败不能把成功操作伪装成失败并诱导重复提交。
        setError('正式申请已创建，但活动流同步失败；刷新后可恢复状态。')
      }
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : '正式申请创建失败')
    } finally {
      setIsSubmitting(false)
    }
  }, [generateDecisionPacket, onEvents])

  const retryDecisionPacket = useCallback(async () => {
    if (requestResult === null) return
    await generateDecisionPacket(requestResult.request_id)
  }, [generateDecisionPacket, requestResult])

  const startApproval = useCallback(async () => {
    if (requestResult === null || decisionPacket === null || isStartingApproval) return
    setIsStartingApproval(true)
    setApprovalError(null)
    try {
      try {
        await startApprovalRequest(requestResult.request_id)
      } catch (startError) {
        // A repeated click or lost success response is safe: the requester
        // rereads the authoritative Case and treats an existing route as done.
        if (!(startError instanceof ApiError && startError.status === 409)) throw startError
      }
      const detail = await readRequestDetail(requestResult.request_id)
      if (detail.approval === null) throw new Error('审批操作已提交，但最新审批事实尚未可读，请重试。')
      setApprovalCase(detail.approval)
    } catch (startError) {
      setApprovalError(startError instanceof Error ? startError.message : '启动审批失败，请重试。')
    } finally {
      setIsStartingApproval(false)
    }
  }, [decisionPacket, isStartingApproval, requestResult])

  const selectEntitlement = useCallback(async (
    entitlementId: string,
  ): Promise<EntitlementSelectionResult> => {
    const candidateDraft = {
      entitlement_id: entitlementId,
      duration_days: draft?.duration_days ?? null,
      justification: draft?.justification ?? null,
      // Selecting a different candidate is a new business fact. The server
      // revalidates it and never accepts a stale confirmation from the UI.
      confirmed: false,
    }
    const preview = await previewDraft(candidateDraft)
    if (preview.draft !== null) setDraft(preview.draft)
    const nonMissingIssues = preview.issues.filter((issue) => !issue.code.startsWith('missing_fields:'))
    const revalidated = preview.draft?.entitlement_id === entitlementId
      && preview.entitlement_resolution?.status === 'matched'
      && preview.entitlement_resolution.candidates.length === 1
      && preview.entitlement_resolution.candidates[0]?.code === entitlementId
      && nonMissingIssues.length === 0
    const result: EntitlementSelectionResult = {
      status: revalidated ? 'revalidated' : 'rejected',
      code: entitlementId,
      message: revalidated
        ? '重新校验通过，可以继续补充申请字段。'
        : (nonMissingIssues[0]?.message ?? '当前身份已不再具备申请资格。'),
      previous_confirmation_invalidated: draft?.confirmed === true,
    }
    setError(null)
    return result
  }, [draft, identity.employee_id])


  const context = useMemo<WorkbenchContextValue>(
    () => ({
      identity,
      draft: principalDraft,
      missingFields: missingFields(principalDraft),
      events,
      businessStatus,
      error,
      requestResult,
      decisionPacket,
      decisionPacketError,
      approvalCase,
      approvalError,
      isSubmitting,
      isGeneratingDecisionPacket,
      isStartingApproval,
      retryableInterruption,
      connectionState,
      selectEntitlement,
      submit,
      retryDecisionPacket,
      startApproval,
    }),
    [
      businessStatus,
      draft,
      error,
      events,
      identity,
      decisionPacket,
      decisionPacketError,
      approvalCase,
      approvalError,
      isGeneratingDecisionPacket,
      isStartingApproval,
      isSubmitting,
      requestResult,
      retryableInterruption,
      connectionState,
      selectEntitlement,
      submit,
      retryDecisionPacket,
      startApproval,
      principalDraft,
    ],
  )

  return (
    <WorkbenchContext.Provider value={context}>
      <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
    </WorkbenchContext.Provider>
  )
}
