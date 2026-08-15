import type { ChatModelAdapter } from '@assistant-ui/react'

import { ApiError, streamChatMessage, type TurnSseFrame } from './api'
import type { ChatTurn, WorkspaceEvent } from './types'

interface AdapterCallbacks {
  getLastEventId: () => number
  onTurn: (turn: ChatTurn) => void
  onEvents: (events: WorkspaceEvent[]) => void
  onError: (message: string) => void
  onStreamEvent?: (event: TurnSseFrame) => void
}

function latestUserText(messages: Parameters<ChatModelAdapter['run']>[0]['messages']): string {
  let userMessage = messages[messages.length - 1]
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === 'user') {
      userMessage = messages[index]
      break
    }
  }
  if (!userMessage || userMessage.role !== 'user') throw new Error('没有可发送的用户消息')
  const content = userMessage.content
    .filter((part) => part.type === 'text')
    .map((part) => part.text)
    .join('\n')
    .trim()
  if (!content) throw new Error('消息不能为空')
  return content
}

function safeErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error && error.name === 'AbortError') return '请求已取消'
  if (error instanceof Error && error.message.length > 0) {
    if (error.message.includes('SSE') || error.message.includes('后端')) {
      return '实时响应格式无效，请稍后重试。'
    }
    return error.message
  }
  return '对话服务暂时不可用，请稍后重试。'
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
    || error instanceof Error && error.name === 'AbortError'
}

function textField(payload: Record<string, unknown>, key: string): string | null {
  return typeof payload[key] === 'string' ? payload[key] : null
}

export function createChatModelAdapter(callbacks: AdapterCallbacks): ChatModelAdapter {
  return {
    async *run({ messages, abortSignal }) {
      let cumulative = ''
      let errorReported = false
      try {
        for await (const event of streamChatMessage(latestUserText(messages), abortSignal)) {
          callbacks.onStreamEvent?.(event)
          const payload = event.data.payload

          if (event.event === 'message.delta') {
            const delta = textField(payload, 'text') ?? textField(payload, 'delta')
            if (!delta) throw new Error('当前轮 SSE 增量缺少文本')
            cumulative += delta
            yield { content: [{ type: 'text', text: cumulative }] }
            continue
          }

          if (event.event === 'message.completed') {
            const completionDelta = textField(payload, 'delta') ?? textField(payload, 'text')
            if (completionDelta && !cumulative.endsWith(completionDelta)) {
              cumulative += completionDelta
              yield { content: [{ type: 'text', text: cumulative }] }
            }
            const content = textField(payload, 'content')
            if (content && content !== cumulative) {
              const suffix = content.startsWith(cumulative) ? content.slice(cumulative.length) : content
              if (suffix) yield { content: [{ type: 'text', text: content }] }
              cumulative = content
            }
            continue
          }

          if (event.event === 'turn.interrupted') {
            yield { status: { type: 'incomplete', reason: 'cancelled' } }
            continue
          }

          if (event.event === 'error.recoverable') {
            const message = textField(payload, 'message') ?? '本轮可安全重试，请稍后再试。'
            callbacks.onError(message)
            errorReported = true
            throw new Error(message)
          }
        }
      } catch (error) {
        if (isAbortError(error)) throw error
        if (!errorReported) callbacks.onError(safeErrorMessage(error))
        throw error
      }
    },
  }
}
