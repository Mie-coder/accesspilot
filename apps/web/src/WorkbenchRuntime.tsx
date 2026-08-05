import {
  AssistantRuntimeProvider,
  type ThreadMessageLike,
  useLocalRuntime,
} from '@assistant-ui/react'
import {
  type ReactNode,
  useCallback,
  useMemo,
  useRef,
  useState,
} from 'react'

import { replayEvents, startNewWorkspace, submitRequest } from './api'
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

function eventMessages(events: WorkspaceEvent[]): ThreadMessageLike[] {
  return events.flatMap((event) => {
    if (event.type !== 'message.user' && event.type !== 'message.assistant') return []
    const content = event.payload.content
    if (typeof content !== 'string') return []
    const role: 'user' | 'assistant' =
      event.type === 'message.user' ? 'user' : 'assistant'
    return [{ id: `event-${event.id}`, role, content }]
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
  const [quota, setQuota] = useState(snapshot.quota)
  const [events, setEvents] = useState(snapshot.events)
  const [businessStatus, setBusinessStatus] = useState('collecting')
  const [error, setError] = useState<string | null>(null)
  const [requestResult, setRequestResult] = useState<RequestResult | null>(() =>
    submittedRequest(snapshot.events),
  )
  const [isSubmitting, setIsSubmitting] = useState(false)
  const lastEventIdRef = useRef(snapshot.lastEventId)

  const onTurn = useCallback((turn: ChatTurn) => {
    setDraft(turn.draft)
    setQuota(turn.quota)
    setBusinessStatus(turn.business_status)
    setError(null)
  }, [])
  const onEvents = useCallback((newEvents: WorkspaceEvent[]) => {
    if (newEvents.length === 0) return
    lastEventIdRef.current = newEvents.at(-1)?.id ?? lastEventIdRef.current
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
      }),
    [onError, onEvents, onTurn],
  )
  const initialMessages = useMemo(() => eventMessages(snapshot.events), [snapshot.events])
  const runtime = useLocalRuntime(adapter, { initialMessages })

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

  const reset = useCallback(async () => {
    setIsSubmitting(true)
    setError(null)
    try {
      // 新 Workspace 比删除旧审计记录更安全，也能得到全新的模型额度和事件游标。
      await startNewWorkspace()
      window.location.reload()
    } catch (resetError) {
      setError(resetError instanceof Error ? resetError.message : '演示空间重置失败')
      setIsSubmitting(false)
    }
  }, [])

  const context = useMemo<WorkbenchContextValue>(
    () => ({
      draft,
      missingFields: missingFields(draft),
      quota,
      events,
      businessStatus,
      error,
      requestResult,
      isSubmitting,
      submit,
      reset,
    }),
    [businessStatus, draft, error, events, isSubmitting, quota, requestResult, reset, submit],
  )

  return (
    <WorkbenchContext.Provider value={context}>
      <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
    </WorkbenchContext.Provider>
  )
}
