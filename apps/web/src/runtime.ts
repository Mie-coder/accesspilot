import type { ChatModelAdapter } from '@assistant-ui/react'

import { ApiError, replayEvents, sendChatMessage } from './api'
import type { ChatTurn, WorkspaceEvent } from './types'

interface AdapterCallbacks {
  getLastEventId: () => number
  onTurn: (turn: ChatTurn) => void
  onEvents: (events: WorkspaceEvent[]) => void
  onError: (message: string) => void
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
  if (error instanceof ApiError || error instanceof Error) return error.message
  return '对话服务暂时不可用，请稍后重试。'
}

export function createChatModelAdapter(callbacks: AdapterCallbacks): ChatModelAdapter {
  return {
    async *run({ messages, abortSignal }) {
      try {
        const turn = await sendChatMessage(latestUserText(messages), abortSignal)
        callbacks.onTurn(turn)

        try {
          const events = await replayEvents(callbacks.getLastEventId(), abortSignal)
          callbacks.onEvents(events)
        } catch (error) {
          if (error instanceof DOMException && error.name === 'AbortError') throw error
          // 本轮业务结果已由 POST 返回；SSE 回放失败只降级活动流，不重复调用模型。
          callbacks.onError('本轮结果已保存，但实时事件同步失败，可稍后刷新重试。')
        }

        yield { content: [{ type: 'text', text: turn.assistant_message }] }
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') throw error
        callbacks.onError(safeErrorMessage(error))
        throw error
      }
    },
  }
}
