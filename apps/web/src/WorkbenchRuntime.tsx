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
  replayEvents,
  submitRequest,
  subscribeWorkspaceEvents,
  type TurnSseFrame,
} from './api'
import { createChatModelAdapter } from './runtime'
import type {
  ChatTurn,
  RequestDraft,
  RequestResult,
  WorkspaceEvent,
  WorkspaceSnapshot,
} from './types'
import { WorkbenchContext, type WorkbenchContextValue } from './workbench-context'

const requiredFields: Array<keyof Pick<
  RequestDraft,
  'employee_id' | 'entitlement_id' | 'duration_days' | 'justification'
>> = ['employee_id', 'entitlement_id', 'duration_days', 'justification']

function missingFields(draft: RequestDraft | null): string[] {
  if (draft === null) return [...requiredFields]
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
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [retryableInterruption, setRetryableInterruption] = useState(false)
  const lastEventIdRef = useRef(snapshot.lastEventId)

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
          for await (const event of subscribeWorkspaceEvents(before, controller.signal)) {
            if (!active) return
            onEvents([event])
          }
          if (!active || controller.signal.aborted) return
        } catch (streamError) {
          if (!active || controller.signal.aborted) return
          setError(streamError instanceof Error ? streamError.message : '活动流暂时断开')
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

  const submit = useCallback(async () => {
    setIsSubmitting(true)
    setError(null)
    try {
      const result = await submitRequest()
      setRequestResult(result)
      setBusinessStatus('submitted')
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
  }, [onEvents])


  const context = useMemo<WorkbenchContextValue>(
    () => ({
      identity,
      draft,
      missingFields: missingFields(draft),
      demoSession: snapshot.demoSession,
      events,
      businessStatus,
      error,
      requestResult,
      isSubmitting,
      retryableInterruption,
      submit,
    }),
    [
      businessStatus,
      draft,
      error,
      events,
      identity,
      isSubmitting,
      requestResult,
      retryableInterruption,
      submit,
    ],
  )

  return (
    <WorkbenchContext.Provider value={context}>
      <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
    </WorkbenchContext.Provider>
  )
}
